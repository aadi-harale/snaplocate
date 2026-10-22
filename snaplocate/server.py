"""Web UI on stdlib http.server. No framework, no build step.

    python3 -m snaplocate.cli serve --repo . --graph graphify-out/graph.json

Routes:
    GET  /                  the single HTML page
    GET  /api/health        config + whether the model key is present
    POST /api/locate        {"issue_url": "..."} -> the full pipeline result
    GET  /api/image?url=    proxies the screenshot so the page can draw on it

The image proxy exists because the page needs the same bytes the model saw, and
because GitHub attachment URLs can redirect. It only fetches http(s) URLs that
appear in the issue we just fetched -- see _allowed_images.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import cache, gemma, gh, pipeline

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "web", "static")

# URLs we have seen in an issue this process fetched. The proxy will not fetch
# anything else, so the endpoint cannot be used to make the server request
# arbitrary URLs on someone's behalf.
_allowed_images = set()
_allowed_lock = threading.Lock()
MAX_ALLOWED = 500


def _allow(urls):
    with _allowed_lock:
        if len(_allowed_images) > MAX_ALLOWED:
            _allowed_images.clear()
        _allowed_images.update(urls)


def _is_allowed(url):
    with _allowed_lock:
        return url in _allowed_images


class Handler(BaseHTTPRequestHandler):
    server_version = "SnapLocate/0.1"
    repo_root = "."
    graph_path = None
    cache_dir = cache.DEFAULT_DIR
    record = False          # save every successful live run
    offline = False         # never touch the network; replay only
    cache_first = False     # replay a cached issue instead of calling the API

    def log_message(self, fmt, *a):
        print("[web] %s - %s" % (self.address_string(), fmt % a))

    # -- helpers ------------------------------------------------------------
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._serve_page()
        if u.path == "/api/health":
            entries = cache.list_entries(self.cache_dir)
            return self._send(200, {
                "ok": True,
                "model_key_present": bool(gemma.api_key()),
                "github_token_present": bool(os.environ.get("GITHUB_TOKEN")
                                             or os.environ.get("GH_TOKEN")),
                "repo_root": os.path.abspath(self.repo_root),
                "graph_path": self.graph_path,
                "graph_exists": bool(self.graph_path
                                     and os.path.exists(self.graph_path)),
                "box_convention": gemma.box_convention(),
                "offline": self.offline,
                "recording": self.record,
                "cache_first": self.cache_first,
                "cached_issues": [
                    {"issue_url": m.get("issue_url"),
                     "title": m.get("title"),
                     "recorded_at_human": m.get("recorded_at_human"),
                     "image_cached": m.get("image_cached"),
                     "image_url": m.get("image_url"),
                     "model": m.get("model"),
                     "n_ranked": m.get("n_ranked"),
                     "n_regions": m.get("n_regions")}
                    for m in entries],
            })
        if u.path == "/api/image":
            return self._serve_image(parse_qs(u.query).get("url", [""])[0])
        return self._err(404, "not found")

    def _serve_page(self):
        path = os.path.join(STATIC, "index.html")
        if not os.path.exists(path):
            return self._err(500, "web/static/index.html is missing")
        with open(path, "rb") as fh:
            body = fh.read()
        self._send(200, body, "text/html; charset=utf-8")

    def _serve_image(self, url):
        if not url:
            return self._err(400, "url required")
        if not url.startswith(("http://", "https://")):
            return self._err(400, "only http(s) URLs")
        cached_ok = cache.load_image(self.cache_dir, url) is not None
        if not _is_allowed(url) and not cached_ok:
            return self._err(403, "that image URL did not come from an issue "
                                  "this server fetched, and is not in the "
                                  "cache")

        # Cache first when offline, and as a fallback when the fetch fails --
        # a replayed result whose screenshot will not load is a broken demo.
        cached = cache.load_image(self.cache_dir, url)
        if self.offline:
            if not cached:
                return self._err(503, "offline and this screenshot is not "
                                      "cached")
            return self._send(200, cached[0], cached[1],
                              {"Cache-Control": "private, max-age=300"})
        try:
            data, mime = gh.download_image(url)
        except gh.GitHubError as exc:
            if cached:
                return self._send(200, cached[0], cached[1],
                                  {"Cache-Control": "private, max-age=300"})
            return self._err(502, str(exc))
        if self.record:
            try:
                cache.save_image(self.cache_dir, url, data, mime)
            except OSError:
                pass
        self._send(200, data, mime, {"Cache-Control": "private, max-age=300"})

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/locate":
            return self._err(404, "not found")

        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "bad Content-Length")
        if n <= 0 or n > 64_000:
            return self._err(400, "body must be 1..64000 bytes")
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception as exc:
            return self._err(400, "bad JSON: %s" % exc)

        issue_url = (req.get("issue_url") or "").strip()
        if not issue_url:
            return self._err(400, "issue_url required")
        try:
            gh.parse_issue_url(issue_url)
        except gh.GitHubError as exc:
            return self._err(400, str(exc))

        variant = req.get("variant")

        def finish(result):
            _allow(result.get("image_urls") or [])
            # The page draws boxes using this, so send it explicitly rather
            # than letting the frontend assume a convention.
            result.setdefault("box_convention", gemma.box_convention())
            return self._send(200, result)

        def replay(reason):
            hit = cache.load(self.cache_dir, issue_url)
            if not hit:
                return None
            result, meta = hit
            return cache.mark_replayed(result, meta, reason)

        # --- cache-first: a recorded issue replays instantly ----------------
        # The demo path. Gemma costs ~45s for the x-ray plus ~30s for the
        # rerank, and the free tier rate-limits at two calls per issue, so
        # running the live pipeline in front of judges is slow AND the most
        # likely thing to fail. Cached issues come back instantly; anything
        # not cached still runs live.
        if self.cache_first and not self.offline:
            r = replay("cache-first mode: this issue was recorded earlier")
            if r:
                return finish(r)

        # --- offline: replay only, never touch the network -----------------
        if self.offline:
            r = replay("server is in offline demo mode")
            if r:
                return finish(r)
            return self._err(503, "offline demo mode and this issue is not in "
                                  "the cache. Cached: %d issue(s)."
                                  % len(cache.list_entries(self.cache_dir)))

        # --- no key: the API is effectively down, so fall back -------------
        if not gemma.api_key():
            r = replay("no GEMMA_API_KEY on the server")
            if r:
                return finish(r)
            return self._err(503, "no GEMMA_API_KEY on the server; the vision "
                                  "stage cannot run, and this issue is not "
                                  "cached")

        try:
            res = pipeline.run(
                issue_url=issue_url,
                repo_root=self.repo_root,
                graph_path=self.graph_path,
                search_only=(variant == "search-only"),
                do_rerank=bool(req.get("rerank", True)),
                model=req.get("model") or None,
            )
        except Exception as exc:                        # noqa: BLE001
            # pipeline.run is meant never to raise; if it somehow does, the
            # demo should still survive it.
            r = replay("the live run raised %s" % type(exc).__name__)
            if r:
                return finish(r)
            return self._err(500, "live run failed: %s: %s"
                             % (type(exc).__name__, exc))

        # --- live run failed: replay rather than show the audience an error -
        if res.get("error"):
            r = replay("the live run failed at stage '%s'"
                       % res.get("failed_stage"))
            if r:
                r["live_error"] = res.get("error")
                return finish(r)
            return finish(res)

        if self.record:
            try:
                image = None
                urls = res.get("image_urls") or []
                if urls:
                    got = cache.load_image(self.cache_dir, urls[0])
                    if not got:
                        got = gh.download_image(urls[0])
                        cache.save_image(self.cache_dir, urls[0], *got)
                    image = got
                cache.save(self.cache_dir, issue_url, res, image=image)
                res["recorded"] = True
            except Exception as exc:                    # noqa: BLE001
                res.setdefault("degraded", []).append(
                    "could not record this run to the cache: %s" % exc)
        return finish(res)


def serve(host="0.0.0.0", port=8000, repo_root=".", graph_path=None,
          cache_dir=cache.DEFAULT_DIR, record=False, offline=False,
          cache_first=False):
    Handler.repo_root = repo_root
    Handler.graph_path = graph_path
    Handler.cache_dir = cache_dir
    Handler.record = record
    Handler.offline = offline
    Handler.cache_first = cache_first

    entries = cache.list_entries(cache_dir)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("SnapLocate on http://%s:%d" % (host, port))
    print("  repo  : %s" % os.path.abspath(repo_root))
    print("  graph : %s%s" % (graph_path,
                              "" if graph_path and os.path.exists(graph_path)
                              else "  (MISSING -> search-only)"))
    print("  model : %s" % ("key present" if gemma.api_key()
                            else "NO GEMMA_API_KEY -- will replay from cache"))
    print("  cache : %s (%d issue(s) recorded)"
          % (os.path.abspath(cache_dir), len(entries)))
    if offline:
        print("")
        print("  OFFLINE DEMO MODE: nothing will hit the network. Only the")
        print("  %d cached issue(s) can be shown, and every result is" % len(entries))
        print("  labelled as a replay in the UI.")
        for m in entries:
            print("    %s" % m.get("issue_url"))
    elif cache_first:
        print("")
        print("  CACHE-FIRST: a recorded issue replays instantly (labelled as")
        print("  a replay); anything not cached still runs live.")
        for m in entries:
            print("    %s" % m.get("issue_url"))
    if record:
        print("  RECORDING: successful live runs are saved for offline replay")
    if not offline and entries:
        print("  (if a live run fails, a cached issue replays automatically)")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
