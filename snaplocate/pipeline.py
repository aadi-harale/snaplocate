"""The seven stages, wired together.

DEGRADATION CONTRACT -- every one of these is exercised by scripts/selftest.py:
  no image in issue   -> stop with a clear reason (nothing to x-ray)
  xray call fails     -> stop with a clear reason, report the stage
  no graph.json       -> search-only ranking, `degraded` says so
  graph resolves 0    -> search-only ranking, `degraded` says so
  rerank call fails   -> ranking still returned, rerank section marked failed
  no community field  -> difficulty M/unknown rather than a confident guess

Nothing in here raises for an expected failure. `run()` returns a result dict
with a `degraded` list; an empty list means every stage did its job.
"""
import time

from . import config, fuse, gemma, gh, search
from .graph import CodeGraph


def _now():
    return time.time()


def run(issue_url=None, repo_root=".", graph_path=None, *,
        issue=None, image=None, xray_fn=None, rerank_fn=None,
        search_only=False, do_rerank=True, model=None):
    """Run the pipeline.

    Injection points, used by the self-test and the eval so neither needs a
    network or an API key:
      issue     -- a pre-fetched issue dict (skips stage 1)
      image     -- (bytes, mime) tuple (skips the download)
      xray_fn   -- replaces gemma.xray
      rerank_fn -- replaces gemma.rerank
    """
    t0 = _now()
    out = {
        "issue_url": issue_url,
        "repo_root": repo_root,
        "graph_path": graph_path,
        "degraded": [],
        "timing": {},
        "variant": "search-only" if search_only else "snaplocate",
        "weights_are_untuned": True,
    }

    # -- stage 1: FETCH -----------------------------------------------------
    if issue is None:
        if not issue_url:
            out["error"] = "no issue_url and no pre-fetched issue"
            return out
        try:
            issue = gh.fetch_issue(issue_url)
        except gh.GitHubError as exc:
            out["error"] = "FETCH failed: %s" % exc
            out["failed_stage"] = "fetch"
            return out
    out["issue"] = {k: issue.get(k) for k in
                    ("owner", "repo", "number", "title", "url", "state")}
    out["image_urls"] = issue.get("image_urls", [])
    out["timing"]["fetch"] = round(_now() - t0, 3)

    if image is None:
        urls = issue.get("image_urls") or []
        if not urls:
            out["error"] = ("no image found in the issue body or comments; "
                            "SnapLocate needs a screenshot")
            out["failed_stage"] = "fetch_image"
            return out
        try:
            image = gh.download_image(urls[0])
        except gh.GitHubError as exc:
            out["error"] = "image download failed: %s" % exc
            out["failed_stage"] = "fetch_image"
            return out
    image_bytes, image_mime = image
    out["image_bytes"] = len(image_bytes)
    out["image_mime"] = image_mime

    # -- stage 2: XRAY ------------------------------------------------------
    t = _now()
    fn = xray_fn or gemma.xray
    try:
        xr = fn(image_bytes, image_mime, gh.issue_context(issue), model=model)
    except Exception as exc:
        out["error"] = "XRAY failed: %s: %s" % (type(exc).__name__, exc)
        out["failed_stage"] = "xray"
        return out
    out["xray"] = xr
    out["timing"]["xray"] = round(_now() - t, 3)

    if not xr.get("regions"):
        out["error"] = "the model returned no readable regions from the image"
        out["failed_stage"] = "xray_empty"
        return out

    # -- stage 3: SEARCH ----------------------------------------------------
    t = _now()
    corpus = search.load_repo(repo_root)
    if not corpus:
        out["error"] = ("no source files found under %s -- wrong --repo path?"
                        % repo_root)
        out["failed_stage"] = "search"
        return out
    sr = search.seed_scores(corpus, xr["regions"])
    out["search"] = {k: sr[k] for k in
                     ("regions_dropped", "suspect_boosted", "files_scanned")}
    out["region_files"] = search.region_file_map(sr["regions_used"])
    out["timing"]["search"] = round(_now() - t, 3)

    if not sr["seed"]:
        out["error"] = ("none of the on-screen strings were found in %s. The "
                        "model may have rewritten them, or this is the wrong "
                        "repo." % repo_root)
        out["failed_stage"] = "search_empty"
        out["strings_tried"] = [r["text"] for r in xr["regions"]]
        return out

    repo_files = list(corpus.keys())

    # -- stage 4: GRAPH -----------------------------------------------------
    t = _now()
    cg = None
    graph_scores = {}
    if search_only:
        out["degraded"].append("graph skipped: search-only variant requested")
    else:
        cg = CodeGraph.load(graph_path)
        out["graph"] = cg.doctor_report()
        if not cg.ok:
            out["degraded"].append("no usable graph (%s): ranking by search "
                                   "only" % cg.reason)
            cg = None
        else:
            graph_scores, ginfo = cg.propagate(sr["seed"], repo_files)
            out["graph"]["propagation"] = ginfo
            if not graph_scores:
                out["degraded"].append(
                    "graph loaded (%d nodes) but no repo file resolved to a "
                    "node: ranking by search only" % cg.n_nodes)
                cg = None
            elif cg.builder_note:
                out["degraded"].append(
                    "graph came from the FALLBACK builder, not Graphify")
    out["timing"]["graph"] = round(_now() - t, 3)

    # -- stage 5: FUSE ------------------------------------------------------
    effective_search_only = search_only or not graph_scores
    ranked = fuse.fuse(sr["seed"], graph_scores,
                       search_only=effective_search_only)
    for r in ranked:
        r["evidence"] = sr["evidence"].get(r["path"], [])
    out["ranked"] = ranked
    out["ranking_used_graph"] = bool(graph_scores) and not search_only

    # -- stage 6: RERANK ----------------------------------------------------
    t = _now()
    out["rerank"] = {"ran": False, "reason": "skipped"}
    if do_rerank and ranked:
        rfn = rerank_fn or gemma.rerank
        try:
            rr = rfn(xr, ranked[: config.RERANK_SHORTLIST], model=model)
            rr["ran"] = True
            out["rerank"] = rr
            if rr.get("hallucinated_paths_dropped"):
                out["degraded"].append(
                    "rerank proposed %d path(s) not on the shortlist; dropped"
                    % len(rr["hallucinated_paths_dropped"]))
        except Exception as exc:
            out["rerank"] = {"ran": False,
                             "reason": "%s: %s" % (type(exc).__name__, exc)}
            out["degraded"].append("rerank failed (%s): showing the fused "
                                   "ranking instead" % type(exc).__name__)
    out["timing"]["rerank"] = round(_now() - t, 3)

    # -- stage 7: OUTPUT ---------------------------------------------------
    # The files shown to the user: rerank's order if it ran, else the fusion.
    #
    # The shortlist is re-enforced HERE as well as inside gemma.rerank. That is
    # deliberate duplication: this is the last gate before a path is shown to a
    # contributor, and it must hold for ANY rerank implementation -- a stub, a
    # swapped-in local Ollama call, a future model -- not only for the one
    # function that happens to filter internally today.
    by_path = {r["path"]: r for r in ranked}
    final = []
    if out["rerank"].get("ran") and out["rerank"].get("top"):
        rejected = []
        for item in out["rerank"]["top"]:
            p = item.get("path")
            if p not in by_path:
                rejected.append(p)
                continue
            row = dict(by_path[p])
            row["reason"] = item.get("reason", "")
            final.append(row)
        if rejected:
            out["degraded"].append(
                "rerank returned %d path(s) not in the ranking; dropped at the "
                "output gate: %s" % (len(rejected), rejected))
            out["rerank"]["rejected_at_output"] = rejected
        out["rerank"]["call_path"] = [
            p for p in (out["rerank"].get("call_path") or [])
            if isinstance(p, str) and p.split("::")[0] in by_path
        ]
    if final:
        out["final_source"] = "rerank"
    else:
        if out["rerank"].get("ran"):
            out["degraded"].append(
                "rerank returned no usable path: showing the fused ranking")
        final = [dict(r) for r in ranked[:3]]
        out["final_source"] = "fusion"
    out["final"] = final

    # A drawable neighbourhood around the picks, so the UI can show the code
    # graph instead of only asserting that one was used.
    out["subgraph"] = (cg.subgraph([f["path"] for f in (final or ranked)[:3]],
                                   repo_files)
                       if cg and cg.ok else
                       {"nodes": [], "edges": [], "truncated": False})

    out["difficulty"] = fuse.difficulty(final or ranked, cg, repo_files)
    out["blast_radius"] = fuse.blast_radius(final or ranked, cg, repo_files)
    out["timing"]["total"] = round(_now() - t0, 3)
    return out
