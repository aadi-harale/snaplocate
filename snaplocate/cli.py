"""SnapLocate CLI.

    python3 -m snaplocate.cli doctor --graph graphify-out/graph.json
    python3 -m snaplocate.cli locate <issue_url> --repo . --graph <path>
    python3 -m snaplocate.cli mkgraph --repo . --out graphify-out/graph.json
    python3 -m snaplocate.cli verify-boxes <image> --out boxes.html
    python3 -m snaplocate.cli eval data/issues.csv --split test
    python3 -m snaplocate.cli serve --repo . --graph <path> --port 8000
"""
import argparse
import json
import os
import sys

from . import cache, config, evalh, gemma, gh, mkgraph, pipeline
from .graph import CodeGraph

DEFAULT_GRAPH = os.path.join("graphify-out", "graph.json")


# -- doctor -----------------------------------------------------------------
def cmd_doctor(args):
    """Print the sniffed graph schema so a mismatch is obvious immediately."""
    gpath = args.graph or DEFAULT_GRAPH
    cg = CodeGraph.load(gpath)
    rep = cg.doctor_report()

    print("graph file   : %s" % rep["source"])
    print("loaded       : %s" % ("yes" if rep["ok"] else "NO"))
    print("reason       : %s" % rep["reason"])
    print("nodes / edges: %s / %s" % (rep["nodes"], rep["edges"]))
    print("built by     : %s%s" % (rep.get("builder"),
          "  <-- the real Graphify" if rep.get("builder") == "graphify" else ""))
    if rep.get("relations"):
        print("edge relations: %s" % ", ".join(
            "%s=%d" % kv for kv in list(rep["relations"].items())[:6]))
    if rep.get("communities"):
        print("communities  : %d" % rep["communities"])
    print("")
    print("SNIFFED SCHEMA (nothing is hard-coded; these were chosen by")
    print("scoring each candidate key on how path-like its values are)")
    print("  path field     : %s" % rep["path_key"])
    for c in rep["path_candidates"]:
        print("      %-14s coverage=%.2f quality=%.2f score=%.3f"
              % (c["key"], c["coverage"], c["quality"], c["score"]))
    print("  community field: %s" % rep["community_key"])
    for c in rep["community_candidates"][:4]:
        print("      %-14s coverage=%.2f distinct=%s score=%.3f"
              % (c["key"], c["coverage"], c.get("distinct"), c["score"]))
    print("  edge fields    : %s -> %s"
          % (rep["edge_keys"]["source"], rep["edge_keys"]["target"]))
    print("")
    print("sample node paths from the graph:")
    for p in rep["sample_paths"]:
        print("  %s" % p)

    if rep.get("builder_note"):
        print("")
        print("!! THIS GRAPH IS NOT FROM GRAPHIFY !!")
        for c in rep["builder_note"]:
            print("   - %s" % c)

    repo = args.repo or "."
    from .search import load_repo
    corpus = load_repo(repo)
    print("")
    print("repo         : %s" % os.path.abspath(repo))
    print("source files : %d" % len(corpus))

    if not corpus:
        print("")
        print("0 source files found. Wrong --repo, or everything is in a")
        print("skipped directory (see config.SKIP_DIRS).")
        return 1

    if not cg.ok:
        print("")
        print("FIX: open the graph JSON, find the field holding the file path,")
        print("     and add its name to PATH_KEYS in snaplocate/graph.py")
        return 1

    mapping, how = cg.resolve(list(corpus.keys()))
    print("resolved     : %d of %d files map to a graph node"
          % (len(mapping), len(corpus)))
    print("  exact=%d suffix=%d basename=%d unresolved=%d"
          % (how["exact"], how["suffix"], how["basename"], how["unresolved"]))

    if not mapping:
        print("")
        print("0 FILES RESOLVED. The graph loaded but nothing lines up.")
        print("FIX: compare 'sample node paths' above with your repo layout.")
        print("     If the field is wrong, add the right key to PATH_KEYS in")
        print("     snaplocate/graph.py. If the paths are absolute or have an")
        print("     extra prefix, resolve() already tries suffix matching --")
        print("     a 0 here usually means the path FIELD is wrong.")
        return 1

    pct = 100.0 * len(mapping) / len(corpus)
    if pct < 50:
        print("")
        print("WARNING: only %.0f%% of files resolved. The graph will"
              " contribute" % pct)
        print("         little. Check the path field before trusting results.")
    if not cg.community_key:
        print("")
        print("NOTE: no community/subsystem field found, so S/M/L difficulty")
        print("      will report M/unknown rather than guessing.")
    print("")
    print("OK" if pct >= 50 else "USABLE BUT WEAK")
    return 0


# -- locate -----------------------------------------------------------------
def _print_result(res, as_json=False):
    if as_json:
        print(json.dumps(res, indent=2, default=str))
        return 0 if not res.get("error") else 1

    if res.get("error"):
        print("FAILED at stage: %s" % res.get("failed_stage"))
        print("  %s" % res["error"])
        if res.get("strings_tried"):
            print("\n  strings searched for:")
            for s in res["strings_tried"]:
                print("    %r" % s)
        return 1

    iss = res.get("issue", {})
    print("=" * 68)
    print("%s/%s#%s  %s" % (iss.get("owner"), iss.get("repo"),
                            iss.get("number"), iss.get("title", "")[:40]))
    print("=" * 68)
    xr = res.get("xray", {})
    print("surface : %s" % xr.get("surface"))
    print("symptom : %s" % xr.get("symptom"))
    print("model   : %s" % xr.get("model"))
    print("variant : %s%s" % (res.get("variant"),
                              "" if res.get("ranking_used_graph")
                              else "  (graph did NOT contribute)"))
    print("")

    print("ON-SCREEN STRINGS (verbatim from the screenshot):")
    for r in xr.get("regions", []):
        files = res.get("region_files", {}).get(r["id"], [])
        print("  %-4s %-9s %s%s" % (
            r["id"], r.get("kind", ""), json.dumps(r["text"])[:46],
            "  <- SUSPECT" if r.get("suspect") else ""))
        if files:
            print("       found in: %s" % ", ".join(files[:3])
                  + (" (+%d)" % (len(files) - 3) if len(files) > 3 else ""))
        else:
            print("       found in: (nothing)")
    dropped = res.get("search", {}).get("regions_dropped", [])
    if dropped:
        print("  dropped: %s" % ", ".join(
            "%s(%s)" % (json.dumps(d["text"])[:22], d["reason"])
            for d in dropped))
    print("")

    print("FILES  (%s)" % ("reranked by model" if res.get("final_source")
                           == "rerank" else "fused ranking"))
    for i, f in enumerate(res.get("final", []), 1):
        print("  %d. %s" % (i, f["path"]))
        print("     score=%.3f (search %.3f, graph %.3f)"
              % (f.get("score", 0), f.get("seed", 0), f.get("graph", 0)))
        if f.get("reason"):
            print("     %s" % f["reason"][:100])
    print("")
    print("  full ranking:")
    for f in res.get("ranked", [])[:10]:
        print("   %2d. %-44s %.3f (s=%.3f g=%.3f)"
              % (f["rank"], f["path"][:44], f["score"], f["seed"], f["graph"]))
    print("")

    rr = res.get("rerank", {})
    if rr.get("call_path"):
        print("CALL PATH: %s" % " -> ".join(rr["call_path"]))
    if rr.get("confidence"):
        print("model confidence: %s" % rr.get("confidence"))
    print("")

    d = res.get("difficulty", {})
    print("DIFFICULTY: %s  (%s)%s" % (d.get("label"), d.get("reason"),
                                      "" if d.get("confident")
                                      else "  [not confident]"))
    b = res.get("blast_radius", {})
    if b.get("available"):
        print("BLAST RADIUS: %d files within %d hops of %s"
              % (b["count"], b["hops"], b["origin"]))
        for f in b["files"][:8]:
            print("   %s" % f)
        if b["count"] > 8:
            print("   ... +%d more" % (b["count"] - 8))
    else:
        print("BLAST RADIUS: unavailable (%s)" % b.get("note"))

    print("")
    if res.get("degraded"):
        print("DEGRADED:")
        for d_ in res["degraded"]:
            print("  - %s" % d_)
    print("timing: %s" % json.dumps(res.get("timing", {})))
    print("")
    print("NOTE: all ranking weights are UNTUNED hand-picked values.")
    return 0


def _replay_banner(res):
    rp = res.get("replay") or {}
    if not rp.get("replayed"):
        return
    print("+" + "-" * 66 + "+")
    print("|  REPLAYED FROM CACHE -- this was NOT computed just now".ljust(67) + "|")
    print(("|  recorded %s (%s ago)"
           % (rp.get("recorded_at_human"), rp.get("age_human"))).ljust(67) + "|")
    print(("|  reason: %s" % rp.get("reason", ""))[:66].ljust(67) + "|")
    print("+" + "-" * 66 + "+")
    print("")


def cmd_locate(args):
    cache_dir = args.cache_dir or cache.DEFAULT_DIR

    def replay(reason):
        hit = cache.load(cache_dir, args.issue_url)
        if not hit:
            return None
        return cache.mark_replayed(hit[0], hit[1], reason)

    if args.offline:
        res = replay("--offline given")
        if not res:
            print("--offline, but %s is not in the cache at %s"
                  % (args.issue_url, os.path.abspath(cache_dir)))
            print("Cached issues:")
            for m in cache.list_entries(cache_dir):
                print("  %s" % m.get("issue_url"))
            return 2
        _replay_banner(res)
        return _print_result(res, args.json)

    if not gemma.api_key() and not args.no_model:
        res = replay("no GEMMA_API_KEY set")
        if res:
            _replay_banner(res)
            return _print_result(res, args.json)
        print("No GEMMA_API_KEY set. SnapLocate needs the vision call to read")
        print("the screenshot; there is nothing to search for without it.")
        print("  export GEMMA_API_KEY=...")
        return 2

    res = pipeline.run(
        issue_url=args.issue_url,
        repo_root=args.repo or ".",
        graph_path=args.graph or DEFAULT_GRAPH,
        search_only=args.variant == "search-only",
        do_rerank=not args.no_rerank,
        model=args.model,
    )

    if res.get("error"):
        fallback = replay("the live run failed at stage '%s'"
                          % res.get("failed_stage"))
        if fallback:
            print("live run failed (%s); replaying the cached run instead\n"
                  % res.get("failed_stage"))
            _replay_banner(fallback)
            return _print_result(fallback, args.json)
        return _print_result(res, args.json)

    if args.record:
        try:
            image = None
            urls = res.get("image_urls") or []
            if urls:
                got = cache.load_image(cache_dir, urls[0]) \
                    or gh.download_image(urls[0])
                cache.save_image(cache_dir, urls[0], *got)
                image = got
            where = cache.save(cache_dir, args.issue_url, res, image=image)
            print("recorded for offline replay: %s\n" % where)
        except Exception as exc:                        # noqa: BLE001
            print("WARNING: could not record this run: %s\n" % exc)

    return _print_result(res, args.json)


# -- cache ------------------------------------------------------------------
def cmd_cache(args):
    cache_dir = args.cache_dir or cache.DEFAULT_DIR
    entries, problems = cache.check(cache_dir)

    print("cache: %s" % os.path.abspath(cache_dir))
    print("%d issue(s) recorded" % len(entries))
    print("")
    for m in entries:
        print("  %s" % (m.get("issue_url") or m.get("key")))
        print("     %s" % (m.get("title") or "")[:60])
        print("     recorded %s | model %s | %d files ranked | %d regions%s"
              % (m.get("recorded_at_human"), m.get("model"),
                 m.get("n_ranked") or 0, m.get("n_regions") or 0,
                 "" if m.get("image_cached") else " | NO SCREENSHOT CACHED"))
        if m.get("used_graph") is False:
            print("     note: recorded WITHOUT the graph (search-only)")
        for d in (m.get("degraded_when_recorded") or [])[:2]:
            print("     degraded when recorded: %s" % d[:60])
        print("")

    if problems:
        print("PROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  ! %s" % p)
        print("")
        print("Fix these BEFORE the demo, not during it.")
        return 1

    print("All cached entries would replay. Test it for real:")
    print("  python3 -m snaplocate.cli serve --offline --cache-dir %s"
          % cache_dir)
    print("")
    print("A replayed run is always labelled as a replay in the UI and the")
    print("CLI. Do not describe one as a live run.")
    return 0


# -- mkgraph ----------------------------------------------------------------
def cmd_mkgraph(args):
    out = args.out or DEFAULT_GRAPH
    g = mkgraph.build(args.repo or ".", out)
    print("wrote %s" % out)
    print("  nodes: %d" % len(g["nodes"]))
    print("  edges: %d" % len(g["links"]))
    print("")
    print("THIS IS THE FALLBACK BUILDER, NOT GRAPHIFY.")
    for c in g["_snaplocate_caveats"]:
        print("  - %s" % c)
    print("")
    print("If you ship with this graph, say so in the README and on the slide.")
    return 0


# -- verify-boxes -----------------------------------------------------------
VERIFY_HTML = """<!doctype html>
<meta charset="utf-8"><title>SnapLocate box check</title>
<style>
 body{background:#0d1117;color:#e6edf3;font:14px/1.5 ui-monospace,monospace;
      margin:0;padding:24px}
 h1{font-size:16px;font-weight:600}
 .wrap{position:relative;display:inline-block;border:1px solid #30363d}
 .wrap img{display:block;max-width:100%%;height:auto}
 .b{position:absolute;border:2px solid #f85149;background:rgba(248,81,73,.12);
    box-sizing:border-box}
 .b.ok{border-color:#3fb950;background:rgba(63,185,80,.12)}
 .b span{position:absolute;top:-18px;left:-2px;background:#f85149;color:#fff;
    font-size:10px;padding:0 4px;white-space:nowrap}
 .b.ok span{background:#3fb950}
 table{border-collapse:collapse;margin-top:16px;font-size:12px}
 td,th{border:1px solid #30363d;padding:4px 8px;text-align:left}
 .note{color:#8b949e;max-width:60em}
</style>
<h1>Box convention check &mdash; %(order)s, scale %(scale)d</h1>
<p class="note">Each box should sit ON the text quoted beside it. If the boxes
are rotated, mirrored or shifted, the convention is wrong: change BOX_ORDER /
BOX_SCALE in <code>snaplocate/gemma.py</code> and re-run. The web UI reads the
convention from the API, so fixing it there fixes the overlay too.</p>
<div class="wrap"><img src="data:%(mime)s;base64,%(img)s">%(boxes)s</div>
<table><tr><th>id</th><th>raw box</th><th>text</th></tr>%(rows)s</table>
"""


def cmd_verify_boxes(args):
    """Render real Gemma boxes over the real screenshot, for eyeballing."""
    import base64
    if not gemma.api_key():
        print("set GEMMA_API_KEY first")
        return 2

    src = args.image
    if src.startswith(("http://", "https://")):
        data, mime = gh.download_image(src)
    else:
        with open(src, "rb") as fh:
            data = fh.read()
        mime = gemma.guess_mime(src)

    print("sending %d bytes (%s) to %s ..."
          % (len(data), mime, args.model or config.MODEL_DEFAULT))
    try:
        xr = gemma.xray(data, mime, args.context or "", model=args.model)
    except gemma.GemmaError as exc:
        print("XRAY failed: %s" % exc)
        return 1

    print("model   : %s" % xr["model"])
    print("surface : %s" % xr["surface"])
    print("symptom : %s" % xr["symptom"])
    print("regions : %d" % len(xr["regions"]))
    print("")
    boxes, rows = [], []
    bad = 0
    for r in xr["regions"]:
        f = r.get("box_frac")
        print("  %-4s %-8s box=%-26s %s"
              % (r["id"], r.get("kind"), r.get("box"),
                 json.dumps(r["text"])[:40]))
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (r["id"], r.get("box"),
                       (r["text"] or "").replace("<", "&lt;")[:80]))
        if not f:
            bad += 1
            continue
        boxes.append(
            '<div class="b %s" style="left:%.3f%%;top:%.3f%%;width:%.3f%%;'
            'height:%.3f%%"><span>%s %s</span></div>'
            % ("ok" if not r.get("suspect") else "",
               f["left"] * 100, f["top"] * 100,
               f["width"] * 100, f["height"] * 100,
               r["id"], (r["text"] or "")[:24].replace("<", "&lt;")))

    out = args.out or "boxcheck.html"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(VERIFY_HTML % {
            "order": ",".join(gemma.BOX_ORDER), "scale": gemma.BOX_SCALE,
            "mime": mime, "img": base64.b64encode(data).decode("ascii"),
            "boxes": "".join(boxes), "rows": "".join(rows),
        })
    print("")
    if bad:
        print("%d region(s) had an unusable box." % bad)
    print("wrote %s -- OPEN IT and check the boxes sit on the text." % out)
    print("")
    print("The convention %s / 0-%d is an ASSUMPTION and is still UNVERIFIED"
          % (",".join(gemma.BOX_ORDER), gemma.BOX_SCALE))
    print("until you have looked at that file.")
    return 0


# -- eval -------------------------------------------------------------------
def cmd_eval(args):
    rows = evalh.read_csv(args.csv)
    if not rows:
        print("no usable rows in %s" % args.csv)
        return 1

    problems, counts = evalh.check_rows(rows)
    print("rows: %s" % ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    if problems:
        print("")
        print("CSV PROBLEMS (%d):" % len(problems))
        for p in problems:
            print("  ! %s" % p)
        if not args.force:
            print("")
            print("Refusing to run. These would silently corrupt the numbers.")
            print("Fix them, or pass --force to run anyway (and then do not")
            print("report the result as clean).")
            return 1
        print("")
        print("--force given: running with known-bad rows. The numbers below")
        print("are NOT reportable as a clean result.")

    if not problems:
        print("no problems found.")
    print("")
    print("NOTE: the base_commit check confirms each repo is sitting at the")
    print("      commit you DECLARED. It cannot confirm that commit is really")
    print("      the fix PR's base -- if you recorded the repo's main tip,")
    print("      this passes while the fix is already in the tree.")
    print("        gh pr view <PR> --repo owner/repo --json baseRefOid")

    if args.check:
        print("\ncheck only, not running the pipeline.")
        return 0 if not problems else 1

    if args.split == "test" and not args.i_am_done_tuning:
        print("")
        print("Running the TEST split. Confirm you are done tuning: re-run")
        print("with --i-am-done-tuning. Every look at this split costs you")
        print("the right to call it held-out.")
        return 2

    if not gemma.api_key():
        print("no GEMMA_API_KEY: cannot run the vision stage")
        return 2

    results = []
    print("\n--- snaplocate (search + graph) ---")
    m1, per1 = evalh.run_split(rows, args.split)
    results.append(m1)
    if args.ablation:
        print("\n--- search-only ablation ---")
        m2, _ = evalh.run_split(rows, args.split, search_only=True)
        results.append(m2)

    print("")
    print(evalh.format_report(results, per1))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"metrics": results, "per_issue": per1}, fh, indent=2)
        print("wrote %s" % args.out)

    if args.ablation and len(results) == 2:
        a, b = results[0], results[1]
        if a["mrr"] is not None and b["mrr"] is not None:
            delta = a["mrr"] - b["mrr"]
            print("")
            if delta > 0:
                print("The graph improved MRR by %.3f on %d issues. At this n"
                      % (delta, a["n"]))
                print("that is a direction, not a validated effect size.")
            elif delta == 0:
                print("The graph changed MRR by exactly 0. Report that: its")
                print("value here is the call path and blast radius, not")
                print("ranking.")
            else:
                print("The graph made MRR WORSE by %.3f. Report that honestly."
                      % -delta)
                print("Argue its value as the call path and blast radius, or")
                print("drop the 0.4 graph weight -- do not hide it.")
    return 0


# -- serve ------------------------------------------------------------------
def cmd_serve(args):
    from .server import serve
    serve(host=args.host, port=args.port, repo_root=args.repo or ".",
          graph_path=args.graph or DEFAULT_GRAPH,
          cache_dir=args.cache_dir or cache.DEFAULT_DIR,
          record=args.record, offline=args.offline,
          cache_first=args.cache_first)
    return 0


# -- parser -----------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="snaplocate",
        description="Screenshot in a GitHub issue -> the source files behind it.")
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("doctor", help="print the sniffed graph schema")
    d.add_argument("--graph")
    d.add_argument("--repo", default=".")
    d.set_defaults(fn=cmd_doctor)

    l = sub.add_parser("locate", help="locate files for a GitHub issue")
    l.add_argument("issue_url")
    l.add_argument("--repo", default=".")
    l.add_argument("--graph")
    l.add_argument("--model")
    l.add_argument("--variant", choices=["snaplocate", "search-only"],
                   default="snaplocate")
    l.add_argument("--no-rerank", action="store_true")
    l.add_argument("--no-model", action="store_true",
                   help="internal: skip the API-key check")
    l.add_argument("--json", action="store_true")
    l.add_argument("--record", action="store_true",
                   help="save this run for offline replay")
    l.add_argument("--offline", action="store_true",
                   help="replay the cached run; do not touch the network")
    l.add_argument("--cache-dir", help="default: ./demo-cache")
    l.set_defaults(fn=cmd_locate)

    c = sub.add_parser("cache",
                       help="list cached runs and pre-flight offline mode")
    c.add_argument("--cache-dir", help="default: ./demo-cache")
    c.set_defaults(fn=cmd_cache)

    m = sub.add_parser("mkgraph", help="FALLBACK graph builder (not Graphify)")
    m.add_argument("--repo", default=".")
    m.add_argument("--out")
    m.set_defaults(fn=cmd_mkgraph)

    v = sub.add_parser("verify-boxes",
                       help="render real Gemma boxes over a screenshot")
    v.add_argument("image", help="local path or URL")
    v.add_argument("--out")
    v.add_argument("--model")
    v.add_argument("--context", default="")
    v.set_defaults(fn=cmd_verify_boxes)

    e = sub.add_parser("eval", help="run the eval harness")
    e.add_argument("csv")
    e.add_argument("--split", choices=["tune", "test"], default="tune")
    e.add_argument("--ablation", action="store_true",
                   help="also run the search-only variant")
    e.add_argument("--check", action="store_true",
                   help="validate the CSV without running anything")
    e.add_argument("--force", action="store_true")
    e.add_argument("--i-am-done-tuning", action="store_true")
    e.add_argument("--out")
    e.set_defaults(fn=cmd_eval)

    s = sub.add_parser("serve", help="web UI on stdlib http.server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--repo", default=".")
    s.add_argument("--graph")
    s.add_argument("--record", action="store_true",
                   help="save every successful live run for offline replay")
    s.add_argument("--offline", action="store_true",
                   help="offline demo mode: replay only, never use the network")
    s.add_argument("--cache-first", action="store_true",
                   help="replay a recorded issue instantly instead of calling "
                        "the API; uncached issues still run live")
    s.add_argument("--cache-dir", help="default: ./demo-cache")
    s.set_defaults(fn=cmd_serve)

    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 1
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
