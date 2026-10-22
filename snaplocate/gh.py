"""Stage 1: FETCH -- GitHub issue, comments and the first image. urllib only.

Unauthenticated the REST API allows 60 requests/hour per IP, which a demo will
burn through fast. Set GITHUB_TOKEN to get 5000.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UA = "SnapLocate/0.1 (stdlib urllib)"

ISSUE_URL = re.compile(
    r"github\.com/([^/\s]+)/([^/\s]+)/(?:issues|pull)/(\d+)", re.IGNORECASE
)

# Markdown image, HTML <img>, and bare attachment links. GitHub serves uploads
# from user-images.githubusercontent.com and (newer) github.com/user-attachments.
MD_IMG = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?\s*\)")
HTML_IMG = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
BARE_IMG = re.compile(
    r"https?://[^\s)<>\"']+?"
    r"(?:\.(?:png|jpe?g|gif|webp)|user-attachments/assets/[\w-]+)",
    re.IGNORECASE,
)


class GitHubError(RuntimeError):
    pass


def parse_issue_url(url):
    """-> (owner, repo, number). Raises GitHubError on anything unrecognised."""
    m = ISSUE_URL.search(url or "")
    if not m:
        raise GitHubError(
            "not a GitHub issue URL: %r (expected "
            "github.com/owner/repo/issues/123)" % (url,))
    owner, repo, num = m.group(1), m.group(2), int(m.group(3))
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo, num


def _headers():
    h = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def _get_json(path, timeout=30):
    url = path if path.startswith("http") else API + path
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code == 403:
            hint = (" -- rate limited? set GITHUB_TOKEN for 5000 req/hour "
                    "instead of 60")
        elif exc.code == 404:
            hint = " -- private repo, or wrong owner/repo/number"
        raise GitHubError("HTTP %s for %s%s" % (exc.code, url, hint))
    except Exception as exc:
        raise GitHubError("%s fetching %s: %s" % (type(exc).__name__, url, exc))


def extract_image_urls(*texts):
    """Image URLs in markdown order, de-duplicated, markdown/HTML/bare."""
    out, seen = [], set()
    for t in texts:
        if not t:
            continue
        for rx in (MD_IMG, HTML_IMG, BARE_IMG):
            for m in rx.finditer(t):
                u = m.group(1) if rx is not BARE_IMG else m.group(0)
                u = u.strip().rstrip(".,;")
                if not u.lower().startswith(("http://", "https://")):
                    continue
                if u in seen:
                    continue
                seen.add(u)
                out.append(u)
    return out


def fetch_issue(url, with_comments=True):
    """Issue title, body, comments and image URLs."""
    owner, repo, num = parse_issue_url(url)
    issue = _get_json("/repos/%s/%s/issues/%d" % (owner, repo, num))

    comments = []
    if with_comments and issue.get("comments"):
        try:
            for c in _get_json("/repos/%s/%s/issues/%d/comments"
                               % (owner, repo, num)):
                comments.append({"user": (c.get("user") or {}).get("login"),
                                 "body": c.get("body") or ""})
        except GitHubError:
            pass  # comments are a nice-to-have; the issue body is the payload

    body = issue.get("body") or ""
    images = extract_image_urls(body, *[c["body"] for c in comments])

    return {
        "owner": owner, "repo": repo, "number": num,
        "url": issue.get("html_url") or url,
        "title": issue.get("title") or "",
        "body": body,
        "state": issue.get("state"),
        "comments": comments,
        "image_urls": images,
    }


def download_image(url, timeout=60, max_bytes=12_000_000):
    """-> (bytes, mime). Refuses non-images and anything oversized."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            mime = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            data = r.read(max_bytes + 1)
    except Exception as exc:
        raise GitHubError("could not download image %s: %s" % (url, exc))
    if len(data) > max_bytes:
        raise GitHubError("image larger than %d bytes: %s" % (max_bytes, url))
    if not data:
        raise GitHubError("empty image: %s" % url)
    if mime and not mime.startswith("image/"):
        # GitHub attachment links redirect and can report octet-stream; sniff.
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise GitHubError("not an image (%s): %s" % (mime, url))
    return data, mime or "image/png"


def pr_base(owner, repo, number):
    """-> {'base_sha', 'base_ref', 'merged', 'merge_commit', 'changed_files'}.

    `base_sha` is the commit the PR branched from -- the commit the eval must
    run against. Equivalent to:
        gh pr view <n> --repo owner/repo --json baseRefOid
    """
    pr = _get_json("/repos/%s/%s/pulls/%d" % (owner, repo, number))
    base = pr.get("base") or {}
    return {
        "number": number,
        "base_sha": base.get("sha"),
        "base_ref": base.get("ref"),
        "merged": bool(pr.get("merged")),
        "merge_commit": pr.get("merge_commit_sha"),
        "title": pr.get("title") or "",
    }


def pr_changed_files(owner, repo, number, max_pages=10):
    """Repo-relative paths the PR changed. Paginated; 100 per page."""
    out = []
    for page in range(1, max_pages + 1):
        batch = _get_json("/repos/%s/%s/pulls/%d/files?per_page=100&page=%d"
                          % (owner, repo, number, page))
        if not batch:
            break
        for f in batch:
            if f.get("filename"):
                out.append(f["filename"])
        if len(batch) < 100:
            break
    return out


def linked_pr_numbers(owner, repo, issue_number):
    """PR numbers that reference this issue, best-effort.

    *** UNTESTED. Nothing in this project calls it yet and it has not been run
    against a real issue. Check what it returns before relying on it. ***

    GitHub exposes no clean 'the PR that closed this issue' field on the REST
    API, so this reads the issue timeline and picks out cross-references. It
    can miss a link and it can return more than one -- treat the result as
    candidates to check by hand, not an answer. The timeline endpoint is also
    the slowest way to get this; if you have `gh`, this is faster and better:
        gh issue view <n> --repo owner/repo --json closedByPullRequestsReferences
    """
    try:
        events = _get_json(
            "/repos/%s/%s/issues/%d/timeline?per_page=100"
            % (owner, repo, issue_number))
    except GitHubError:
        return []
    nums = []
    for e in events or []:
        src = (e.get("source") or {}).get("issue") or {}
        if src.get("pull_request") and src.get("number"):
            nums.append(src["number"])
        commit_id = e.get("commit_id")
        if e.get("event") == "closed" and commit_id:
            continue
    seen, out = set(), []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def issue_context(issue):
    """Flatten an issue into the text block handed to the model."""
    parts = ["TITLE: " + issue.get("title", ""), "", issue.get("body", "")]
    for c in issue.get("comments", [])[:5]:
        parts += ["", "COMMENT by %s:" % c.get("user"), c.get("body", "")]
    return "\n".join(parts)
