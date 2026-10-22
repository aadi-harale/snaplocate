"""Offline demo mode: record a real run, replay it when the network is gone.

The venue wifi dies, or the API rate-limits you, and the demo still runs.

THE RULE THIS FILE ENFORCES: a replayed result is never presented as a live
one. Every replay carries a `replay` block with the recording timestamp and
its age, the UI shows a badge, and the CLI prints a banner. A cached result
shown as live would be the same dishonesty as letting a fallback graph imply
Graphify -- the demo has to survive the wifi, not the audience's trust.

Layout under --cache-dir (default ./demo-cache):

    demo-cache/
      entries/<key>/result.json     the pipeline output, as recorded
      entries/<key>/meta.json       issue url, recorded_at, model, graph info
      images/<urlkey>.bin           screenshot bytes
      images/<urlkey>.json          its mime type and source url

`key` is a hash of the issue URL, so re-recording the same issue replaces it.
"""
import hashlib
import json
import os
import time

DEFAULT_DIR = "demo-cache"


def _key(s):
    return hashlib.sha1((s or "").strip().encode("utf-8")).hexdigest()[:16]


def _entry_dir(cache_dir, issue_url):
    return os.path.join(cache_dir, "entries", _key(issue_url))


def _images_dir(cache_dir):
    return os.path.join(cache_dir, "images")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
    os.replace(tmp, path)       # atomic: a half-written cache is worse than none


# -- images -----------------------------------------------------------------
def save_image(cache_dir, url, data, mime):
    if not url or not data:
        return None
    d = _images_dir(cache_dir)
    os.makedirs(d, exist_ok=True)
    k = _key(url)
    with open(os.path.join(d, k + ".bin"), "wb") as fh:
        fh.write(data)
    _write_json(os.path.join(d, k + ".json"),
                {"url": url, "mime": mime, "bytes": len(data)})
    return k


def load_image(cache_dir, url):
    """-> (bytes, mime) or None."""
    d = _images_dir(cache_dir)
    k = _key(url)
    binp = os.path.join(d, k + ".bin")
    meta = _read_json(os.path.join(d, k + ".json")) or {}
    if not os.path.exists(binp):
        return None
    try:
        with open(binp, "rb") as fh:
            return fh.read(), meta.get("mime") or "image/png"
    except OSError:
        return None


# -- entries ----------------------------------------------------------------
def save(cache_dir, issue_url, result, image=None):
    """Record a successful run. `image` is (bytes, mime).

    Refuses to record a failed run: replaying an error teaches you nothing and
    would turn the safety net into a second way to fail on stage.
    """
    if result.get("error"):
        raise ValueError("refusing to cache a failed run (%s): there is "
                         "nothing useful to replay"
                         % result.get("failed_stage"))

    ed = _entry_dir(cache_dir, issue_url)
    os.makedirs(ed, exist_ok=True)

    img_key = None
    if image:
        data, mime = image
        urls = result.get("image_urls") or []
        if urls:
            img_key = save_image(cache_dir, urls[0], data, mime)

    meta = {
        "issue_url": issue_url,
        "recorded_at": time.time(),
        "recorded_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "title": (result.get("issue") or {}).get("title", ""),
        "model": (result.get("xray") or {}).get("model"),
        "variant": result.get("variant"),
        "used_graph": result.get("ranking_used_graph"),
        "n_ranked": len(result.get("ranked") or []),
        "n_regions": len((result.get("xray") or {}).get("regions") or []),
        "image_key": img_key,
        "image_url": (result.get("image_urls") or [None])[0],
        "degraded_when_recorded": result.get("degraded") or [],
    }
    _write_json(os.path.join(ed, "result.json"), result)
    _write_json(os.path.join(ed, "meta.json"), meta)
    return ed


def load(cache_dir, issue_url):
    """-> (result, meta) or None. Never raises."""
    ed = _entry_dir(cache_dir, issue_url)
    result = _read_json(os.path.join(ed, "result.json"))
    meta = _read_json(os.path.join(ed, "meta.json"))
    if not result or not meta:
        return None
    return result, meta


def has(cache_dir, issue_url):
    return load(cache_dir, issue_url) is not None


def mark_replayed(result, meta, reason):
    """Stamp a cached result so it can never be mistaken for a live run."""
    out = dict(result)
    recorded = meta.get("recorded_at") or 0
    age = max(0, time.time() - recorded)
    out["replay"] = {
        "replayed": True,
        "reason": reason,
        "recorded_at": recorded,
        "recorded_at_human": meta.get("recorded_at_human"),
        "age_seconds": int(age),
        "age_human": _age(age),
        "model": meta.get("model"),
    }
    out["degraded"] = list(out.get("degraded") or [])
    out["degraded"].insert(
        0, "REPLAYED FROM CACHE (recorded %s, %s ago) -- %s. Nothing in this "
           "result was computed just now."
           % (meta.get("recorded_at_human"), _age(age), reason))
    return out


def _age(seconds):
    s = int(seconds)
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm" % (s // 60)
    if s < 172800:
        return "%dh" % (s // 3600)
    return "%dd" % (s // 86400)


def list_entries(cache_dir):
    """Every recorded entry, newest first."""
    root = os.path.join(cache_dir, "entries")
    out = []
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        meta = _read_json(os.path.join(root, name, "meta.json"))
        if not meta:
            continue
        has_img = False
        if meta.get("image_url"):
            has_img = load_image(cache_dir, meta["image_url"]) is not None
        meta = dict(meta)
        meta["key"] = name
        meta["image_cached"] = has_img
        out.append(meta)
    out.sort(key=lambda m: m.get("recorded_at") or 0, reverse=True)
    return out


def check(cache_dir):
    """Pre-flight: would each entry actually replay? -> (entries, problems).

    Run this BEFORE the demo, not during it.
    """
    entries = list_entries(cache_dir)
    problems = []
    if not entries:
        problems.append("cache is empty: nothing to fall back on. Record a "
                        "run first with `locate <url> --record`.")
    for m in entries:
        tag = m.get("issue_url") or m.get("key")
        loaded = load(cache_dir, m.get("issue_url") or "")
        if not loaded:
            problems.append("%s: meta.json exists but result.json does not "
                            "load" % tag)
            continue
        result, _ = loaded
        if result.get("error"):
            problems.append("%s: cached result is an ERROR (%s) -- re-record"
                            % (tag, result.get("failed_stage")))
        if not result.get("ranked"):
            problems.append("%s: cached result has no ranked files" % tag)
        if not m.get("image_cached"):
            problems.append("%s: screenshot not cached -- the overlay will be "
                            "blank offline" % tag)
        if not (result.get("xray") or {}).get("regions"):
            problems.append("%s: no regions in the cached x-ray" % tag)
    return entries, problems
