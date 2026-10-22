"""Fallback graph builder. INSURANCE ONLY -- Graphify is the primary graph.

If you ship this instead of Graphify, say so in the README and on the slide. It
writes its own caveats into graph.json under "_snaplocate_caveats" so a graph
built here cannot be passed off as a Graphify graph: `doctor` prints that field.

What this is:  file-level import edges, Python via `ast`, JS/TS via regex.
What it is not: a call graph, and not Leiden communities.
"""
import ast
import json
import os
import re

from . import config
from .search import iter_source_files

CAVEATS = [
    "FALLBACK BUILDER, NOT GRAPHIFY. Built by snaplocate/mkgraph.py.",
    "Edges are FILE-LEVEL IMPORTS, not call edges. 'a -> b' means a imports b,"
    " not that a calls into b.",
    "JS/TS/JSX/TSX imports are found by REGEX, not parsed. String literals and"
    " commented-out imports can produce phantom edges; dynamic import() with a"
    " computed path is missed entirely.",
    "Communities are CONNECTED COMPONENTS, not Leiden. A single hub file can"
    " collapse the whole repo into one component, which makes the S/M/L"
    " subsystem count much less meaningful.",
    "Only files with an extension in config.SOURCE_EXTS are nodes.",
]

_JS_IMPORT = re.compile(
    r"""(?:^|\s)(?:import\s+(?:[\w*{}\s,$]+\s+from\s+)?|export\s+[\w*{}\s,]+\s+from\s+)"""
    r"""["']([^"']+)["']"""
    r"""|require\(\s*["']([^"']+)["']\s*\)"""
    r"""|import\(\s*["']([^"']+)["']\s*\)""",
    re.MULTILINE,
)

_JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"]


def _py_imports(src):
    """Dotted module names imported by a Python file.

    `from X import Y` emits BOTH "X" and "X.Y", because Y may be a submodule
    rather than a name inside X -- `from . import inventory` is an edge to
    inventory.py, and resolving only the "." part would miss it entirely.
    Candidates that resolve to no file are dropped by the caller, so emitting
    the extra one costs nothing.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level>0 is a relative import; record the dots so the resolver
            # can walk up from the importing file's package.
            base = "." * (node.level or 0) + (node.module or "")
            out.append(base)
            for a in node.names:
                if a.name == "*":
                    continue
                out.append(base + "." + a.name if not base.endswith(".")
                           else base + a.name)
    return out


def _resolve_py(mod, from_rel, by_module, all_files):
    """Resolve a Python module name to a repo-relative file, or None."""
    if mod.startswith("."):
        depth = len(mod) - len(mod.lstrip("."))
        tail = mod.lstrip(".")
        base = from_rel.rsplit("/", 1)[0] if "/" in from_rel else ""
        for _ in range(depth - 1):
            base = base.rsplit("/", 1)[0] if "/" in base else ""
        cand = (base + "/" + tail.replace(".", "/")).lstrip("/") if tail else base
        for suffix in (".py", "/__init__.py"):
            p = cand + suffix
            if p in all_files:
                return p
        return None
    return by_module.get(mod) or by_module.get(mod.split(".")[0])


def _resolve_js(spec, from_rel, all_files):
    """Resolve a JS import specifier to a repo-relative file, or None.

    Bare specifiers (node_modules packages) resolve to nothing on purpose --
    an edge to 'react' is not a code edge inside this repo.
    """
    if not spec.startswith("."):
        return None
    base = from_rel.rsplit("/", 1)[0] if "/" in from_rel else ""
    parts = [p for p in (base + "/" + spec).split("/") if p not in ("", ".")]
    stack = []
    for p in parts:
        if p == "..":
            if stack:
                stack.pop()
        else:
            stack.append(p)
    cand = "/".join(stack)
    if cand in all_files:
        return cand
    for ext in _JS_EXTS:
        if cand + ext in all_files:
            return cand + ext
    for ext in _JS_EXTS:
        if cand + "/index" + ext in all_files:
            return cand + "/index" + ext
    return None


def _components(nodes, adj):
    """Connected components -> {node: component_id}. NOT Leiden."""
    comp = {}
    cid = 0
    for n in nodes:
        if n in comp:
            continue
        cid += 1
        stack = [n]
        comp[n] = cid
        while stack:
            cur = stack.pop()
            for nb in adj.get(cur, ()):
                if nb not in comp:
                    comp[nb] = cid
                    stack.append(nb)
        # nodes with no edges still get their own component id
    return comp


def build(repo_root, out_path=None):
    """Build a graph.json compatible with graph.py. Returns the dict."""
    files = {}
    for rel, full in iter_source_files(repo_root):
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                files[rel] = fh.read()
        except OSError:
            continue

    all_files = set(files)

    # Python module path -> file, so `import pkg.mod` can find pkg/mod.py.
    by_module = {}
    for rel in all_files:
        if not rel.endswith(".py"):
            continue
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        by_module.setdefault(mod, rel)
        by_module.setdefault(mod.split(".")[-1], rel)

    edges = set()
    for rel, src in files.items():
        if rel.endswith(".py"):
            for mod in _py_imports(src):
                tgt = _resolve_py(mod, rel, by_module, all_files)
                if tgt and tgt != rel:
                    edges.add((rel, tgt))
        elif os.path.splitext(rel)[1].lower() in set(_JS_EXTS):
            for m in _JS_IMPORT.finditer(src):
                spec = m.group(1) or m.group(2) or m.group(3)
                if not spec:
                    continue
                tgt = _resolve_js(spec, rel, all_files)
                if tgt and tgt != rel:
                    edges.add((rel, tgt))

    adj = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    comp = _components(sorted(all_files), adj)

    graph = {
        "_snaplocate_caveats": CAVEATS,
        "directed": True,
        "multigraph": False,
        "graph": {"builder": "snaplocate.mkgraph", "kind": "file-import-graph"},
        "nodes": [
            {"id": rel, "path": rel, "community": comp.get(rel, 0),
             "degree": len(adj.get(rel, ()))}
            for rel in sorted(all_files)
        ],
        "links": [{"source": a, "target": b} for a, b in sorted(edges)],
    }

    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(graph, fh, indent=2)
    return graph
