"""Vercel entry point.

Vercel's Python runtime imports `handler`, a BaseHTTPRequestHandler subclass --
which is exactly what snaplocate/server.py already is, so this file is a thin
configuration shim rather than a second implementation.

It runs in OFFLINE mode on purpose:
  * a serverless filesystem is read-only apart from /tmp, so nothing can be
    recorded anyway;
  * no API key has to live in the deployment, so the public demo holds no
    secret at all;
  * the committed demo-cache replays a real recorded run, and every response
    is labelled as a replay.

A live run needs a key and a repo checkout, which is what the Droplet deploy
(deploy/droplet.sh) is for.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from snaplocate.server import Handler as _Handler   # noqa: E402

# NOTE: Vercel finds the entrypoint by looking for a top-level `app`,
# `application` or `handler`, and it does NOT see a plain assignment like
# `handler = _Handler` -- that failed the build with
# PYTHON_ENTRYPOINT_NOT_FOUND. It has to be a real class statement, so
# `handler` subclasses our server's Handler and inherits every route.

_Handler.repo_root = os.path.join(ROOT, "demo-repo")
_Handler.graph_path = os.path.join(ROOT, "demo-repo", "graphify-out", "graph.json")
_Handler.cache_dir = os.path.join(ROOT, "demo-cache")
_Handler.offline = True      # replay only; never reach the network
_Handler.record = False      # the filesystem is read-only here
_Handler.cache_first = True

class handler(_Handler):            # noqa: N801  (Vercel requires this name)
    """The Vercel entrypoint. All behaviour is inherited from the one server
    implementation in snaplocate/server.py -- this adds nothing."""
