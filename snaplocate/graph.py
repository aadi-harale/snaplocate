"""Stage 4: GRAPH -- load graphify-out/graph.json and propagate scores.

The field names in graph.json are NOT verified. Nothing here hard-codes a
schema: we SNIFF it by scoring candidate keys on how often their values look
like file paths, and `doctor` prints what we picked so a mismatch is obvious in
two seconds instead of showing up as silently-zero graph scores.

Accepts NetworkX node-link ({"nodes","links"}) and {"nodes","edges"}.
"""
import json
import os
import re

from . import config
from .search import minmax

# Candidate keys for the file path on a node. Order is a tie-break hint only --
# the winner is decided by how path-like the values are, not by this order.
PATH_KEYS = [
    # `source_file` is what GRAPHIFY actually uses. Its nodes are SYMBOLS
    # (functions, classes), so `id` is like "src_payments_paymentdeclined"
    # and `label` is "charge_card()" -- neither is a path. Verified against a
    # real graphify 0.9.93 build on 2026-10-22; without this key the sniffer
    # picked `label` at quality 0.23 and resolved files only by accident.
    "source_file", "sourceFile",
    "path", "file", "file_path", "filepath", "filename", "file_name",
    "rel_path", "relpath", "relative_path", "source", "src", "location",
    "uri", "url", "module", "name", "label", "id",
]

# Below this, the chosen path field is probably not a path at all.
PATH_QUALITY_WARN = 0.5

# Candidate keys for the community / subsystem / cluster id.
COMMUNITY_KEYS = [
    "community", "cluster", "module", "group", "subsystem", "component",
    "partition", "leiden", "community_id", "cluster_id", "modularity_class",
]

EDGE_SRC_KEYS = ["source", "src", "from", "u", "start", "tail", "a"]
EDGE_DST_KEYS = ["target", "dst", "to", "v", "end", "head", "b"]

_PATHISH_EXT = re.compile(
    r"\.(py|js|jsx|ts|tsx|mjs|cjs|vue|svelte|go|rs|rb|java|kt|cs|php|c|h|cc|"
    r"cpp|hpp|m|swift|scala|ex|exs|dart|html|htm|css|scss|sass|less)$",
    re.IGNORECASE,
)


def looks_like_path(v):
    """Heuristic: does this value look like a file path?

    An extension alone is enough (a flat repo has 'main.py' with no slash); a
    separator alone is enough ('src/app/thing'). Having both scores highest.
    """
    if not isinstance(v, str) or not v.strip():
        return 0.0
    s = v.strip()
    if len(s) > 400:
        return 0.0
    score = 0.0
    if _PATHISH_EXT.search(s):
        score += 0.7
    if "/" in s or "\\" in s:
        score += 0.3
    return min(score, 1.0)


def _normalise_nodes(raw_nodes):
    """Nodes may be dicts, or bare strings/ints. Return a list of dicts."""
    out = []
    for n in raw_nodes:
        if isinstance(n, dict):
            out.append(n)
        else:
            # A bare value is both the id and the path candidate.
            out.append({"id": n, "_bare": True})
    return out


def sniff_key(nodes, candidates, scorer=looks_like_path):
    """Pick the key whose values most often satisfy `scorer`.

    Returns (best_key, report) where report lists every candidate's score so
    `doctor` can show the runner-up -- a close second usually means the graph
    has two path-like fields and we may have picked the wrong one.
    """
    report = []
    if not nodes:
        return None, report
    for key in candidates:
        present = 0
        total = 0.0
        for n in nodes:
            if key in n:
                present += 1
                total += scorer(n.get(key))
        if present == 0:
            continue
        coverage = present / len(nodes)
        quality = total / present
        report.append({
            "key": key,
            "coverage": round(coverage, 3),
            "quality": round(quality, 3),
            "score": round(coverage * quality, 4),
        })
    report.sort(key=lambda r: (-r["score"], r["key"]))
    best = report[0]["key"] if report and report[0]["score"] > 0 else None
    return best, report


def sniff_community_key(nodes):
    """Community ids are short scalars that repeat. Score on that shape."""
    def scorer(v):
        if isinstance(v, bool) or v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return 1.0
        if isinstance(v, str) and 0 < len(v) <= 80 and not looks_like_path(v):
            return 0.6
        return 0.0

    report = []
    if not nodes:
        return None, report
    for key in COMMUNITY_KEYS:
        vals = [n[key] for n in nodes if key in n]
        if not vals:
            continue
        ok = sum(scorer(v) for v in vals) / len(vals)
        distinct = len({str(v) for v in vals})
        # A community field GROUPS nodes, so it must have fewer distinct values
        # than there are nodes. This is what stops us picking a unique id field.
        grouping = 1.0 if 1 < distinct < max(2, len(nodes)) else 0.2
        report.append({
            "key": key,
            "coverage": round(len(vals) / len(nodes), 3),
            "quality": round(ok, 3),
            "distinct": distinct,
            "score": round((len(vals) / len(nodes)) * ok * grouping, 4),
        })
    report.sort(key=lambda r: (-r["score"], r["key"]))
    best = report[0]["key"] if report and report[0]["score"] > 0 else None
    return best, report


def _sniff_edge_keys(edges):
    if not edges:
        return None, None
    sample = [e for e in edges if isinstance(e, dict)][:200]
    if not sample:
        return None, None  # likely list-pair edges
    src = next((k for k in EDGE_SRC_KEYS if any(k in e for e in sample)), None)
    dst = next((k for k in EDGE_DST_KEYS if any(k in e for e in sample)), None)
    return src, dst


class CodeGraph:
    """A loaded, schema-sniffed code graph. Degrades to empty, never raises."""

    def __init__(self):
        self.ok = False
        self.reason = "not loaded"
        self.path_key = None
        self.community_key = None
        self.path_report = []
        self.community_report = []
        self.edge_keys = (None, None)
        self.node_path = {}      # node id -> path string from the graph
        self.adj = {}            # node id -> set(node id)
        self.community = {}      # node id -> community id
        self.n_nodes = 0
        self.n_edges = 0
        self.source = None
        self.builder_note = None  # fallback builders stamp their caveats here
        self.relations = {}       # relation type -> count (graphify gives these)
        self.builder = "unknown"  # "graphify" | "snaplocate-fallback" 
        self._resolve_cache = {}

    # -- loading ------------------------------------------------------------
    @classmethod
    def load(cls, graph_path):
        g = cls()
        g.source = graph_path
        if not graph_path:
            g.reason = "no graph path given"
            return g
        if not os.path.exists(graph_path):
            g.reason = "graph file not found: %s" % graph_path
            return g
        try:
            with open(graph_path, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except Exception as exc:
            g.reason = "graph file is not readable JSON: %s" % exc
            return g

        if isinstance(data, dict) and "graph" in data and "nodes" not in data:
            data = data["graph"]          # some writers nest it
        if not isinstance(data, dict):
            g.reason = "graph JSON is not an object"
            return g

        g.builder_note = data.get("_snaplocate_caveats") or data.get("caveats")

        raw_nodes = data.get("nodes") or []
        raw_edges = data.get("links")
        if raw_edges is None:
            raw_edges = data.get("edges") or []

        if not raw_nodes:
            g.reason = "graph has no 'nodes'"
            return g

        nodes = _normalise_nodes(raw_nodes)
        g.n_nodes = len(nodes)

        g.path_key, g.path_report = sniff_key(nodes, PATH_KEYS)
        g.community_key, g.community_report = sniff_community_key(nodes)
        if not g.path_key:
            g.reason = ("could not find a path-like field on the nodes; "
                        "add the right key to PATH_KEYS in snaplocate/graph.py")
            return g

        # Node identity: prefer an explicit id, else the path value itself.
        for n in nodes:
            nid = n.get("id", n.get(g.path_key))
            if nid is None:
                continue
            nid = str(nid)
            pv = n.get(g.path_key)
            if isinstance(pv, (str, int)):
                g.node_path[nid] = str(pv)
            if g.community_key and g.community_key in n:
                g.community[nid] = str(n[g.community_key])

        g.edge_keys = _sniff_edge_keys(raw_edges)
        sk, dk = g.edge_keys
        for e in raw_edges:
            a = b = None
            if isinstance(e, dict):
                if sk and dk:
                    a, b = e.get(sk), e.get(dk)
            elif isinstance(e, (list, tuple)) and len(e) >= 2:
                a, b = e[0], e[1]
            if a is None or b is None:
                continue
            if isinstance(e, dict) and e.get("relation"):
                g.relations[e["relation"]] = g.relations.get(e["relation"], 0) + 1
            a, b = str(a), str(b)
            g.adj.setdefault(a, set()).add(b)
            g.adj.setdefault(b, set()).add(a)   # undirected for propagation
            g.n_edges += 1

        # Provenance. Graphify stamps typed relations and an _origin on its
        # nodes; our fallback stamps _snaplocate_caveats. Say which one built
        # this graph rather than letting a demo imply Graphify was used.
        if g.builder_note:
            g.builder = "snaplocate-fallback"
        elif g.relations or any("_origin" in n for n in nodes):
            g.builder = "graphify"

        g.ok = True
        g.reason = "ok"
        return g

    # -- path resolution ----------------------------------------------------
    def resolve(self, repo_files):
        """Map repo-relative paths to node ids.

        Graph paths may be absolute, or prefixed, or use backslashes, so we try
        exact, then longest-suffix, then unique-basename. Returns
        (mapping, stats). Cached: propagate/neighbours/subsystems all call this.
        """
        key = id(repo_files) if isinstance(repo_files, (list, tuple)) else None
        cache_key = tuple(sorted(repo_files)) if len(repo_files) < 2000 else key
        if cache_key in self._resolve_cache:
            return self._resolve_cache[cache_key]

        def norm(p):
            s = str(p).replace("\\", "/").lower()
            while s.startswith("./"):
                s = s[2:]
            return s.lstrip("/")

        # A file maps to a LIST of node ids, not one.
        #
        # Graphify's nodes are symbols: checkout.py alone yields finalise(),
        # TOTAL_LABEL and so on, all sharing one source_file. Mapping a file to
        # a single node would seed one arbitrary symbol and throw the rest of
        # the file's connectivity away -- on a real graphify build that is most
        # of the graph. File-level graphs (our fallback) simply map to a
        # one-element list, so both shapes work.
        by_norm = {}
        by_base = {}
        for nid, p in self.node_path.items():
            n = norm(p)
            by_norm.setdefault(n, []).append(nid)
            by_base.setdefault(n.rsplit("/", 1)[-1], []).append(nid)

        mapping = {}
        how = {"exact": 0, "suffix": 0, "basename": 0, "unresolved": 0,
               "nodes_mapped": 0}
        for rel in repo_files:
            n = norm(rel)
            if n in by_norm:
                mapping[rel] = list(by_norm[n])
                how["exact"] += 1
            else:
                hit = []
                for gn, nids in by_norm.items():
                    if gn.endswith("/" + n) or n.endswith("/" + gn):
                        hit.extend(nids)
                if hit:
                    mapping[rel] = hit
                    how["suffix"] += 1
                else:
                    cands = by_base.get(n.rsplit("/", 1)[-1], [])
                    # only trust a basename match when it is unambiguous
                    uniq_files = {norm(self.node_path[c]) for c in cands}
                    if cands and len(uniq_files) == 1:
                        mapping[rel] = list(cands)
                        how["basename"] += 1
                    else:
                        how["unresolved"] += 1
                        continue
            how["nodes_mapped"] += len(mapping[rel])

        result = (mapping, how)
        self._resolve_cache[cache_key] = result
        return result

    # -- propagation --------------------------------------------------------
    def propagate(self, seed_by_file, repo_files):
        """Stage 4. Spread seed mass up to GRAPH_MAX_HOPS with decay.

        Returns (graph_scores_by_file, info). Files with no node resolve simply
        get no graph score -- that is the degradation, not an error.
        """
        mapping, how = self.resolve(repo_files)
        if not mapping:
            return {}, {"resolved": 0, "how": how,
                        "note": "no repo file resolved to a graph node"}

        rev = {}
        for rel, nids in mapping.items():
            for nid in nids:
                rev.setdefault(nid, []).append(rel)

        mass = {}
        for rel, s in seed_by_file.items():
            if s <= 0 or rel not in mapping:
                continue
            starts = mapping[rel]
            # BFS by hop so each node is charged at its SHORTEST distance only.
            # All of the file's symbol nodes start at hop 0 together.
            seen = set(starts)
            frontier = list(starts)
            for hop in range(0, config.GRAPH_MAX_HOPS + 1):
                if hop > 0:
                    nxt = []
                    for nid in frontier:
                        for nb in self.adj.get(nid, ()):
                            if nb not in seen:
                                seen.add(nb)
                                nxt.append(nb)
                    frontier = nxt
                if not frontier:
                    break
                if hop == 0 and not config.GRAPH_INCLUDE_HOP0:
                    continue
                add = s * (config.GRAPH_DECAY ** hop)
                for nid in frontier:
                    mass[nid] = mass.get(nid, 0.0) + add

        by_file = {}
        for nid, m in mass.items():
            for rel in rev.get(nid, ()):
                by_file[rel] = by_file.get(rel, 0.0) + m

        return minmax(by_file), {
            "resolved": len(mapping),
            "how": how,
            "include_hop0": config.GRAPH_INCLUDE_HOP0,
        }

    # -- stage 7 helpers ----------------------------------------------------
    def neighbours(self, rel, repo_files, hops=1):
        """Repo files within `hops` of `rel`, excluding `rel` itself."""
        mapping, _ = self.resolve(repo_files)
        if rel not in mapping:
            return []
        rev = {}
        for r, nids in mapping.items():
            for nid in nids:
                rev.setdefault(nid, []).append(r)
        starts = mapping[rel]
        seen = set(starts)
        frontier = list(starts)
        for _ in range(hops):
            nxt = []
            for nid in frontier:
                for nb in self.adj.get(nid, ()):
                    if nb not in seen:
                        seen.add(nb)
                        nxt.append(nb)
            frontier = nxt
            if not frontier:
                break
        out = set()
        for nid in seen:
            out.update(rev.get(nid, ()))
        out.discard(rel)
        return sorted(out)

    def subsystems_of(self, rels, repo_files):
        """Distinct community ids covering `rels`. Empty if no community field."""
        if not self.community:
            return []
        mapping, _ = self.resolve(repo_files)
        out = set()
        for rel in rels:
            for nid in mapping.get(rel, ()):
                if nid in self.community:
                    out.add(self.community[nid])
        return sorted(out)

    def doctor_report(self):
        return {
            "source": self.source,
            "ok": self.ok,
            "reason": self.reason,
            "nodes": self.n_nodes,
            "edges": self.n_edges,
            "path_key": self.path_key,
            "path_candidates": self.path_report[:6],
            "community_key": self.community_key,
            "community_candidates": self.community_report[:6],
            "edge_keys": {"source": self.edge_keys[0],
                          "target": self.edge_keys[1]},
            "sample_paths": list(self.node_path.values())[:5],
            "builder_note": self.builder_note,
            "builder": self.builder,
            "relations": dict(sorted(self.relations.items(),
                                     key=lambda kv: -kv[1])),
            "communities": len(set(self.community.values())) if self.community else 0,
        }
