"""Stage 5: FUSE, and stage 7: difficulty / blast radius.

score = W_SEED * seed + W_GRAPH * graph   (both UNTUNED, see config.py)

Tie-break is deterministic and total, so two runs on the same inputs produce
byte-identical rankings: higher score, then higher seed, then shorter path,
then lexicographic. The last key guarantees totality -- without it, two files
with equal scores and equal path length would order by dict insertion.
"""
from . import config


def fuse(seed, graph, search_only=False):
    """Combine normalised seed and graph scores into a ranked list.

    `search_only` is the ablation: graph contributes nothing and the seed is
    used at full weight (NOT 0.6 weight -- otherwise the ablation would just be
    the same ranking with every score scaled by 0.6, which changes no order but
    makes the printed numbers incomparable to the fused run).
    """
    rows = []
    keys = set(seed) | (set() if search_only else set(graph))
    for rel in keys:
        s = seed.get(rel, 0.0)
        g = 0.0 if search_only else graph.get(rel, 0.0)
        score = s if search_only else (config.W_SEED * s + config.W_GRAPH * g)
        rows.append({
            "path": rel,
            "score": round(score, 6),
            "seed": round(s, 6),
            "graph": round(g, 6),
        })
    rows.sort(key=lambda r: (-r["score"], -r["seed"], len(r["path"]), r["path"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def difficulty(top_files, cg, repo_files):
    """S / M / L per the spec, with the under-specification stated.

    Spec: "S = 1 subsystem and <=5 neighbours; L = >=3 subsystems; M otherwise".

    UNDER-SPECIFIED: it does not say which file set the subsystems are counted
    over, nor whose neighbours. We count subsystems over the TOP 3 files (the
    set actually shown to the user) and neighbours as the 1-hop neighbours of
    the TOP 1 file. Other readings -- top 1 only, or the whole blast radius --
    would give different labels on the same repo.

    With no graph, or no community field, subsystem count is unknown. We return
    "M" with reason "unknown" rather than defaulting to "S", because calling an
    unmeasured issue Small is the failure that actually costs a contributor
    their afternoon.
    """
    top3 = [r["path"] for r in top_files[:3]]
    top1 = top_files[0]["path"] if top_files else None

    if not cg or not cg.ok or not top1:
        return {"label": "M", "reason": "no graph: subsystems unknown",
                "subsystems": None, "neighbours": None, "confident": False}

    subs = cg.subsystems_of(top3, repo_files)
    nbrs = cg.neighbours(top1, repo_files, hops=1)

    if not subs:
        return {"label": "M",
                "reason": "graph has no community/subsystem field: unknown",
                "subsystems": None, "neighbours": len(nbrs),
                "confident": False}

    n_subs, n_nbrs = len(subs), len(nbrs)
    if n_subs >= config.DIFF_L_MIN_SUBSYSTEMS:
        label, reason = "L", "%d subsystems touched" % n_subs
    elif n_subs == 1 and n_nbrs <= config.DIFF_S_MAX_NEIGHBOURS:
        label, reason = "S", "1 subsystem, %d neighbours" % n_nbrs
    else:
        label, reason = "M", "%d subsystems, %d neighbours" % (n_subs, n_nbrs)

    return {"label": label, "reason": reason, "subsystems": subs,
            "neighbours": n_nbrs, "confident": True}


def blast_radius(top_files, cg, repo_files):
    """Files within BLAST_HOPS of the top pick.

    This is a reachability set, not a prediction of what a fix will touch. It
    answers "what could this change break", which is strictly larger than
    "what will change".
    """
    if not top_files:
        return {"files": [], "count": 0, "available": False,
                "note": "no ranked files"}
    if not cg or not cg.ok:
        return {"files": [], "count": 0, "available": False,
                "note": "no graph loaded: blast radius unavailable"}
    top1 = top_files[0]["path"]
    files = cg.neighbours(top1, repo_files, hops=config.BLAST_HOPS)
    return {
        "files": files,
        "count": len(files),
        "available": True,
        "origin": top1,
        "hops": config.BLAST_HOPS,
        "note": "files reachable within %d hops of %s; upper bound on what a "
                "fix here could affect, not a prediction of the diff"
                % (config.BLAST_HOPS, top1),
    }
