"""Defensive JSON extraction from free-form model text.

We assume the model does NOT support a structured-output mode, so the reply can
be prose, or fenced, or fenced-and-truncated. Everything here is about getting
an object out of that without raising.
"""
import json
import re

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _balanced_span(text, open_ch="{", close_ch="}"):
    """Return (start, end) of the first balanced object/array, or None.

    String-aware: braces inside JSON string literals do not count, and \\"
    escapes are handled, so a brace in an error message cannot unbalance us.
    """
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == open_ch:
            if depth == 0:
                start = i
            depth += 1
        elif ch == close_ch:
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return start, i + 1
    return None


def extract_json(text, want="object"):
    """Best-effort parse of a JSON object (or array) out of `text`.

    Returns the parsed value, or None. Never raises.
    """
    if not text:
        return None
    if not isinstance(text, str):
        return text

    candidates = []
    for m in _FENCE.finditer(text):
        candidates.append(m.group(1))
    candidates.append(text)

    pairs = [("{", "}")] if want == "object" else [("[", "]")]
    # If the caller is flexible, try both shapes.
    if want == "any":
        pairs = [("{", "}"), ("[", "]")]

    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            return json.loads(cand)
        except Exception:
            pass
        for o, c in pairs:
            span = _balanced_span(cand, o, c)
            if not span:
                continue
            chunk = cand[span[0]:span[1]]
            try:
                return json.loads(chunk)
            except Exception:
                # Last resort: trailing commas are the usual culprit.
                cleaned = re.sub(r",(\s*[}\]])", r"\1", chunk)
                try:
                    return json.loads(cleaned)
                except Exception:
                    continue
    return None
