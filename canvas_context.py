import os
import re
import json
import html
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# ─── CONSTANTS ────────────────────────────────────────────────────────────────
# Read from env at import time. No network calls at module level.
BASE_DIR = Path(__file__).parent

_COURSE_ID       = os.environ.get("CHEM_CANVAS_COURSE_ID", "")
_API_BASE        = os.environ.get("CANVAS_API_BASE", "https://canvas.ucsd.edu/api/v1").rstrip("/")
_TTL_HOURS       = int(os.environ.get("CANVAS_CACHE_TTL_HOURS", "6"))
_CACHE_FILE      = os.environ.get("CANVAS_CACHE_FILE", "canvas_cache.json")
_CACHE_PATH      = BASE_DIR / _CACHE_FILE
_COOKIES_PATH    = BASE_DIR / os.environ.get("CANVAS_COOKIES_PATH", "canvas_cookies.json")
_LOCAL_CACHE_DIR = Path(os.environ.get("CANVAS_LOCAL_CACHE_DIR", "~/.claude/canvas-cache")).expanduser()

# Hard cap on context_block to keep token budgets sane (~2000 tokens)
_MAX_CONTEXT_CHARS = 8000

# Title keywords that indicate an assessment (case-insensitive, checked via `in`)
_ASSESSMENT_WORDS = {"quiz", "exam", "midterm", "final", "test"}

# Set True when the server returns 401 or redirects to login; suppresses further requests
_session_expired = False


# ─── HTML STRIPPING ───────────────────────────────────────────────────────────

def _strip_html(text):
    """Remove HTML tags and unescape entities. Mirrors the assistant_b.py pattern."""
    if not text:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", " ", text)).strip()


# ─── CACHE: JSON FILE WITH TTL ────────────────────────────────────────────────

def _load_cache():
    """Load the cache file from disk. Returns empty dict on any error."""
    try:
        if _CACHE_PATH.exists():
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cache(data):
    """Write the cache dict to disk. Swallows all errors — cache is best-effort."""
    try:
        _CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _cache_get(key):
    """Return the stored value for key if it exists and has not expired, else None."""
    try:
        entry = _load_cache().get(key)
        if entry and entry.get("expires_at", 0) > time.time():
            return entry["value"]
    except Exception:
        pass
    return None


def _cache_set(key, value):
    """Store value under key with a TTL expiry timestamp. Swallows all errors."""
    try:
        data = _load_cache()
        data[key] = {
            "value": value,
            "expires_at": time.time() + _TTL_HOURS * 3600,
        }
        _save_cache(data)
    except Exception:
        pass


# ─── HTTP: urllib ONLY, session-cookie auth, pagination ───────────────────────

def _parse_next_link(link_header):
    """
    Extract the 'next' page URL from a Canvas Link response header, or None.
    Canvas format: <url>; rel="next", <url>; rel="last"
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            m = re.match(r'\s*<([^>]+)>', part.strip())
            if m:
                return m.group(1)
    return None


def _load_cookies():
    """
    Read canvas_cookies.json and return a Cookie header string like
    "canvas_session=abc; _csrf_token=xyz", or None if file absent, empty, or any error.
    """
    try:
        if not _COOKIES_PATH.exists():
            return None
        data = json.loads(_COOKIES_PATH.read_text(encoding="utf-8"))
        if not data:
            return None
        return "; ".join(f"{k}={v}" for k, v in data.items())
    except Exception:
        return None


def _ssl_context():
    """
    SSL context for Canvas requests. python.org macOS installs ship without a
    linked system trust store, so prefer certifi's bundle when available.
    """
    try:
        import ssl, certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _strip_json_guard(body):
    """Canvas prefixes cookie-authenticated JSON responses with 'while(1);'."""
    return body[9:] if body.startswith("while(1);") else body


def _api_get(path, params=None):
    """
    GET {_API_BASE}{path} with session-cookie auth.
    Follows Canvas pagination via Link headers automatically.
    Returns parsed JSON (list or dict) or None on any error.
    Sets _session_expired=True on 401 or redirect-to-login responses.
    """
    global _session_expired
    cookie_str = _load_cookies()
    if cookie_str is None:
        return None
    try:
        headers = {"Cookie": cookie_str}
        ctx = _ssl_context()
        base_params = {"per_page": "100"}
        if params:
            base_params.update({str(k): str(v) for k, v in params.items()})
        url = f"{_API_BASE}{path}?" + urllib.parse.urlencode(base_params)

        accumulated = []
        is_list = None

        while url:
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    body = _strip_json_guard(resp.read().decode("utf-8", errors="replace"))
                    resp_url = resp.geturl() if hasattr(resp, "geturl") else ""
                    # Canvas redirects expired sessions to SSO login page
                    if ("login" in body.lower() and "canvas" in body.lower()
                            and "login" in str(resp_url).lower()):
                        print("WARNING: Canvas session appears expired (redirect-to-login detected).")
                        _session_expired = True
                        return None
                    parsed = json.loads(body)

                    if is_list is None:
                        is_list = isinstance(parsed, list)

                    if is_list:
                        accumulated.extend(parsed)
                        url = _parse_next_link(resp.headers.get("Link", ""))
                    else:
                        return parsed

            except urllib.error.HTTPError as e:
                if e.code == 401:
                    print("WARNING: Canvas session cookie returned 401 — session may be expired.")
                    _session_expired = True
                return None

        return accumulated if is_list else None

    except Exception:
        return None


# ─── PUBLIC: CONFIGURATION CHECK ─────────────────────────────────────────────

def is_configured():
    """True iff a cookies file can be loaded OR the local cache directory exists."""
    return _load_cookies() is not None or _LOCAL_CACHE_DIR.exists()


# ─── CANVAS ENDPOINTS (all TTL-cached) ───────────────────────────────────────

def list_assignments(course_id):
    """GET /courses/{id}/assignments — returns list, cached by course_id."""
    key = f"{course_id}:assignments"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = _api_get(f"/courses/{course_id}/assignments") or []
    _cache_set(key, result)
    return result


def get_assignment(course_id, assignment_id):
    """GET /courses/{id}/assignments/{id} — returns dict or None, cached."""
    key = f"{course_id}:assignment:{assignment_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = _api_get(f"/courses/{course_id}/assignments/{assignment_id}")
    if result:
        _cache_set(key, result)
    return result


def list_pages(course_id):
    """GET /courses/{id}/pages — returns list, cached by course_id."""
    key = f"{course_id}:pages"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = _api_get(f"/courses/{course_id}/pages") or []
    _cache_set(key, result)
    return result


def get_page(course_id, url_or_slug):
    """GET /courses/{id}/pages/{url} — returns dict or None, cached."""
    key = f"{course_id}:page:{url_or_slug}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = _api_get(f"/courses/{course_id}/pages/{url_or_slug}")
    if result:
        _cache_set(key, result)
    return result


def list_modules(course_id):
    """GET /courses/{id}/modules — returns list, cached by course_id."""
    key = f"{course_id}:modules"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = _api_get(f"/courses/{course_id}/modules") or []
    _cache_set(key, result)
    return result


# ─── ASSESSMENT DETECTION (safety-critical) ───────────────────────────────────

def _looks_like_assessment(item):
    """
    True if this Canvas item appears to be a quiz or exam.
    Safety-critical: when True, the caller must skip the question.

    Checks (in order):
    1. Item has a truthy quiz_id field (Canvas assigns this for quiz-backed assignments).
    2. submission_types list includes 'online_quiz'.
    3. Title contains any assessment keyword: quiz/exam/midterm/final/test.
    """
    if not item or not isinstance(item, dict):
        return False

    if item.get("quiz_id"):
        return True

    sub_types = item.get("submission_types") or []
    if isinstance(sub_types, list) and "online_quiz" in sub_types:
        return True

    title = (item.get("title") or item.get("name") or "").lower()
    return any(word in title for word in _ASSESSMENT_WORDS)


# ─── SCORING ──────────────────────────────────────────────────────────────────

_ABBREVIATIONS = {"ch": "chapter", "chap": "chapter", "hw": "homework", "wk": "week"}

_CHAPTER_RE = re.compile(r"\bch(?:ap(?:ter)?)?[ _\-]*0*(\d+)\b", re.IGNORECASE)


def _chapter_refs(text):
    """Chapter numbers explicitly referenced in text ('Ch 1', 'chapter_1', 'Chapter 01') → {'1'}."""
    return set(_CHAPTER_RE.findall(text or ""))


def _tokenize(text):
    """
    Lowercase word/number tokens from text. Words need length >= 3. Numbers are
    kept at any length and normalized without leading zeros so 'Ch 1' matches
    'Chapter 01'. Common course abbreviations expand (ch -> chapter, hw -> homework).
    """
    tokens = set()
    for tok in re.findall(r"[a-z]+|\d+", text.lower()):
        if tok.isdigit():
            tokens.add(str(int(tok)))
        elif tok in _ABBREVIATIONS:
            tokens.add(_ABBREVIATIONS[tok])
        elif len(tok) >= 3:
            tokens.add(tok)
    return tokens


def _score(question_text, tags, item):
    """
    Keyword + tag overlap score for ranking a Canvas item against the question.

    Scoring rationale:
    - Title tokens weighted 2x: the title is the most discriminating field
      (e.g., "Chapter 1 Homework" matches "chapter" and "homework" tags strongly).
    - Description/body tokens weighted 1x: broader but still relevant.
    - Tag bonus (1.5x per tag token matching the title): Piazza folder tags
      like 'chapter_1' or 'homework' are strong topic signals.
    - We cap the description scan at 2000 chars to avoid slowing down on very
      large bodies during the scoring pass (full text is fetched only for top hits).

    Returns a float >= 0. Higher is a better match.
    """
    if not item:
        return 0.0
    q_tokens = _tokenize(question_text)
    if not q_tokens:
        return 0.0

    title = item.get("title") or item.get("name") or ""
    desc_raw = item.get("description") or item.get("body") or ""
    desc = _strip_html(desc_raw[:2000])

    title_tokens = _tokenize(title)
    desc_tokens = _tokenize(desc)

    title_overlap = len(q_tokens & title_tokens)
    desc_overlap = len(q_tokens & desc_tokens)

    tag_bonus = 0.0
    if tags:
        tag_tokens = set()
        for t in tags:
            tag_tokens |= _tokenize(t)
        tag_bonus = len(tag_tokens & title_tokens) * 1.5

    # Explicit chapter references beat loose number overlap: a question tagged
    # chapter_1 must prefer "Chapter 01" over "Chapter 3" even when stray numbers
    # in the question body ("3 significant figures", "Item 7") match other titles.
    chapter_adjust = 0.0
    q_chapters = _chapter_refs(question_text) | _chapter_refs(" ".join(tags or []))
    t_chapters = _chapter_refs(title)
    if q_chapters and t_chapters:
        chapter_adjust = 6.0 if (q_chapters & t_chapters) else -4.0

    return title_overlap * 2.0 + desc_overlap + tag_bonus + chapter_adjust


def _assignment_metadata_text(a):
    """
    External-tool assignments (e.g. Macmillan Achieve homework) have empty
    descriptions — the question content lives outside Canvas. Return the
    metadata Canvas does have so the answer model still gets assignment context.
    """
    parts = []
    if a.get("due_at"):
        parts.append(f"Due: {a['due_at']}")
    if a.get("points_possible") is not None:
        parts.append(f"Points possible: {a['points_possible']}")
    if "external_tool" in (a.get("submission_types") or []):
        parts.append("This assignment is hosted on an external tool (e.g. Macmillan Achieve); "
                     "the individual question items are not visible in Canvas.")
    return " | ".join(parts)


# ─── CONTEXT BLOCK BUILDER ────────────────────────────────────────────────────

def _empty_result():
    """Canonical empty/error result dict. All expected keys always present."""
    return {
        "found": False,
        "is_assessment": False,
        "context_block": "",
        "excerpts": [],
    }


def _build_context_block(excerpts):
    """
    Concatenate excerpt text into a single context block.
    Hard-bounded to _MAX_CONTEXT_CHARS. Never raises.
    """
    parts = []
    total = 0
    for ex in excerpts:
        header = f"[{ex['type'].upper()}] {ex['title']}\n"
        body = ex["text"]
        budget = _MAX_CONTEXT_CHARS - total
        if budget <= len(header) + 10:
            break
        chunk = header + body + "\n\n"
        if len(chunk) > budget:
            # Truncate body to fit; reserve room for ellipsis and separator
            body_budget = budget - len(header) - 5
            chunk = header + body[:body_budget] + "...\n\n"
        parts.append(chunk)
        total += len(chunk)
        if total >= _MAX_CONTEXT_CHARS:
            break
    return "".join(parts)[:_MAX_CONTEXT_CHARS].strip()


# ─── LOCAL CACHE FALLBACK ────────────────────────────────────────────────────

def _search_local_cache(question, tags):
    """
    Scan _LOCAL_CACHE_DIR for subdirectories matching "CHEM_11" or "CHEM11"
    (case-insensitive) and score .txt files within them against question/tags.

    Returns list of excerpt dicts (same shape as live Canvas excerpts), or []
    on any error. Never raises.
    """
    try:
        if not _LOCAL_CACHE_DIR.exists():
            return []

        scored = []
        for subdir in _LOCAL_CACHE_DIR.iterdir():
            if not subdir.is_dir():
                continue
            name_lower = subdir.name.lower()
            if "chem_11" not in name_lower and "chem11" not in name_lower:
                continue
            for txt_file in subdir.glob("*.txt"):
                try:
                    content = txt_file.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                title = txt_file.stem
                pseudo_item = {
                    "title": title,
                    "name": title,
                    "description": content[:2000],
                }
                s = _score(question, tags, pseudo_item)
                if s > 0:
                    scored.append((s, txt_file, title))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:3]

        excerpts = []
        for _, filepath, title in top:
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            excerpts.append({
                "title": title,
                "type": "file",
                "text": content[:_MAX_CONTEXT_CHARS],
                "url": str(filepath),
            })
        return excerpts

    except Exception:
        return []


# ─── PUBLIC: RETRIEVE ─────────────────────────────────────────────────────────

def retrieve(question_text, tags=None, course_id=None):
    """
    Find the best-matching Canvas course material for question_text + tags.

    Tries live Canvas first (if session cookies present and not expired), then
    falls back to local file cache. Always returns a complete dict and never
    raises. On unconfigured state, network error, or no match, returns the
    empty-result dict.

    Return shape:
    {
        "found": bool,
        "is_assessment": bool,       # True if best match is a quiz/exam — caller must skip
        "context_block": str,        # bounded to ~8000 chars, "" when not found
        "excerpts": [{"title": str, "type": "assignment|page|file", "text": str, "url": str}]
    }
    """
    try:
        return _retrieve_inner(question_text, tags, course_id)
    except Exception:
        return _empty_result()


def _retrieve_inner(question_text, tags, course_id):
    """Inner retrieval logic. Always called through retrieve() which catches all exceptions."""
    if not is_configured():
        return _empty_result()

    # Try live Canvas only if session cookies are available and not expired
    if _load_cookies() is not None and not _session_expired:
        resolved_course_id = course_id or _COURSE_ID
        if resolved_course_id:
            candidates = []

            for item in (list_assignments(resolved_course_id) or []):
                s = _score(question_text, tags, item)
                if s > 0:
                    candidates.append({"_type": "assignment", "_item": item, "_score": s})

            for item in (list_pages(resolved_course_id) or []):
                s = _score(question_text, tags, item)
                if s > 0:
                    candidates.append({"_type": "page", "_item": item, "_score": s})

            if candidates:
                candidates.sort(key=lambda c: c["_score"], reverse=True)
                top = candidates[:3]

                excerpts = []
                is_assessment = False

                for cand in top:
                    item = cand["_item"]
                    ctype = cand["_type"]

                    if ctype == "assignment":
                        full = get_assignment(resolved_course_id, item.get("id", "")) or item
                        text = _strip_html(full.get("description") or "")
                        if not text:
                            text = _assignment_metadata_text(full)
                        title = full.get("name") or full.get("title") or ""
                        url = full.get("html_url") or ""

                    elif ctype == "page":
                        slug = item.get("url") or item.get("page_id") or ""
                        if not slug:
                            continue
                        full = get_page(resolved_course_id, slug) or item
                        text = _strip_html(full.get("body") or "")
                        title = full.get("title") or ""
                        url = full.get("html_url") or ""

                    else:
                        continue

                    # Assessment check: the top-ranked match is what the question
                    # is about — if it's an assessment, the caller must skip the
                    # question. Lower-ranked assessment items are merely nearby
                    # material: drop them so quiz/exam content never reaches the
                    # answer model, but don't veto the whole question.
                    if _looks_like_assessment(full):
                        if cand is top[0]:
                            is_assessment = True
                        continue

                    # Binary/non-text items have no extractable text — skip gracefully
                    if not text:
                        continue

                    excerpts.append({
                        "title": title,
                        "type": ctype,
                        "text": text,
                        "url": url,
                    })

                if excerpts or is_assessment:
                    return {
                        "found": True,
                        "is_assessment": is_assessment,
                        "context_block": _build_context_block(excerpts),
                        "excerpts": excerpts,
                    }

    # Fallback: scan local file cache
    local_excerpts = _search_local_cache(question_text, tags)
    if local_excerpts:
        # Same top-match semantics as the live path: only the best match vetoes;
        # lower-ranked assessment files are dropped from the context instead.
        is_assessment = _looks_like_assessment({"name": local_excerpts[0]["title"]})
        local_excerpts = [
            ex for ex in local_excerpts
            if not _looks_like_assessment({"name": ex["title"]})
        ]
        return {
            "found": True,
            "is_assessment": is_assessment,
            "context_block": _build_context_block(local_excerpts),
            "excerpts": local_excerpts,
        }

    return _empty_result()
