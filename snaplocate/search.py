"""Stage 3: SEARCH -- find region strings in the repo and score files.

Scoring, per the spec:
    weight    = min(len(s), 40) / 40
    exactness = 1.0 exact | 0.6 case-insensitive | 0.4 whitespace-normalised
    rarity    = 1 / (1 + log(1 + files_containing_s))
    seed(f)   = sum over matched strings of weight * exactness * rarity
then min-max normalised to 0..1.

All weights come from config.py and are UNTUNED.
"""
import math
import os
import re

from . import config

_WS = re.compile(r"\s+")


def _wsnorm(s):
    return _WS.sub(" ", s).strip()


def iter_source_files(repo_root):
    """Yield repo-relative posix paths of scannable source files."""
    repo_root = os.path.abspath(repo_root)
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in config.SKIP_DIRS and not d.startswith(".")
        ]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in config.SOURCE_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > config.MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(full, repo_root).replace(os.sep, "/")
            yield rel, full


def load_repo(repo_root):
    """Read every source file once. Returns {rel: {raw, lower, ws}}.

    Undecodable bytes are replaced rather than skipped -- a file with one bad
    byte should still be searchable.
    """
    out = {}
    for rel, full in iter_source_files(repo_root):
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        out[rel] = {"raw": raw, "lower": raw.lower(), "ws": _wsnorm(raw)}
    return out


def match_strings(corpus, regions):
    """Match each region's text against the corpus.

    Returns (per_region, dropped) where per_region maps region id to
    {"text", "suspect", "matches": {rel: exactness}, "df", "weight", "rarity"}
    and `dropped` explains anything we refused to use.
    """
    per_region = {}
    dropped = []

    for idx, reg in enumerate(regions):
        rid = str(reg.get("id", idx))
        text = (reg.get("text") or "")
        probe = text.strip()
        if len(probe) < config.MIN_STRING_LEN:
            dropped.append({"id": rid, "text": text, "reason": "too_short"})
            continue

        probe_l = probe.lower()
        probe_w = _wsnorm(probe)
        matches = {}
        for rel, forms in corpus.items():
            if probe in forms["raw"]:
                matches[rel] = config.EXACTNESS["exact"]
            elif probe_l in forms["lower"]:
                matches[rel] = config.EXACTNESS["icase"]
            elif probe_w and probe_w in forms["ws"]:
                matches[rel] = config.EXACTNESS["wsnorm"]

        df = len(matches)
        if df == 0:
            dropped.append({"id": rid, "text": text, "reason": "no_match"})
            continue
        if df > config.MAX_DOC_FREQ:
            dropped.append({"id": rid, "text": text, "reason": "boilerplate",
                            "df": df})
            continue

        per_region[rid] = {
            "text": text,
            "suspect": bool(reg.get("suspect")),
            "kind": reg.get("kind"),
            "why": reg.get("why"),
            "box": reg.get("box"),
            "matches": matches,
            "df": df,
            "weight": min(len(probe), config.WEIGHT_LEN_CAP) / config.WEIGHT_LEN_CAP,
            "rarity": 1.0 / (1.0 + math.log(1.0 + df)),
        }
    return per_region, dropped


def minmax(scores, mode=None):
    """Normalise a {key: float} to 0..1.

    MEASURED PROBLEM WITH TRUE MIN-MAX (2026-10-22, real Gemma run on
    demo-repo): min-max maps the LOWEST scorer to exactly 0.0, always. With
    two matched files the raw seeds were

        src/payments.py  0.886   -> 1.0
        src/checkout.py  0.576   -> 0.0     <-- exact match, scored zero

    checkout.py contains, verbatim, the error string the user screenshotted.
    Min-max erased it: the fused score became 0.6*0.0 + 0.4*graph, and the UI
    showed "search 0.000" next to a file that matched perfectly. The fewer
    files match, the worse this gets -- and few matches is the normal case for
    a distinctive error string, i.e. exactly when the tool should work best.

    So the default is "max": divide by the maximum. Ratios are preserved
    (checkout.py becomes 0.650, not 0.0) and a real match can never be
    annihilated by the existence of a better one.

    mode="minmax" restores the spec's original behaviour for comparison. The
    spec said min-max; this is a deliberate, measured deviation from it, not
    an oversight.
    """
    mode = mode or config.SEED_NORM
    if not scores:
        return {}
    vals = list(scores.values())
    lo, hi = min(vals), max(vals)
    if hi <= 0:
        return {k: 0.0 for k in scores}
    if mode == "minmax":
        if hi == lo:
            return {k: 1.0 for k in scores}
        return {k: (v - lo) / (hi - lo) for k, v in scores.items()}
    return {k: v / hi for k, v in scores.items()}


def seed_scores(corpus, regions):
    """Run stage 3. Returns a dict with normalised seeds and the evidence."""
    per_region, dropped = match_strings(corpus, regions)

    raw = {}
    evidence = {}
    suspect_files = set()

    for rid, info in per_region.items():
        contrib_base = info["weight"] * info["rarity"]
        for rel, exactness in info["matches"].items():
            contrib = contrib_base * exactness
            raw[rel] = raw.get(rel, 0.0) + contrib
            evidence.setdefault(rel, []).append({
                "region": rid,
                "text": info["text"],
                "exactness": exactness,
                "df": info["df"],
                "contribution": round(contrib, 6),
            })
            if info["suspect"]:
                suspect_files.add(rel)

    # Suspect boost.
    #
    # UNDER-SPECIFIED IN THE SPEC: "files matching a suspect=true region get a
    # 1.5x boost" does not say whether a file hit by three suspect regions gets
    # 1.5x or 3.375x. We apply it ONCE per file, so it stays a flat
    # "this file is implicated" signal and cannot compound into dominating the
    # ranking. Compounding is the other defensible reading.
    boosted = set()
    for rel in suspect_files:
        if rel in raw:
            raw[rel] *= config.SUSPECT_BOOST
            boosted.add(rel)

    return {
        "seed": minmax(raw),
        "seed_raw": raw,
        "evidence": evidence,
        "regions_used": per_region,
        "regions_dropped": dropped,
        "suspect_boosted": sorted(boosted),
        "files_scanned": len(corpus),
    }


def region_file_map(per_region):
    """Stage 7: region id -> files containing that region's string."""
    return {rid: sorted(info["matches"].keys())
            for rid, info in per_region.items()}
