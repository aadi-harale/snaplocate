"""Eval harness: Top-1, Top-3, MRR, median latency, plus a search-only ablation.

CSV columns (see data/issues.csv):
    split,issue_url,repo_path,base_commit,gold_files,notes

  split       "tune" or "test". NEVER report tune numbers as results.
  gold_files  ';'-separated repo-relative source files the fix PR changed,
              excluding tests/docs/lockfiles/snapshots.
  base_commit the PR's BASE commit. The repo at repo_path must be checked out
              at this commit, NOT at HEAD -- on HEAD the fix is already in and
              the eval measures nothing. `check` verifies this for you.

Metrics are defined per issue over `gold_files`:
  Top-1  1 if the rank-1 file is in gold_files
  Top-3  1 if ANY of the top 3 is in gold_files
  MRR    1/rank of the FIRST gold file in the ranking, else 0
These are "did we point at a file the fix touched", not "did we find all of
them". Recall over multi-file fixes is not measured; with 5 test issues it
could not be measured meaningfully anyway.
"""
import csv
import os
import statistics
import subprocess

from . import config, pipeline


def _norm(p):
    return str(p).replace("\\", "/").strip().lstrip("./").lower()


def is_source_gold(path):
    """Would this path be allowed in gold_files?

    Mirrors the exclusion rule so `check` can flag a row that smuggled a test
    file into gold_files -- which would silently inflate the numbers.
    """
    n = _norm(path)
    base = n.rsplit("/", 1)[-1]
    if base in {x.lower() for x in config.NON_SOURCE_NAMES}:
        return False, "lockfile"
    parts = n.split("/")
    for hint in config.NON_SOURCE_HINTS:
        if hint in parts:
            return False, "in a %s/ directory" % hint
    if base.startswith("test_") or base.endswith(("_test.py", ".test.js",
                                                  ".test.ts", ".spec.js",
                                                  ".spec.ts", ".snap")):
        return False, "test or snapshot file"
    ext = os.path.splitext(base)[1]
    if ext and ext not in config.SOURCE_EXTS:
        return False, "extension %s is not a source extension" % ext
    if n.endswith(".md") or n.endswith(".rst") or n.endswith(".txt"):
        return False, "documentation"
    return True, ""


def read_csv(path):
    """Read the issue CSV, skipping blank and #-commented LINES.

    Comments are filtered at the line level, before the CSV parser sees them:
    a commented line containing a comma would otherwise be split across columns
    and show up as a malformed row rather than being ignored.
    """
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        raw = fh.read().splitlines()

    kept = []                       # (original_line_number, text)
    for i, line in enumerate(raw, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        kept.append((i, line))
    if not kept:
        return rows

    body = [kept[0][1]] + [t for _, t in kept[1:]]
    linenos = [n for n, _ in kept[1:]]

    for idx, row in enumerate(csv.DictReader(body)):
        url = (row.get("issue_url") or "").strip()
        if not url:
            continue
        rows.append({
            "line": linenos[idx] if idx < len(linenos) else -1,
            "split": (row.get("split") or "").strip().lower(),
            "issue_url": url,
            "repo_path": (row.get("repo_path") or "").strip(),
            "base_commit": (row.get("base_commit") or "").strip(),
            "gold_files": [g.strip() for g in
                           (row.get("gold_files") or "").split(";")
                           if g.strip()],
            "notes": (row.get("notes") or "").strip(),
        })
    return rows


def git_head(repo_path):
    try:
        out = subprocess.run(["git", "-C", repo_path, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def check_rows(rows):
    """Validate the CSV before burning API calls on it. Returns problems.

    WHAT THE base_commit CHECK CAN AND CANNOT DO:
    it confirms the checkout is sitting at the commit you DECLARED. It cannot
    confirm that commit is really the fix PR's base. If you record the repo's
    main tip as base_commit and clone there, both agree and this passes while
    you evaluate on code that already contains the fix. Getting base_commit
    right is on you:
        gh pr view <PR> --repo owner/repo --json baseRefOid
    """
    problems = []
    seen = set()
    counts = {}
    for r in rows:
        counts[r["split"]] = counts.get(r["split"], 0) + 1
        tag = "line %d (%s)" % (r["line"], r["issue_url"])
        if r["split"] not in ("tune", "test"):
            problems.append("%s: split must be 'tune' or 'test', got %r"
                            % (tag, r["split"]))
        if r["issue_url"] in seen:
            problems.append("%s: duplicate issue_url" % tag)
        seen.add(r["issue_url"])
        if not r["gold_files"]:
            problems.append("%s: gold_files is empty" % tag)
        if not r["repo_path"] or not os.path.isdir(r["repo_path"]):
            problems.append("%s: repo_path not a directory: %r"
                            % (tag, r["repo_path"]))
            continue
        for g in r["gold_files"]:
            ok, why = is_source_gold(g)
            if not ok:
                problems.append("%s: gold file %r should be excluded (%s)"
                                % (tag, g, why))
            if not os.path.exists(os.path.join(r["repo_path"],
                                               g.replace("/", os.sep))):
                problems.append("%s: gold file %r does not exist at "
                                "repo_path -- wrong commit checked out?"
                                % (tag, g))
        if not r["base_commit"]:
            problems.append("%s: base_commit empty -- cannot confirm you are "
                            "not evaluating on HEAD" % tag)
        else:
            head = git_head(r["repo_path"])
            if head and not head.startswith(r["base_commit"][:7]):
                problems.append(
                    "%s: repo is at %s but base_commit is %s. RUN AGAINST THE "
                    "BASE COMMIT -- on HEAD the fix is already present."
                    % (tag, head[:10], r["base_commit"][:10]))
    return problems, counts


def score_one(result, gold):
    """Top-1 / Top-3 / MRR for one issue."""
    goldn = {_norm(g) for g in gold}
    ranked = [_norm(r["path"]) for r in result.get("ranked", [])]
    if not ranked:
        return {"top1": 0, "top3": 0, "mrr": 0.0, "first_gold_rank": None,
                "ranked_n": 0}
    rr = 0.0
    first = None
    for i, p in enumerate(ranked, 1):
        if p in goldn:
            rr = 1.0 / i
            first = i
            break
    return {
        "top1": 1 if ranked[0] in goldn else 0,
        "top3": 1 if goldn & set(ranked[:3]) else 0,
        "mrr": rr,
        "first_gold_rank": first,
        "ranked_n": len(ranked),
    }


def run_split(rows, split, *, search_only=False, do_rerank=True,
              xray_fn=None, rerank_fn=None, graph_for=None, verbose=True):
    """Run every row in `split`. Returns (metrics, per_issue)."""
    sel = [r for r in rows if r["split"] == split]
    per = []
    for r in sel:
        gpath = graph_for(r) if graph_for else os.path.join(
            r["repo_path"], "graphify-out", "graph.json")
        res = pipeline.run(
            issue_url=r["issue_url"], repo_root=r["repo_path"],
            graph_path=gpath, search_only=search_only, do_rerank=do_rerank,
            xray_fn=xray_fn, rerank_fn=rerank_fn,
        )
        entry = {
            "issue_url": r["issue_url"],
            "gold_files": r["gold_files"],
            "error": res.get("error"),
            "failed_stage": res.get("failed_stage"),
            "degraded": res.get("degraded", []),
            "latency": res.get("timing", {}).get("total"),
            "used_graph": res.get("ranking_used_graph"),
            "top_paths": [x["path"] for x in res.get("ranked", [])[:5]],
        }
        if res.get("error"):
            # A failed issue counts as 0, not as absent. Dropping failures
            # would mean reporting accuracy only over the issues that worked.
            entry.update({"top1": 0, "top3": 0, "mrr": 0.0,
                          "first_gold_rank": None, "counted": True})
        else:
            entry.update(score_one(res, r["gold_files"]))
            entry["counted"] = True
        per.append(entry)
        if verbose:
            mark = "ERR" if entry.get("error") else (
                "T1" if entry["top1"] else ("T3" if entry["top3"] else "--"))
            print("  [%s] %s  mrr=%.3f  %s" % (
                mark, r["issue_url"][-40:], entry["mrr"],
                entry.get("error", "")[:60] or ""))

    n = len(per)
    lats = [p["latency"] for p in per if p.get("latency")]
    metrics = {
        "split": split,
        "variant": "search-only" if search_only else "snaplocate",
        "n": n,
        "top1": round(sum(p["top1"] for p in per) / n, 4) if n else None,
        "top3": round(sum(p["top3"] for p in per) / n, 4) if n else None,
        "mrr": round(sum(p["mrr"] for p in per) / n, 4) if n else None,
        "median_latency_s": round(statistics.median(lats), 2) if lats else None,
        "errors": sum(1 for p in per if p.get("error")),
        "used_graph_n": sum(1 for p in per if p.get("used_graph")),
    }
    return metrics, per


def format_report(metrics_list, per_issue=None):
    """Plain-text report with the honesty caveats attached, not optional."""
    lines = []
    lines.append("=" * 68)
    lines.append("SnapLocate eval")
    lines.append("=" * 68)
    hdr = "%-14s %-6s %4s  %6s  %6s  %6s  %8s  %4s" % (
        "variant", "split", "n", "Top-1", "Top-3", "MRR", "med lat", "err")
    lines.append(hdr)
    lines.append("-" * 68)
    for m in metrics_list:
        lines.append("%-14s %-6s %4s  %6s  %6s  %6s  %8s  %4s" % (
            m["variant"], m["split"], m["n"],
            "-" if m["top1"] is None else "%.2f" % m["top1"],
            "-" if m["top3"] is None else "%.2f" % m["top3"],
            "-" if m["mrr"] is None else "%.3f" % m["mrr"],
            "-" if m["median_latency_s"] is None
            else "%.1fs" % m["median_latency_s"],
            m["errors"]))
    lines.append("-" * 68)

    ns = {m["n"] for m in metrics_list}
    n = max(ns) if ns else 0
    lines.append("")
    lines.append("READ THIS BEFORE QUOTING ANY NUMBER ABOVE:")
    lines.append("  * n = %d issues. At this size one issue moves Top-1 by"
                 % n)
    if n:
        lines.append("    %.0f percentage points, so these are indicative, not"
                     % (100.0 / n))
        lines.append("    a measurement. No confidence interval is reported")
        lines.append("    because none would be meaningful at n=%d." % n)
    lines.append("  * NOT comparable to MM-IssueLoc, SWE-bench M or any")
    lines.append("    published benchmark: different issues, different repos,")
    lines.append("    different gold definition, and %d-vs-thousands of" % n)
    lines.append("    instances. Do not put them on the same axis.")
    lines.append("  * All weights are UNTUNED hand-picked values"
                 " (snaplocate/config.py).")
    lines.append("  * An issue that errored counts as 0, it is not dropped.")
    lines.append("")

    if per_issue:
        graphn = sum(1 for p in per_issue if p.get("used_graph"))
        if graphn < len(per_issue):
            lines.append("  ! the graph was used on only %d of %d issues; on"
                         % (graphn, len(per_issue)))
            lines.append("    the rest the 'snaplocate' column IS search-only.")
            lines.append("")
    return "\n".join(lines)
