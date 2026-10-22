#!/usr/bin/env python3
"""Get a demo target ready in one command: clone at a commit, build the graph.

Python (not bash) so it behaves the same on your laptop and on the Droplet.
Standard library only; `git` is the one external binary, and `graphify` is used
if it is on PATH.

    # a demo repo, at the commit you want
    python3 scripts/setup_demo.py --repo https://github.com/owner/name \
        --commit abc1234

    # let it find the base commit from the fix PR
    python3 scripts/setup_demo.py --repo https://github.com/owner/name --pr 456

    # set up EVERY repo in the eval CSV, each at its own base commit
    python3 scripts/setup_demo.py --from-csv data/issues.csv

Clones land in ./repos/<name>-<shortsha> by default, so two rows needing the
same repo at different commits do not fight over one checkout.

It will not quietly pass a fallback graph off as Graphify: whichever builder
produced graph.json is printed, and written into the file itself.
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from snaplocate import gh, mkgraph                      # noqa: E402
from snaplocate.graph import CodeGraph                  # noqa: E402
from snaplocate.search import load_repo                 # noqa: E402

GREEN, RED, YELL, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    GREEN = RED = YELL = DIM = OFF = ""     # old consoles render these as junk


def say(msg=""):
    print(msg, flush=True)


def step(msg):
    say("\n%s== %s%s" % (DIM, msg, OFF))


def ok(msg):
    say("   %sok%s  %s" % (GREEN, OFF, msg))


def warn(msg):
    say("   %s!%s   %s" % (YELL, OFF, msg))


def bad(msg):
    say("   %sX%s   %s" % (RED, OFF, msg))


class SetupError(RuntimeError):
    pass


def run(cmd, cwd=None, timeout=600, check=True):
    """Run a command. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        raise SetupError("%s is not installed or not on PATH" % cmd[0])
    except subprocess.TimeoutExpired:
        raise SetupError("timed out after %ds: %s" % (timeout, " ".join(cmd)))
    if check and p.returncode != 0:
        raise SetupError("%s failed (rc=%d)\n%s"
                         % (" ".join(cmd[:3]), p.returncode,
                            (p.stderr or p.stdout or "").strip()[:600]))
    return p.returncode, p.stdout, p.stderr


def repo_name(url):
    n = url.rstrip("/").rsplit("/", 1)[-1]
    return n[:-4] if n.endswith(".git") else n


# -- clone ------------------------------------------------------------------
def clone_at(url, commit, dest, force=False):
    """Put `url` at `commit` in `dest`. Idempotent; reuses an existing clone.

    Fetches the single commit where the host allows it (GitHub does), falling
    back to a full fetch. The result is a detached HEAD at exactly `commit`,
    which is what the eval's base-commit check compares against.
    """
    dest = os.path.abspath(dest)
    git_dir = os.path.join(dest, ".git")
    # True only if we fetched this exact ref, so FETCH_HEAD is known to be it.
    single_ref = False

    if os.path.isdir(git_dir) and force:
        warn("--force: removing existing %s" % dest)
        shutil.rmtree(dest, ignore_errors=True)

    if os.path.isdir(git_dir):
        ok("reusing existing clone %s" % dest)
        _, cur, _ = run(["git", "-C", dest, "rev-parse", "HEAD"], check=False)
        if cur.strip().startswith(commit[:7]):
            ok("already at %s" % commit[:10])
            return dest
        rc, _, _ = run(["git", "-C", dest, "cat-file", "-e", commit + "^{commit}"],
                       check=False)
        if rc != 0:
            say("   fetching %s ..." % commit[:10])
            rc, _, err = run(["git", "-C", dest, "fetch", "--depth", "1",
                              "origin", commit], check=False)
            if rc == 0:
                single_ref = True
            else:
                warn("single-commit fetch refused; full fetch (slower)")
                run(["git", "-C", dest, "fetch", "origin",
                     "+refs/heads/*:refs/remotes/origin/*",
                     "+refs/tags/*:refs/tags/*"])
    else:
        if os.path.exists(dest) and os.listdir(dest):
            raise SetupError("%s exists and is not a git clone. Move it, or "
                             "pass --force." % dest)
        os.makedirs(dest, exist_ok=True)
        run(["git", "init", "-q", dest])
        if os.name == "nt":
            # Windows caps paths at 260 chars by default. A repo with deep
            # directories, cloned into an already-long path, fails mid-fetch
            # with "Filename too long" and leaves a broken checkout.
            run(["git", "-C", dest, "config", "core.longpaths", "true"],
                check=False)
        run(["git", "-C", dest, "remote", "add", "origin", url])
        say("   fetching %s from %s ..." % (commit[:10], url))
        rc, _, err = run(["git", "-C", dest, "fetch", "--depth", "1",
                          "origin", commit], check=False)
        if rc == 0:
            single_ref = True
        else:
            warn("single-commit fetch refused; cloning history (slower)")
            rc, _, err = run(["git", "-C", dest, "fetch", "origin",
                              "+refs/heads/*:refs/remotes/origin/*",
                              "+refs/tags/*:refs/tags/*"], check=False)
            if rc != 0:
                hint = ""
                if "Filename too long" in (err or ""):
                    hint = ("\n\nWindows MAX_PATH. Clone somewhere shorter, "
                            "e.g. --dest C:/sl/<name>, or enable long paths "
                            "system-wide:\n"
                            "  git config --global core.longpaths true")
                raise SetupError("could not fetch from %s\n%s%s"
                                 % (url, err.strip()[:400], hint))

    # `commit` may be a sha, a tag or a branch. A shallow fetch of a tag or
    # branch leaves it in FETCH_HEAD without creating a local ref, so resolve
    # to a concrete sha before checking out rather than handing git a name it
    # will mistake for a path.
    target = _resolve_ref(dest, commit, fetch_head_ok=single_ref)
    rc, _, err = run(["git", "-C", dest, "checkout", "-q", "--detach", target],
                     check=False)
    if rc != 0:
        raise SetupError("could not check out %s (resolved to %s) in %s\n%s"
                         % (commit, target[:10], dest, err.strip()[:400]))

    _, head, _ = run(["git", "-C", dest, "rev-parse", "HEAD"])
    head = head.strip()
    if _looks_like_sha(commit) and not head.startswith(commit[:7]):
        raise SetupError("asked for %s but HEAD is %s" % (commit, head))
    if _looks_like_sha(commit):
        ok("at %s (detached)" % head[:10])
    else:
        ok("at %s (detached) -- %r resolved to that commit" % (head[:10], commit))
        warn("%r is a name, not a commit sha. Fine for a demo; for an eval "
             "row use the PR's base sha so data/issues.csv can be checked."
             % commit)
    return dest


def _looks_like_sha(s):
    s = (s or "").strip()
    return len(s) >= 7 and all(c in "0123456789abcdefABCDEF" for c in s)


def _resolve_ref(dest, ref, fetch_head_ok=False):
    """Resolve a sha / tag / branch to a concrete commit sha inside `dest`.

    `fetch_head_ok` must only be True when we just fetched THIS EXACT ref, so
    FETCH_HEAD is known to be it. After a full `git fetch origin`, FETCH_HEAD
    is the default branch tip: trusting it there silently checks out main
    instead of the commit that was asked for -- which, for an eval, means
    measuring against code that already contains the fix. That is the single
    worst thing this script could do, so the fallback is gated rather than
    convenient.
    """
    cands = [ref + "^{commit}",
             "refs/tags/" + ref + "^{commit}",
             "origin/" + ref + "^{commit}"]
    if fetch_head_ok:
        cands.append("FETCH_HEAD^{commit}")
    for cand in cands:
        rc, out, _ = run(["git", "-C", dest, "rev-parse", "--verify",
                          "--quiet", cand], check=False)
        if rc == 0 and out.strip():
            return out.strip()
    raise SetupError(
        "could not resolve %r to a commit in %s.\n"
        "If it is a sha, check it really exists in this repo (a typo'd or "
        "truncated sha lands here).\n"
        "If it is a tag or branch, the fetch did not retrieve it.\n"
        "Refusing to fall back to the default branch: that would check out "
        "code that may already contain the fix." % (ref, dest))


# -- graph ------------------------------------------------------------------
def graphify_available():
    return shutil.which("graphify") is not None


def try_graphify(repo_dir, timeout=900):
    """Run the real Graphify CLI. Returns the graph path, or None.

    Returning None is not a failure of this script -- it is the signal to fall
    back, and the caller prints which builder actually won.
    """
    if not graphify_available():
        warn("`graphify` is not on PATH (pip install graphifyy)")
        return None
    say("   running graphify (this can take a few minutes) ...")
    rc, out, err = run(["graphify", repo_dir], cwd=repo_dir,
                       timeout=timeout, check=False)
    if rc != 0:
        warn("graphify exited %d: %s" % (rc, (err or out).strip()[:300]))
        return None
    for cand in (os.path.join(repo_dir, "graphify-out", "graph.json"),
                 os.path.join(os.getcwd(), "graphify-out", "graph.json")):
        if os.path.exists(cand):
            ok("graphify wrote %s" % cand)
            return cand
    warn("graphify exited 0 but no graphify-out/graph.json appeared")
    return None


def graph_is_usable(graph_path, repo_dir, min_pct=50.0):
    """Load it and check it actually lines up with the repo. (usable, report)"""
    cg = CodeGraph.load(graph_path)
    if not cg.ok:
        return False, "graph did not load: %s" % cg.reason
    corpus = load_repo(repo_dir)
    if not corpus:
        return False, "no source files found in the repo"
    mapping, how = cg.resolve(list(corpus.keys()))
    pct = 100.0 * len(mapping) / len(corpus)
    rep = ("%d nodes / %d edges, path field '%s', %d of %d files resolved "
           "(%.0f%%)" % (cg.n_nodes, cg.n_edges, cg.path_key, len(mapping),
                         len(corpus), pct))
    if not mapping:
        return False, rep + " -- NOTHING resolved"
    if pct < min_pct:
        return False, rep + " -- below %.0f%%" % min_pct
    return True, rep


def build_graph(repo_dir, prefer_fallback=False):
    """-> (graph_path, builder, report). builder is 'graphify' or 'fallback'."""
    out_dir = os.path.join(repo_dir, "graphify-out")
    out_path = os.path.join(out_dir, "graph.json")

    if not prefer_fallback:
        gpath = try_graphify(repo_dir)
        if gpath:
            usable, rep = graph_is_usable(gpath, repo_dir)
            if usable:
                ok("graphify graph is usable: %s" % rep)
                return gpath, "graphify", rep
            warn("graphify graph is not usable: %s" % rep)
            warn("falling back -- keeping the graphify output at %s.graphify"
                 % gpath)
            try:
                shutil.move(gpath, gpath + ".graphify")
            except OSError:
                pass
    else:
        warn("--fallback-graph given: not trying graphify at all")

    say("   building the fallback graph (file-level imports) ...")
    g = mkgraph.build(repo_dir, out_path)
    usable, rep = graph_is_usable(out_path, repo_dir)
    if usable:
        ok("fallback graph: %s" % rep)
    else:
        bad("fallback graph is not usable either: %s" % rep)
    say("")
    warn("THIS GRAPH IS FROM THE FALLBACK BUILDER, NOT GRAPHIFY.")
    warn("Say so in the README and on the slide. Edges are file-level")
    warn("imports (Python via ast, JS/TS via regex), not call edges, and")
    warn("communities are connected components, not Leiden.")
    return out_path, "fallback", rep


# -- one target -------------------------------------------------------------
def setup_one(url, commit, dest, *, force=False, prefer_fallback=False,
              skip_graph=False):
    step("clone %s @ %s" % (url, commit[:10]))
    repo_dir = clone_at(url, commit, dest, force=force)

    corpus = load_repo(repo_dir)
    ok("%d source files" % len(corpus))
    if not corpus:
        warn("no files with a known source extension -- check "
             "config.SOURCE_EXTS, or this repo is in a language we do not scan")

    if skip_graph:
        warn("--skip-graph: no graph built; SnapLocate will run search-only")
        return {"repo": repo_dir, "graph": None, "builder": None,
                "files": len(corpus)}

    step("graph")
    gpath, builder, rep = build_graph(repo_dir, prefer_fallback=prefer_fallback)
    return {"repo": repo_dir, "graph": gpath, "builder": builder,
            "files": len(corpus), "graph_report": rep}


def resolve_commit(args, owner, repo):
    """Work out which commit to check out, and say where it came from."""
    if args.commit:
        return args.commit, "you passed --commit"
    if args.pr:
        step("resolving the base commit of PR #%d" % args.pr)
        info = gh.pr_base(owner, repo, args.pr)
        if not info["base_sha"]:
            raise SetupError("PR #%d has no base sha" % args.pr)
        if not info["merged"]:
            warn("PR #%d is NOT merged. For the eval you want a merged fix PR."
                 % args.pr)
        ok("base of #%d (%s) is %s on %s"
           % (args.pr, info["title"][:40], info["base_sha"][:10],
              info["base_ref"]))
        files = gh.pr_changed_files(owner, repo, args.pr)
        if files:
            say("")
            say("   files that PR changed (candidate gold_files -- you still")
            say("   have to drop tests/docs/lockfiles/snapshots yourself):")
            from snaplocate.evalh import is_source_gold
            keep, drop = [], []
            for f in files:
                (keep if is_source_gold(f)[0] else drop).append(f)
            for f in keep:
                say("     %skeep%s %s" % (GREEN, OFF, f))
            for f in drop:
                say("     %sdrop%s %s  (%s)"
                    % (DIM, OFF, f, is_source_gold(f)[1]))
            if keep:
                say("")
                say("   gold_files column:  %s" % ";".join(keep))
            else:
                warn("nothing left after exclusions -- this PR is not usable "
                     "as an eval row")
        return info["base_sha"], "base of PR #%d" % args.pr
    raise SetupError("need --commit or --pr")


# -- CSV mode ---------------------------------------------------------------
def setup_from_csv(args):
    from snaplocate.evalh import read_csv
    rows = read_csv(args.from_csv)
    if not rows:
        raise SetupError("no usable rows in %s (every row commented out?)"
                         % args.from_csv)

    say("%d row(s) in %s" % (len(rows), args.from_csv))
    results, failures = [], []
    for r in rows:
        if not r["base_commit"]:
            failures.append((r["issue_url"], "base_commit is empty"))
            bad("line %d: base_commit empty, skipping" % r["line"])
            continue
        try:
            owner, repo, _ = gh.parse_issue_url(r["issue_url"])
        except gh.GitHubError as exc:
            failures.append((r["issue_url"], str(exc)))
            bad("line %d: %s" % (r["line"], exc))
            continue

        url = "https://github.com/%s/%s" % (owner, repo)
        dest = r["repo_path"] or os.path.join(
            args.into, "%s-%s" % (repo, r["base_commit"][:7]))
        try:
            res = setup_one(url, r["base_commit"], dest, force=args.force,
                            prefer_fallback=args.fallback_graph,
                            skip_graph=args.skip_graph)
            res["issue_url"] = r["issue_url"]
            res["line"] = r["line"]
            res["csv_repo_path"] = r["repo_path"]
            results.append(res)
        except SetupError as exc:
            failures.append((r["issue_url"], str(exc)))
            bad(str(exc))

    step("summary")
    for res in results:
        say("   %s" % res["issue_url"])
        say("     repo_path column ->  %s" % res["repo"])
        if res["csv_repo_path"] and os.path.abspath(
                res["csv_repo_path"]) != os.path.abspath(res["repo"]):
            warn("CSV says repo_path=%s but the clone is at %s -- update the "
                 "CSV or they will not match"
                 % (res["csv_repo_path"], res["repo"]))
    for url, why in failures:
        say("   %sFAILED%s %s: %s" % (RED, OFF, url, why))

    if results:
        say("")
        say("Now validate the CSV, then run the eval:")
        say("   python3 -m snaplocate.cli eval %s --check" % args.from_csv)
    nfall = sum(1 for r in results if r["builder"] == "fallback")
    if nfall:
        say("")
        warn("%d of %d graphs came from the FALLBACK builder, not Graphify."
             % (nfall, len(results)))
    return 1 if failures else 0


# -- main -------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        prog="setup_demo.py",
        description="Clone a demo repo at a commit and build its code graph.")
    p.add_argument("--repo", help="https://github.com/owner/name")
    p.add_argument("--commit", help="the commit to check out (the PR's BASE)")
    p.add_argument("--pr", type=int,
                   help="look the base commit up from this PR number")
    p.add_argument("--issue", help="an issue URL, to infer owner/repo")
    p.add_argument("--into", default="repos",
                   help="where clones go (default: ./repos)")
    p.add_argument("--dest", help="exact directory for this one clone")
    p.add_argument("--from-csv", help="set up every repo in an eval CSV")
    p.add_argument("--force", action="store_true",
                   help="delete and re-clone an existing checkout")
    p.add_argument("--fallback-graph", action="store_true",
                   help="skip graphify, go straight to the fallback builder")
    p.add_argument("--skip-graph", action="store_true",
                   help="clone only; SnapLocate will then run search-only")
    args = p.parse_args(argv)

    say("SnapLocate demo setup")
    say("=" * 58)
    if not args.skip_graph:
        say("graphify on PATH: %s"
            % ("yes" if graphify_available() else
               "NO -- the fallback builder will be used"))

    try:
        if args.from_csv:
            return setup_from_csv(args)

        url = args.repo
        if not url and args.issue:
            o, r, _ = gh.parse_issue_url(args.issue)
            url = "https://github.com/%s/%s" % (o, r)
        if not url:
            p.error("need --repo, --issue, or --from-csv")

        owner, name = url.rstrip("/").rsplit("/", 2)[-2:]
        name = repo_name(url)
        commit, why = resolve_commit(args, owner, name)
        say("\ncommit: %s  (%s)" % (commit[:12], why))

        dest = args.dest or os.path.join(args.into,
                                         "%s-%s" % (name, commit[:7]))
        res = setup_one(url, commit, dest, force=args.force,
                        prefer_fallback=args.fallback_graph,
                        skip_graph=args.skip_graph)

        step("ready")
        say("   repo : %s" % res["repo"])
        say("   graph: %s%s" % (res["graph"] or "(none -- search-only)",
                                "" if not res["builder"]
                                else "   [%s]" % res["builder"]))
        say("")
        say("Check the graph lines up before trusting any ranking:")
        say("   python3 -m snaplocate.cli doctor --repo %s%s"
            % (res["repo"], "" if not res["graph"]
               else " \\\n       --graph %s" % res["graph"]))
        say("")
        say("Then locate, or serve the UI:")
        gflag = "" if not res["graph"] else " \\\n       --graph %s" % res["graph"]
        say("   python3 -m snaplocate.cli locate <issue_url> \\\n"
            "       --repo %s%s" % (res["repo"], gflag))
        say("   python3 -m snaplocate.cli serve --port 8000 \\\n"
            "       --repo %s%s" % (res["repo"], gflag))
        return 0

    except (SetupError, gh.GitHubError) as exc:
        say("")
        bad(str(exc))
        return 1
    except KeyboardInterrupt:
        say("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
