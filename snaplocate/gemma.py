"""Stage 2 (XRAY) and stage 6 (RERANK): the Gemma 4 calls.

Assumptions baked in here, per the brief:
  - no structured-output mode  -> we parse JSON out of free text (jsonx)
  - no system instructions     -> everything goes in the user turn
  - image input supported      -> inline base64 part

THE BOX CONVENTION IS NOT VERIFIED.
BOX_ORDER/BOX_SCALE below are the single source of truth. The web UI does NOT
hard-code them -- it reads them off the API response (`box_convention`) and
transforms accordingly. So if the convention turns out wrong, you change it
HERE ONLY and the overlay follows. Run `python -m snaplocate.cli verify-boxes`
to check it against a real screenshot before trusting the overlay.
"""
import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request

from . import config
from .jsonx import extract_json

# ---------------------------------------------------------------------------
# VERIFIED 2026-10-22 against gemma-4-26b-a4b-it on a screenshot whose element
# positions were measured from the DOM first, so there was a ground truth to
# compare against (examples/buggy-checkout.html at 900x640):
#
#     string                        measured (y,x,y,x)   model returned
#     "Coudn't complete yor order"  235, 53, 270, -      241, 53, 266, 257
#     "Order total"                 534, 54, 573, 152    541, 55, 567, 158
#     "Retry payment"               642, 31, 704, 172    644, 34, 704, 173
#
# Agreement within ~7/1000 on every edge. (The error row's xmax differs only
# because the measurement took the full-width container while the model took
# the text itself -- the model was the more useful of the two.)
#
# Re-check with `cli verify-boxes` if you change model: this was confirmed for
# 26b-a4b only, NOT for gemma-4-31b-it.
BOX_ORDER = ["ymin", "xmin", "ymax", "xmax"]
BOX_SCALE = 1000          # coordinates are 0..BOX_SCALE, not pixels
BOX_VERIFIED_ON = "gemma-4-26b-a4b-it"
# ---------------------------------------------------------------------------


def box_convention(model=None):
    """Shipped to the frontend so the overlay never guesses.

    `verified` is true only for the model the convention was actually checked
    against. Any other model reports false -- an unchecked assumption should
    not inherit another model's evidence.
    """
    used = model or os.environ.get("SNAPLOCATE_MODEL") or config.MODEL_DEFAULT
    return {"order": BOX_ORDER, "scale": BOX_SCALE,
            "verified": used == BOX_VERIFIED_ON,
            "verified_on": BOX_VERIFIED_ON}


def box_to_fractions(box):
    """Convert a model box to (left, top, width, height) as 0..1 fractions.

    Returns None if the box is unusable. Clamps to the image and tolerates
    swapped min/max, which models do emit.
    """
    if not isinstance(box, (list, tuple)) or len(box) < 4:
        return None
    try:
        vals = [float(v) for v in box[:4]]
    except (TypeError, ValueError):
        return None

    named = dict(zip(BOX_ORDER, vals))
    try:
        ymin, xmin = named["ymin"], named["xmin"]
        ymax, xmax = named["ymax"], named["xmax"]
    except KeyError:
        return None

    if ymax < ymin:
        ymin, ymax = ymax, ymin
    if xmax < xmin:
        xmin, xmax = xmax, xmin

    s = float(BOX_SCALE)
    left, top = xmin / s, ymin / s
    w, h = (xmax - xmin) / s, (ymax - ymin) / s

    left, top = max(0.0, min(1.0, left)), max(0.0, min(1.0, top))
    w, h = max(0.0, min(1.0 - left, w)), max(0.0, min(1.0 - top, h))
    if w <= 0 or h <= 0:
        return None
    return {"left": left, "top": top, "width": w, "height": h}


class GemmaError(RuntimeError):
    pass


def api_key():
    return os.environ.get("GEMMA_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _post(model, payload, key, timeout=None):
    url = "%s/%s:generateContent" % (config.API_BASE, model)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    with urllib.request.urlopen(req, timeout=timeout or config.API_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _text_of(resp):
    """Pull the concatenated text out of a generateContent response."""
    try:
        parts = resp["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def call_model(parts, model=None, key=None, retries=1, timeout=None,
               budget=None, max_tokens=None):
    """One generateContent call. Raises GemmaError on definitive failure."""
    key = key or api_key()
    if not key:
        raise GemmaError("no API key: set GEMMA_API_KEY")
    model = model or os.environ.get("SNAPLOCATE_MODEL") or config.MODEL_DEFAULT

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        # Deterministic-ish: we want the same screenshot to give the same
        # regions across runs so the eval is not measuring sampling noise.
        # Gemma emits a markdown preamble before the JSON even when told not
        # to, so the budget must cover reasoning AND the object or the JSON
        # truncates. But generation time scales with this number -- raising it
        # globally to 8192 made the x-ray time out on a real screenshot. Each
        # caller sets what it actually needs.
        "generationConfig": {"temperature": 0.0,
                             "maxOutputTokens": max_tokens
                             or config.MAX_TOKENS_DEFAULT},
    }

    # HARD WALL-CLOCK DEADLINE.
    #
    # MEASURED 2026-10-22: a rerank call ran for 3127 SECONDS (52 minutes)
    # before failing. urlopen's `timeout` is a per-socket-operation timeout,
    # not a total one -- a server that trickles bytes, or a stalled TLS
    # handshake, resets it indefinitely. On a demo clock that is fatal, and
    # "it hung" is the one failure the cache cannot rescue because nothing
    # ever returns.
    #
    # So every call gets a total budget. When it is spent we give up and
    # raise, and the caller degrades (search-only ranking, or a cache replay)
    # instead of hanging. A slow answer is worth less than a prompt failure.
    deadline = time.monotonic() + (budget or config.API_BUDGET)
    last = None
    attempt = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            raise last or GemmaError(
                "gave up after %.0fs without a reply from %s (wall-clock "
                "budget exhausted)" % (budget or config.API_BUDGET, model))
        try:
            # never let one socket wait outlast the whole budget
            resp = _post(model, payload, key, min(timeout or config.API_TIMEOUT,
                                                  remaining))
            return _text_of(resp), model
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            if exc.code == 429:
                # The free tier caps requests per minute and one issue costs
                # two calls (x-ray + rerank), so this is the failure you
                # actually hit on stage.
                last = GemmaError(
                    "RATE LIMITED (HTTP 429) by %s. Each issue costs two "
                    "calls; the free tier caps requests per minute. Wait, or "
                    "replay from the cache. %s" % (model, detail[:200]))
            else:
                last = GemmaError("HTTP %s from %s: %s"
                                  % (exc.code, model, detail))
                if 400 <= exc.code < 500:
                    break      # other 4xx will not fix themselves
        except Exception as exc:
            last = GemmaError("%s: %s" % (type(exc).__name__, exc))

        attempt += 1
        if attempt > retries:
            break
        nap = min(1.5 * (2 ** (attempt - 1)), deadline - time.monotonic() - 1.0)
        if nap <= 0:
            break
        time.sleep(nap)

    raise last or GemmaError("unknown failure")


# -- stage 2: XRAY ----------------------------------------------------------

XRAY_PROMPT = """You are reading a screenshot attached to a software bug report.

Return ONLY a JSON object, no prose, no markdown fence, in exactly this shape:

{
  "surface": "short name for what part of the app this is",
  "symptom": "one sentence: what looks wrong",
  "regions": [
    {
      "id": "r1",
      "text": "the text EXACTLY as it appears on screen",
      "kind": "error|label|button|menu|field|heading|url|code|value|other",
      "box": [ymin, xmin, ymax, xmax],
      "suspect": true,
      "why": "why this string would appear in the source"
    }
  ]
}

RULES, all of them matter:
- "text" MUST be copied VERBATIM from the pixels. Do not translate it. Do not
  fix spelling, casing, spacing or punctuation. Do not expand abbreviations. If
  the screen says "Coudn't conect", you write "Coudn't conect". These strings
  are going to be grepped against source code, so a "corrected" string is a
  string that matches nothing.
- Box coordinates are integers 0-1000, relative to the image, in the order
  [ymin, xmin, ymax, xmax].
- "suspect": true only for text that looks like it comes from the broken code
  path -- error messages, wrong values, failed states. Chrome, nav labels and
  decoration are false.
- At most %d regions. Prefer long, distinctive, greppable strings over short
  generic words. Skip anything under 4 characters.
- If you cannot read the image, return {"surface":"","symptom":"","regions":[]}.

Bug report context (use it to judge what is suspect, do NOT take strings from
it -- strings must come from the image):
%s

OUTPUT FORMAT, AND THIS IS THE PART THAT MATTERS MOST:
Your entire reply must be the JSON object and nothing else. Begin your reply
with the character { and end it with the character }. Do NOT think out loud,
do NOT restate the task, do NOT write "The user wants...", do NOT number your
reasoning steps, do NOT wrap it in a markdown fence. Any text before the { is
a failure. Start writing { now."""

# Appended on a retry when the first reply was unparseable. Gemma reasons out
# loud by default and ignores a polite "return only JSON"; this was measured
# burning the whole output budget on a preamble, leaving no JSON at all.
JSON_ONLY_NUDGE = """

PREVIOUS ATTEMPT FAILED: you wrote prose instead of JSON, and the reply was
cut off before any JSON appeared. Output the raw JSON object ONLY. The first
character you emit must be {."""


def xray(image_bytes, image_mime=None, context="", model=None, key=None):
    """Send the screenshot to Gemma. Returns a dict with regions.

    Raises GemmaError on API failure -- the caller decides whether to degrade.
    """
    mime = image_mime or "image/png"
    prompt = XRAY_PROMPT % (config.MAX_REGIONS, (context or "")[:4000])
    parts = [
        {"text": prompt},
        {"inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(image_bytes).decode("ascii"),
        }},
    ]
    text, used = call_model(parts, model=model, key=key,
                            max_tokens=config.MAX_TOKENS_XRAY,
                            budget=config.API_BUDGET)
    data = extract_json(text, want="object")

    if not isinstance(data, dict):
        # One hard retry. Measured on gemma-4-26b-a4b-it with a real Flask
        # screenshot: the model spent the entire output budget narrating
        # ("The user wants me to analyze a screenshot...") and emitted no JSON
        # at all. Re-asking with an explicit "you failed, JSON only" is
        # cheaper and far more reliable than raising the token ceiling, which
        # just buys a longer preamble and risks the wall-clock budget.
        retry_parts = [{"text": prompt + JSON_ONLY_NUDGE}] + parts[1:]
        text, used = call_model(retry_parts, model=model, key=key,
                                max_tokens=config.MAX_TOKENS_XRAY,
                                budget=config.API_BUDGET, retries=0)
        data = extract_json(text, want="object")

    if not isinstance(data, dict):
        raise GemmaError("could not parse JSON from model reply after a "
                         "retry: %r" % (text[:300],))

    regions = data.get("regions")
    if not isinstance(regions, list):
        regions = []

    clean = []
    for i, r in enumerate(regions[: config.MAX_REGIONS]):
        if not isinstance(r, dict):
            continue
        txt = r.get("text")
        if not isinstance(txt, str) or not txt.strip():
            continue
        clean.append({
            "id": str(r.get("id") or "r%d" % (i + 1)),
            "text": txt,
            "kind": r.get("kind") or "other",
            "box": r.get("box"),
            "box_frac": box_to_fractions(r.get("box")),
            "suspect": bool(r.get("suspect")),
            "why": r.get("why") or "",
        })

    return {
        "surface": data.get("surface") or "",
        "symptom": data.get("symptom") or "",
        "regions": clean,
        "model": used,
        "box_convention": box_convention(used),
        "raw_text": text[:2000],
    }


def guess_mime(path_or_url, default="image/png"):
    mime, _ = mimetypes.guess_type(path_or_url.split("?")[0])
    if mime and mime.startswith("image/"):
        return mime
    return default


# -- stage 6: RERANK --------------------------------------------------------

RERANK_PROMPT = """You are ranking candidate source files for a bug report.

Bug surface: %s
Symptom: %s

On-screen strings extracted from the screenshot:
%s

Candidate files, with the evidence that put them on this list:
%s

Return ONLY a JSON object, no prose, no markdown fence:

{
  "top": [
    {"path": "exact path from the candidate list", "reason": "one sentence"}
  ],
  "call_path": ["file or file::symbol", "..."],
  "confidence": "high|medium|low"
}

RULES:
- "path" MUST be copied exactly from the candidate list above. Do NOT invent a
  path, do NOT guess a file that is not listed, do NOT fix a path you think is
  wrong. A path not in the list will be discarded.
- Return at most %d entries in "top", best first.
- "call_path" is the likely route from entry point to the failure, using only
  listed files. Empty list if you cannot tell.
- Base the reasoning on the evidence given, not on assumptions about what a
  project with this name usually looks like.
"""


def rerank(xray_result, candidates, model=None, key=None):
    """Hand the shortlist to Gemma for a top-3 plus a call path.

    Paths not present in `candidates` are DISCARDED, so a hallucinated file can
    never reach the output. Raises GemmaError; the caller degrades.
    """
    shortlist = candidates[: config.RERANK_SHORTLIST]
    allowed = {c["path"] for c in shortlist}

    region_lines = "\n".join(
        "- %s%s" % (json.dumps(r.get("text", "")),
                    "  [suspect]" if r.get("suspect") else "")
        for r in xray_result.get("regions", [])
    ) or "(none)"

    cand_lines = []
    for c in shortlist:
        ev = c.get("evidence") or []
        bits = ", ".join(
            "%s (match %.1f, in %d files)"
            % (json.dumps(e.get("text", ""))[:80], e.get("exactness", 0),
               e.get("df", 0))
            for e in ev[:4]
        ) or "graph proximity only"
        cand_lines.append(
            "- %s | score %.3f (search %.3f, graph %.3f) | %s"
            % (c["path"], c.get("score", 0), c.get("seed", 0),
               c.get("graph", 0), bits)
        )

    prompt = RERANK_PROMPT % (
        xray_result.get("surface", ""), xray_result.get("symptom", ""),
        region_lines, "\n".join(cand_lines), config.RERANK_TOPK,
    )

    text, used = call_model([{"text": prompt}], model=model, key=key,
                            max_tokens=config.MAX_TOKENS_RERANK,
                            budget=config.API_BUDGET_RERANK)
    data = extract_json(text, want="object")
    if not isinstance(data, dict):
        raise GemmaError("could not parse rerank JSON: %r" % (text[:300],))

    # Resolve a model-written path back to a shortlist path.
    #
    # Observed on a real gemma-4-26b-a4b-it run: the model echoes paths in its
    # prose wrapped in backticks, and sometimes carries that formatting into
    # the JSON, or prefixes the repo directory. A strict `p in allowed` then
    # drops a CORRECT answer -- we saw it discard the right file and fall back
    # to the fused ranking. Normalising both sides fixes that WITHOUT widening
    # what is admissible: the result is always a path from the shortlist, so a
    # genuinely invented file still cannot get through.
    def canon(s):
        s = str(s or "").strip().strip("`'\"").replace("\\", "/").strip()
        while s.startswith("./"):
            s = s[2:]
        return s.lower()

    lookup = {}
    for c in shortlist:
        lookup[canon(c["path"])] = c["path"]

    def resolve(p):
        if p in allowed:                       # exact, the common case
            return p
        n = canon(p)
        if n in lookup:
            return lookup[n]
        for cn, orig in lookup.items():        # repo prefix, or a sub-path
            if n.endswith("/" + cn) or cn.endswith("/" + n):
                return orig
        return None

    top, dropped = [], []
    for item in (data.get("top") or [])[: config.RERANK_TOPK]:
        if not isinstance(item, dict):
            continue
        p = resolve(item.get("path"))
        if p:
            top.append({"path": p, "reason": item.get("reason") or ""})
        elif item.get("path"):
            dropped.append(item.get("path"))

    call_path = []
    for p in (data.get("call_path") or []):
        if not isinstance(p, str):
            continue
        head, _, sym = p.partition("::")
        r = resolve(head)
        if r:
            call_path.append(r + ("::" + sym if sym else ""))

    return {
        "top": top,
        "call_path": call_path,
        "confidence": data.get("confidence") or "unknown",
        "hallucinated_paths_dropped": dropped,
        "model": used,
    }
