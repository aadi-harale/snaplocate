#!/usr/bin/env python3
"""SnapLocate self-test. NO network, NO API key, stdlib only.

Builds a fake repo, stubs the two Gemma calls, and asserts that the file that
actually contains the on-screen strings ranks first -- plus that every
degradation path returns a result instead of raising.

    python3 scripts/selftest.py          # run
    python3 scripts/selftest.py -v       # show each check

Exit code 0 means pass. This is the check that must stay green; if you change
scoring and this goes red, the scoring changed behaviour, not just numbers.
"""
import json
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from snaplocate import config, fuse, mkgraph, pipeline, search  # noqa: E402
from snaplocate.graph import CodeGraph, sniff_key               # noqa: E402
from snaplocate.gemma import box_to_fractions                   # noqa: E402
from snaplocate.jsonx import extract_json                       # noqa: E402
from snaplocate import gh                                       # noqa: E402

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
_checks = {"pass": 0, "fail": 0}
_failures = []


def check(name, cond, detail=""):
    if cond:
        _checks["pass"] += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        _checks["fail"] += 1
        _failures.append((name, detail))
        print("  FAIL %s %s" % (name, detail))
    return bool(cond)


# ---------------------------------------------------------------------------
# The fake repo.
#
# checkout.py is the ANSWER: it holds both on-screen strings verbatim.
# payments.py is a decoy -- it imports checkout and mentions the word
# "Payment", so it should surface as a neighbour but must not outrank the file
# that actually contains the strings.
# ---------------------------------------------------------------------------
FAKE_REPO = {
    "src/checkout.py": '''
"""Checkout flow."""
from .payments import charge_card
from . import inventory

TOTAL_LABEL = "Order total"


def finalise(cart, card):
    if not inventory.in_stock(cart):
        raise ValueError("Item is no longer available")
    try:
        charge_card(card, cart.total)
    except Exception:
        # the exact string the user sees in the screenshot
        raise RuntimeError("Coudn't complete yor order")
''',
    "src/payments.py": '''
"""Card charging."""
import hashlib


def charge_card(card, amount):
    """Charge a card. Payment gateway lives behind this."""
    token = hashlib.sha256(card.encode()).hexdigest()
    return {"token": token, "amount": amount}
''',
    "src/inventory.py": '''
"""Stock checks."""


def in_stock(cart):
    return all(line.qty > 0 for line in cart.lines)
''',
    "src/ui/banner.js": '''
import { formatMoney } from "../util/money.js";

export function banner(msg) {
  const el = document.createElement("div");
  el.className = "banner banner--error";
  el.textContent = msg;
  return el;
}
''',
    "src/util/money.js": '''
export function formatMoney(cents) {
  return "$" + (cents / 100).toFixed(2);
}
''',
    "src/unrelated/admin.py": '''
"""Admin dashboard, nothing to do with checkout."""


def dashboard():
    return {"title": "Admin", "widgets": []}
''',
    # Boilerplate: a string that appears in many files must be dropped.
    "src/a1.py": "HEADER = 'Copyright 2026 Example Inc'\n",
    "src/a2.py": "HEADER = 'Copyright 2026 Example Inc'\n",
    "src/a3.py": "HEADER = 'Copyright 2026 Example Inc'\n",
}


def make_repo(root):
    for rel, body in FAKE_REPO.items():
        p = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)


# -- stubs ------------------------------------------------------------------
# Regions as Gemma would return them: the misspelled error string is verbatim
# (a "corrected" string would match nothing -- that is the point of the rule).
STUB_REGIONS = [
    {"id": "r1", "text": "Coudn't complete yor order", "kind": "error",
     "box": [420, 100, 470, 880], "suspect": True,
     "why": "error text, likely raised in source"},
    {"id": "r2", "text": "Order total", "kind": "label",
     "box": [300, 100, 330, 400], "suspect": False, "why": "static label"},
    {"id": "r3", "text": "Copyright 2026 Example Inc", "kind": "other",
     "box": [960, 100, 980, 900], "suspect": False, "why": "footer"},
    {"id": "r4", "text": "OK", "kind": "button",
     "box": [500, 400, 530, 460], "suspect": False, "why": "too short"},
]


def stub_xray(image_bytes, mime=None, context="", model=None):
    return {
        "surface": "checkout",
        "symptom": "order fails with a spelling-mangled error",
        "regions": [dict(r, box_frac=box_to_fractions(r["box"]))
                    for r in STUB_REGIONS],
        "model": "stub",
        "box_convention": {"order": ["ymin", "xmin", "ymax", "xmax"],
                           "scale": 1000, "verified": False},
        "raw_text": "(stubbed)",
    }


def stub_rerank(xr, candidates, model=None):
    allowed = [c["path"] for c in candidates]
    return {
        "top": [{"path": allowed[0], "reason": "contains the error string"}],
        "call_path": [allowed[0]],
        "confidence": "medium",
        "hallucinated_paths_dropped": [],
        "model": "stub",
        "ran": True,
    }


def stub_rerank_hallucinating(xr, candidates, model=None):
    return {
        "top": [{"path": "src/does/not/exist.py", "reason": "invented"},
                {"path": candidates[0]["path"], "reason": "real"}],
        "call_path": ["src/does/not/exist.py"],
        "confidence": "low",
        "hallucinated_paths_dropped": [],
        "model": "stub",
        "ran": True,
    }


def stub_rerank_boom(xr, candidates, model=None):
    raise RuntimeError("simulated API outage")


STUB_ISSUE = {
    "owner": "example", "repo": "shop", "number": 1,
    "url": "https://github.com/example/shop/issues/1",
    "title": "Can't check out", "body": "it's broken ![shot](http://x/y.png)",
    "state": "closed", "comments": [],
    "image_urls": ["http://x/y.png"],
}
STUB_IMAGE = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")


# ===========================================================================
def test_units():
    print("[1/10] units: scoring, json, boxes")

    # min-max edge cases
    check("minmax empty", search.minmax({}) == {})
    check("minmax single -> 1.0", search.minmax({"a": 3.0}) == {"a": 1.0})
    check("minmax all-equal -> 1.0",
          search.minmax({"a": 2.0, "b": 2.0}) == {"a": 1.0, "b": 1.0})
    mm = search.minmax({"a": 0.0, "b": 1.0, "c": 0.5})
    check("minmax spread", mm["a"] == 0.0 and mm["b"] == 1.0
          and abs(mm["c"] - 0.5) < 1e-9)
    check("minmax all-zero -> 0.0",
          search.minmax({"a": 0.0, "b": 0.0}) == {"a": 0.0, "b": 0.0})
    # REGRESSION (2026-10-22, real Gemma run): true min-max forced the weaker
    # of two genuine matches to exactly 0.0, erasing a file that contained the
    # error string verbatim. The default must never zero a real match.
    two = search.minmax({"hit": 0.576, "better": 0.886})
    check("a weaker REAL match is not zeroed by a stronger one",
          two["hit"] > 0.0, str(two))
    check("ratios preserved under the default norm",
          abs(two["hit"] - 0.576 / 0.886) < 1e-9, str(two))
    check("explicit minmax mode still available for comparison",
          search.minmax({"hit": 0.576, "better": 0.886},
                        mode="minmax")["hit"] == 0.0)

    # boxes
    f = box_to_fractions([0, 0, 500, 1000])
    check("box top-half", f and abs(f["top"]) < 1e-9
          and abs(f["height"] - 0.5) < 1e-9 and abs(f["width"] - 1.0) < 1e-9,
          str(f))
    check("box swapped min/max tolerated",
          box_to_fractions([500, 1000, 0, 0]) is not None)
    check("box degenerate rejected", box_to_fractions([10, 10, 10, 10]) is None)
    check("box junk rejected", box_to_fractions("nope") is None
          and box_to_fractions([1, 2]) is None
          and box_to_fractions(None) is None)
    check("box out-of-range clamped",
          (box_to_fractions([0, 0, 5000, 5000]) or {}).get("height") == 1.0)

    # json extraction
    check("json from fence",
          extract_json('x ```json\n{"a":1}\n``` y') == {"a": 1})
    check("json with brace in string",
          extract_json('{"a":"} not a brace"}') == {"a": "} not a brace"})
    check("json trailing comma", extract_json('{"a":[1,2,],}') == {"a": [1, 2]})
    check("json absent -> None", extract_json("nothing here") is None)
    check("json empty -> None", extract_json("") is None)

    # issue url parsing
    check("parse issue url",
          gh.parse_issue_url("https://github.com/o/r/issues/42")
          == ("o", "r", 42))
    try:
        gh.parse_issue_url("https://example.com/nope")
        check("bad url raises", False)
    except gh.GitHubError:
        check("bad url raises", True)

    imgs = gh.extract_image_urls(
        "![a](https://user-images.githubusercontent.com/1/a.png) "
        '<img src="https://x.test/b.jpg">')
    check("image urls found", len(imgs) == 2, str(imgs))


def test_search(root):
    print("[2/10] search: the right file seeds highest")
    corpus = search.load_repo(root)
    check("corpus found files", len(corpus) == len(FAKE_REPO),
          "%d of %d" % (len(corpus), len(FAKE_REPO)))

    sr = search.seed_scores(corpus, STUB_REGIONS)
    seed = sr["seed"]

    check("checkout.py seeded", "src/checkout.py" in seed, str(sorted(seed)))
    top = max(seed, key=lambda k: seed[k]) if seed else None
    check("checkout.py is top seed", top == "src/checkout.py", "got %s" % top)
    check("top seed normalised to 1.0", seed.get(top) == 1.0)

    dropped = {d["reason"] for d in sr["regions_dropped"]}
    check("short string dropped", "too_short" in dropped, str(dropped))

    # 'Copyright 2026 Example Inc' is in 3 files; MAX_DOC_FREQ is 40, so it is
    # NOT boilerplate-dropped here. Assert it was kept and that it did not win.
    kept = {i["text"] for i in sr["regions_used"].values()}
    check("copyright string kept (df below threshold)",
          "Copyright 2026 Example Inc" in kept, str(kept))
    check("boilerplate files did not outrank the answer",
          seed.get("src/a1.py", 0) < seed["src/checkout.py"])

    check("suspect boost applied to checkout.py",
          "src/checkout.py" in sr["suspect_boosted"],
          str(sr["suspect_boosted"]))

    # Boilerplate threshold really does fire when df is high enough.
    saved = config.MAX_DOC_FREQ
    config.MAX_DOC_FREQ = 2
    try:
        sr2 = search.seed_scores(corpus, STUB_REGIONS)
        reasons = {(d["reason"]) for d in sr2["regions_dropped"]}
        check("boilerplate dropped when df > threshold",
              "boilerplate" in reasons, str(sr2["regions_dropped"]))
    finally:
        config.MAX_DOC_FREQ = saved

    rfm = search.region_file_map(sr["regions_used"])
    check("region->file map has r1 -> checkout.py",
          rfm.get("r1") == ["src/checkout.py"], str(rfm.get("r1")))


def test_graph_sniffing(root):
    print("[3/10] graph: schema sniffing on unfamiliar field names")
    g = mkgraph.build(root)
    check("fallback graph has nodes", len(g["nodes"]) == len(FAKE_REPO))
    check("fallback graph has import edges", len(g["links"]) >= 3,
          "%d links" % len(g["links"]))
    check("fallback graph carries its caveats",
          any("NOT GRAPHIFY" in c for c in g["_snaplocate_caveats"]))

    srcs = {(l["source"], l["target"]) for l in g["links"]}
    check("py import edge found",
          ("src/checkout.py", "src/payments.py") in srcs, str(sorted(srcs)))
    check("js import edge found",
          ("src/ui/banner.js", "src/util/money.js") in srcs)

    tmp = os.path.join(root, "graphify-out", "graph.json")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(g, fh)
    cg = CodeGraph.load(tmp)
    check("graph loads", cg.ok, cg.reason)
    check("path key sniffed", cg.path_key in ("path", "id"), str(cg.path_key))
    check("community key sniffed", cg.community_key == "community",
          str(cg.community_key))

    # Hostile schema: nothing we listed first, {"nodes","edges"}, absolute
    # Windows-style paths, edges as 2-element lists, community under 'cluster'.
    weird = {
        "nodes": [
            {"nodeId": i, "loc": "C:\\\\proj\\\\" + rel.replace("/", "\\\\"),
             "cluster": "c%d" % (i % 2)}
            for i, rel in enumerate(sorted(FAKE_REPO))
        ],
        "edges": [[0, 1], [1, 2]],
    }
    wp = os.path.join(root, "weird.json")
    with open(wp, "w", encoding="utf-8") as fh:
        json.dump(weird, fh)
    cgw = CodeGraph.load(wp)
    check("unknown path key is NOT silently accepted as ok-with-no-paths",
          (not cgw.ok) or cgw.path_key is not None, cgw.reason)
    if cgw.ok:
        check("weird graph found a path-like field",
              cgw.path_key == "loc", str(cgw.path_key))
    else:
        check("weird graph fails loudly with a fixable message",
              "PATH_KEYS" in cgw.reason, cgw.reason)

    # A graph whose nodes are bare strings.
    bare = {"nodes": sorted(FAKE_REPO), "links": [
        {"source": "src/checkout.py", "target": "src/payments.py"}]}
    bp = os.path.join(root, "bare.json")
    with open(bp, "w", encoding="utf-8") as fh:
        json.dump(bare, fh)
    cgb = CodeGraph.load(bp)
    check("bare-string nodes load", cgb.ok, cgb.reason)
    check("bare-string nodes resolve",
          cgb.resolve(list(FAKE_REPO))[1]["exact"] == len(FAKE_REPO),
          str(cgb.resolve(list(FAKE_REPO))[1]))

    # Absolute paths in the graph must still resolve to repo-relative files.
    absg = {"nodes": [{"id": rel, "path": "/abs/root/" + rel,
                       "community": 1} for rel in sorted(FAKE_REPO)],
            "links": []}
    ap = os.path.join(root, "abs.json")
    with open(ap, "w", encoding="utf-8") as fh:
        json.dump(absg, fh)
    cga = CodeGraph.load(ap)
    _, how = cga.resolve(list(FAKE_REPO))
    check("absolute graph paths resolve by suffix",
          how["unresolved"] == 0, str(how))

    # Malformed inputs must not raise.
    broken = os.path.join(root, "broken.json")
    with open(broken, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    check("broken json degrades", CodeGraph.load(broken).ok is False)
    check("missing file degrades",
          CodeGraph.load(os.path.join(root, "nope.json")).ok is False)
    check("empty path degrades", CodeGraph.load(None).ok is False)
    check("sniff on no nodes", sniff_key([], ["path"]) == (None, []))

    return tmp


def test_fuse_determinism():
    print("[4/10] fuse: deterministic total order")
    seed = {"b.py": 1.0, "a.py": 1.0, "cc.py": 1.0, "d.py": 0.5}
    graph = {"b.py": 0.0, "a.py": 0.0, "cc.py": 0.0, "d.py": 1.0}
    r1 = fuse.fuse(seed, graph)
    r2 = fuse.fuse(dict(reversed(list(seed.items()))), graph)
    check("same inputs, same order regardless of dict order",
          [r["path"] for r in r1] == [r["path"] for r in r2],
          "%s vs %s" % ([r["path"] for r in r1], [r["path"] for r in r2]))
    order = [r["path"] for r in r1]
    # d.py has the LOWEST seed (0.5) but the only graph mass (1.0), so its
    # fused score 0.6*0.5+0.4*1.0 = 0.7 beats the others' 0.6*1.0 = 0.6.
    # That is the graph doing its job, not a tie-break bug.
    check("graph can promote a weaker-seed file above stronger seeds",
          order[0] == "d.py", str(order))
    check("the genuinely tied files break by shorter path then lexicographic",
          [p for p in order if p != "d.py"] == ["a.py", "b.py", "cc.py"],
          str(order))
    check("ranks are 1-based and dense",
          [r["rank"] for r in r1] == [1, 2, 3, 4])

    so = fuse.fuse(seed, graph, search_only=True)
    check("search-only ignores graph",
          all(r["graph"] == 0.0 for r in so))
    check("search-only score equals seed at full weight",
          so[0]["score"] == so[0]["seed"])


def test_pipeline(root, graph_file):
    print("[5/10] pipeline: end-to-end on the fake repo")

    res = pipeline.run(repo_root=root, graph_path=graph_file,
                       issue=STUB_ISSUE, image=STUB_IMAGE,
                       xray_fn=stub_xray, rerank_fn=stub_rerank)
    check("no error", not res.get("error"), str(res.get("error")))
    check("ranked non-empty", bool(res.get("ranked")))
    if res.get("ranked"):
        check("checkout.py ranks FIRST",
              res["ranked"][0]["path"] == "src/checkout.py",
              "got %s" % res["ranked"][0]["path"])
    check("graph was actually used", res.get("ranking_used_graph") is True,
          str(res.get("degraded")))
    check("final pick is checkout.py",
          res["final"][0]["path"] == "src/checkout.py",
          str([f["path"] for f in res["final"]]))
    check("difficulty produced", res["difficulty"]["label"] in "SML")
    check("blast radius available", res["blast_radius"]["available"] is True)
    check("region->file map present", "r1" in res["region_files"])
    check("timing recorded", "total" in res["timing"])
    check("weights flagged untuned", res["weights_are_untuned"] is True)

    # The graph must CHANGE something, or it is decoration.
    so = pipeline.run(repo_root=root, graph_path=graph_file,
                      issue=STUB_ISSUE, image=STUB_IMAGE,
                      xray_fn=stub_xray, rerank_fn=stub_rerank,
                      search_only=True)
    check("search-only also ranks checkout.py first",
          so["ranked"][0]["path"] == "src/checkout.py")
    full_order = [r["path"] for r in res["ranked"]]
    so_order = [r["path"] for r in so["ranked"]]
    check("graph changes the ranking (not just decoration)",
          full_order != so_order,
          "identical order: %s" % full_order[:5])
    check("search-only variant labelled", so["variant"] == "search-only")

    return res


def test_degradation(root, graph_file):
    print("[6/10] degradation: every stage fails soft")

    # no graph at all
    r = pipeline.run(repo_root=root, graph_path=None, issue=STUB_ISSUE,
                     image=STUB_IMAGE, xray_fn=stub_xray,
                     rerank_fn=stub_rerank)
    check("no graph -> still ranks", bool(r.get("ranked")))
    check("no graph -> says so", any("graph" in d for d in r["degraded"]),
          str(r["degraded"]))
    check("no graph -> difficulty not confidently S",
          r["difficulty"]["confident"] is False)
    check("no graph -> blast radius marked unavailable",
          r["blast_radius"]["available"] is False)

    # graph that resolves to nothing
    far = os.path.join(root, "far.json")
    with open(far, "w", encoding="utf-8") as fh:
        json.dump({"nodes": [{"id": "x", "path": "totally/other/thing.py"}],
                   "links": []}, fh)
    r = pipeline.run(repo_root=root, graph_path=far, issue=STUB_ISSUE,
                     image=STUB_IMAGE, xray_fn=stub_xray,
                     rerank_fn=stub_rerank)
    check("unresolvable graph -> still ranks", bool(r.get("ranked")))
    check("unresolvable graph -> says so",
          any("resolved" in d or "no usable" in d for d in r["degraded"]),
          str(r["degraded"]))

    # rerank blows up
    r = pipeline.run(repo_root=root, graph_path=graph_file, issue=STUB_ISSUE,
                     image=STUB_IMAGE, xray_fn=stub_xray,
                     rerank_fn=stub_rerank_boom)
    check("rerank failure -> still ranks", bool(r.get("ranked")))
    check("rerank failure -> no error field", not r.get("error"))
    check("rerank failure -> reported",
          any("rerank failed" in d for d in r["degraded"]), str(r["degraded"]))
    check("rerank failure -> falls back to fusion",
          r["final_source"] == "fusion")

    # rerank invents a path
    r = pipeline.run(repo_root=root, graph_path=graph_file, issue=STUB_ISSUE,
                     image=STUB_IMAGE, xray_fn=stub_xray,
                     rerank_fn=stub_rerank_hallucinating)
    paths = [f["path"] for f in r["final"]]
    check("invented path never reaches the output",
          "src/does/not/exist.py" not in paths, str(paths))

    # xray blows up
    def boom(*a, **k):
        raise RuntimeError("simulated vision outage")
    r = pipeline.run(repo_root=root, graph_path=graph_file, issue=STUB_ISSUE,
                     image=STUB_IMAGE, xray_fn=boom, rerank_fn=stub_rerank)
    check("xray failure -> clean error, no traceback",
          r.get("failed_stage") == "xray" and "XRAY failed" in r["error"],
          str(r.get("error")))

    # xray returns nothing readable
    r = pipeline.run(repo_root=root, graph_path=graph_file, issue=STUB_ISSUE,
                     image=STUB_IMAGE,
                     xray_fn=lambda *a, **k: dict(stub_xray(b""), regions=[]),
                     rerank_fn=stub_rerank)
    check("empty regions -> clean error", r.get("failed_stage") == "xray_empty")

    # strings that match nothing in the repo
    def xray_nomatch(*a, **k):
        return dict(stub_xray(b""), regions=[
            {"id": "r1", "text": "zzzz not in this repo at all zzzz",
             "kind": "error", "box": [0, 0, 10, 10], "suspect": True,
             "why": "x", "box_frac": None}])
    r = pipeline.run(repo_root=root, graph_path=graph_file, issue=STUB_ISSUE,
                     image=STUB_IMAGE, xray_fn=xray_nomatch,
                     rerank_fn=stub_rerank)
    check("no string matches -> clean error naming the cause",
          r.get("failed_stage") == "search_empty", str(r.get("error")))
    check("no string matches -> reports what it tried",
          bool(r.get("strings_tried")))

    # issue with no image
    r = pipeline.run(repo_root=root, graph_path=graph_file,
                     issue=dict(STUB_ISSUE, image_urls=[]),
                     xray_fn=stub_xray, rerank_fn=stub_rerank)
    check("no image -> clean error", r.get("failed_stage") == "fetch_image")

    # wrong repo path
    r = pipeline.run(repo_root=os.path.join(root, "nonexistent"),
                     graph_path=graph_file, issue=STUB_ISSUE,
                     image=STUB_IMAGE, xray_fn=stub_xray,
                     rerank_fn=stub_rerank)
    check("empty repo -> clean error", r.get("failed_stage") == "search")

    # rerank disabled entirely (the no-API-key path)
    r = pipeline.run(repo_root=root, graph_path=graph_file, issue=STUB_ISSUE,
                     image=STUB_IMAGE, xray_fn=stub_xray, do_rerank=False)
    check("rerank off -> still a full result",
          bool(r.get("ranked")) and r["final_source"] == "fusion")


def test_api_budget():
    """A model call must give up on a wall clock, not hang forever.

    MEASURED 2026-10-22: a real rerank ran 3127s before failing, because
    urlopen's timeout is per socket operation, not total. No network is used
    here -- we point the call at a socket that accepts and then says nothing.
    """
    print("[7/10] api budget: a stalled call gives up instead of hanging")
    import socket as _s, threading as _t, time as _time
    from snaplocate import config, gemma

    srv = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    held = []

    def accept_and_stall():
        try:
            while True:
                c, _ = srv.accept()
                held.append(c)      # accept, never reply
        except OSError:
            pass
    _t.Thread(target=accept_and_stall, daemon=True).start()

    old_base, old_to = config.API_BASE, config.API_TIMEOUT
    config.API_BASE = "http://127.0.0.1:%d/v1beta/models" % port
    config.API_TIMEOUT = 30          # deliberately longer than the budget
    try:
        t0 = _time.monotonic()
        try:
            gemma.call_model([{"text": "hi"}], key="test-key", retries=1,
                             budget=3)
            check("stalled call raises", False, "it returned")
        except gemma.GemmaError:
            check("stalled call raises", True)
        took = _time.monotonic() - t0
        check("gave up within the budget, did not hang",
              took < 12, "took %.1fs" % took)
    finally:
        config.API_BASE, config.API_TIMEOUT = old_base, old_to
        for c in held:
            try:
                c.close()
            except OSError:
                pass
        srv.close()


def test_cache(root, graph_file):
    """Offline demo mode: record, replay, and never lie about which it was."""
    print("[8/10] cache: offline replay is labelled as a replay")
    from snaplocate import cache

    cd = tempfile.mkdtemp(prefix="snaplocate-cache-")
    try:
        res = pipeline.run(repo_root=root, graph_path=graph_file,
                           issue=STUB_ISSUE, image=STUB_IMAGE,
                           xray_fn=stub_xray, rerank_fn=stub_rerank)
        url = STUB_ISSUE["url"]

        check("nothing cached yet", cache.load(cd, url) is None)
        cache.save(cd, url, res, image=STUB_IMAGE)
        check("entry saved", cache.has(cd, url))

        hit = cache.load(cd, url)
        check("round-trips", hit is not None)
        cached, meta = hit
        check("cached ranking matches the live one",
              [r["path"] for r in cached["ranked"]]
              == [r["path"] for r in res["ranked"]])
        check("meta records the model", meta.get("model") == "stub")

        img = cache.load_image(cd, STUB_ISSUE["image_urls"][0])
        check("screenshot cached", img is not None and img[0] == STUB_IMAGE[0])

        rp = cache.mark_replayed(cached, meta, "wifi died")
        check("replay flag set", rp["replay"]["replayed"] is True)
        check("replay carries the recording time",
              bool(rp["replay"]["recorded_at_human"]))
        check("replay reason carried", rp["replay"]["reason"] == "wifi died")
        check("replay is announced in degraded[0]",
              rp["degraded"][0].startswith("REPLAYED FROM CACHE"),
              str(rp["degraded"][:1]))
        check("marking does not mutate the cached original",
              "replay" not in cached)
        check("the ranking itself is unchanged by marking",
              [r["path"] for r in rp["ranked"]]
              == [r["path"] for r in cached["ranked"]])

        # A failed run must never be cached: replaying an error on stage is
        # just a second way to fail.
        bad_res = pipeline.run(repo_root=root, graph_path=graph_file,
                               issue=dict(STUB_ISSUE, image_urls=[]),
                               xray_fn=stub_xray, rerank_fn=stub_rerank)
        check("the failed run really did fail", bool(bad_res.get("error")))
        try:
            cache.save(cd, "https://github.com/x/y/issues/9", bad_res)
            check("refuses to cache a failed run", False, "it saved one")
        except ValueError:
            check("refuses to cache a failed run", True)

        entries = cache.list_entries(cd)
        check("list_entries finds it", len(entries) == 1, str(len(entries)))
        check("list_entries reports the image is cached",
              entries[0]["image_cached"] is True)

        _, problems = cache.check(cd)
        check("check() passes on a good cache", not problems, str(problems))

        empty = tempfile.mkdtemp(prefix="snaplocate-cache-empty-")
        try:
            _, probs = cache.check(empty)
            check("check() flags an empty cache", bool(probs))
        finally:
            shutil.rmtree(empty, ignore_errors=True)

        check("missing entry returns None, does not raise",
              cache.load(cd, "https://github.com/no/such/issues/1") is None)
        check("missing image returns None",
              cache.load_image(cd, "http://nope.test/x.png") is None)

        # A corrupt entry must degrade, not crash.
        ed = cache._entry_dir(cd, url)
        with open(os.path.join(ed, "result.json"), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        check("corrupt entry loads as None", cache.load(cd, url) is None)
    finally:
        shutil.rmtree(cd, ignore_errors=True)


def test_setup_demo_ref_resolution():
    """setup_demo must never silently check out the default branch.

    Uses a LOCAL git repo; no network. Skipped if git is unavailable.
    """
    print("[9/10] setup_demo: a bad ref must fail, not fall back to main")
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("git"):
        check("git present (test skipped without it)", True, "(skipped)")
        return

    sys.path.insert(0, HERE)
    import setup_demo

    td = tempfile.mkdtemp(prefix="snaplocate-git-")
    try:
        def g(*a):
            return _sp.run(["git", "-C", td] + list(a), capture_output=True,
                           text=True)
        _sp.run(["git", "init", "-q", td], capture_output=True)
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        with open(os.path.join(td, "a.py"), "w") as fh:
            fh.write("x = 1\n")
        g("add", "-A")
        g("commit", "-qm", "one")
        real = g("rev-parse", "HEAD").stdout.strip()

        got = setup_demo._resolve_ref(td, real)
        check("a real sha resolves to itself", got == real,
              "%s vs %s" % (got[:10], real[:10]))

        # The bug: a ref that does not exist must NOT silently become the
        # default branch tip via FETCH_HEAD.
        try:
            bogus = setup_demo._resolve_ref(td, "0" * 40, fetch_head_ok=False)
            check("nonexistent sha raises instead of resolving", False,
                  "resolved to %s" % bogus[:10])
        except setup_demo.SetupError as exc:
            check("nonexistent sha raises instead of resolving", True)
            check("the error explains why it refused to fall back",
                  "already contain the fix" in str(exc), str(exc)[:90])

        try:
            setup_demo._resolve_ref(td, "no-such-branch", fetch_head_ok=False)
            check("nonexistent branch name raises", False)
        except setup_demo.SetupError:
            check("nonexistent branch name raises", True)

        check("_looks_like_sha discriminates",
              setup_demo._looks_like_sha("8c211a96") and
              not setup_demo._looks_like_sha("v2.31.0") and
              not setup_demo._looks_like_sha("main"))
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_no_network():
    print("[10/10] isolation: no API key needed, no socket opened")
    check("no API key set is fine",
          not os.environ.get("GEMMA_API_KEY") or True)

    import socket
    orig = socket.socket.connect
    opened = []

    def spy(self, addr):
        opened.append(addr)
        raise AssertionError("self-test opened a network connection to %r"
                             % (addr,))
    socket.socket.connect = spy
    try:
        with tempfile.TemporaryDirectory() as td:
            make_repo(td)
            g = os.path.join(td, "g.json")
            mkgraph.build(td, g)
            r = pipeline.run(repo_root=td, graph_path=g, issue=STUB_ISSUE,
                             image=STUB_IMAGE, xray_fn=stub_xray,
                             rerank_fn=stub_rerank)
        check("full pipeline runs with sockets blocked", bool(r.get("ranked")))
        check("no connection attempted", not opened, str(opened))
    finally:
        socket.socket.connect = orig


def main():
    print("SnapLocate self-test (stdlib only, no network, no API key)")
    print("=" * 62)
    root = tempfile.mkdtemp(prefix="snaplocate-selftest-")
    try:
        make_repo(root)
        test_units()
        test_search(root)
        graph_file = test_graph_sniffing(root)
        test_fuse_determinism()
        test_pipeline(root, graph_file)
        test_degradation(root, graph_file)
        test_api_budget()
        test_cache(root, graph_file)
        test_setup_demo_ref_resolution()
        test_no_network()
    except Exception:
        print("\nUNCAUGHT EXCEPTION -- this is a failure:")
        traceback.print_exc()
        _checks["fail"] += 1
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("=" * 62)
    total = _checks["pass"] + _checks["fail"]
    if _checks["fail"]:
        print("FAILED: %d of %d checks failed" % (_checks["fail"], total))
        for name, detail in _failures:
            print("  - %s %s" % (name, detail))
        return 1
    print("PASS: %d checks" % total)
    print("\nNote: this proves the plumbing and the scoring, with Gemma")
    print("stubbed. It says NOTHING about real-screenshot accuracy or whether")
    print("the box convention is right -- see `cli verify-boxes` for that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
