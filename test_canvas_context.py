"""
Tests for canvas_context.py.
Run with: python -m pytest test_canvas_context.py -v
All tests use unittest.mock — no live network calls.
"""
import json
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

# ─── Configure env before importing the module under test ─────────────────────
# Use a temp file for the cache so tests never touch canvas_cache.json.
_TEMP_CACHE = tempfile.mktemp(suffix="_test_canvas_cache.json")

os.environ["CHEM_CANVAS_COURSE_ID"] = "12345"
os.environ["CANVAS_API_BASE"] = "https://canvas.ucsd.edu/api/v1"
os.environ["CANVAS_CACHE_FILE"] = _TEMP_CACHE
os.environ["CANVAS_CACHE_TTL_HOURS"] = "6"

import canvas_context  # noqa: E402  (must come after env setup)

# Override the module's cache path to the temp file (handles absolute-path edge case)
canvas_context._CACHE_PATH = Path(_TEMP_CACHE)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _write_temp_cookies(path, data=None):
    """Write a cookies JSON file to path for use in tests."""
    if data is None:
        data = {"canvas_session": "test-session"}
    Path(path).write_text(json.dumps(data), encoding="utf-8")


# ─── MOCK FIXTURES ────────────────────────────────────────────────────────────

MOCK_ASSIGNMENTS = [
    {
        "id": 1,
        "name": "Chapter 1 Homework",
        "description": (
            "<p>Complete problems 1-10 on Fahrenheit temperature conversions "
            "and significant figures.</p>"
        ),
        "html_url": "https://canvas.ucsd.edu/courses/12345/assignments/1",
        "submission_types": ["online_text_entry"],
        "quiz_id": None,
    },
    {
        "id": 2,
        "name": "Week 2 Lab Report",
        "description": "<p>Write a lab report on the density experiment.</p>",
        "html_url": "https://canvas.ucsd.edu/courses/12345/assignments/2",
        "submission_types": ["online_upload"],
        "quiz_id": None,
    },
]

MOCK_QUIZ_ASSIGNMENT = {
    "id": 3,
    "name": "Midterm Exam Review Quiz",
    "description": "<p>Practice problems for the midterm.</p>",
    "html_url": "https://canvas.ucsd.edu/courses/12345/assignments/3",
    "submission_types": ["online_quiz"],
    "quiz_id": 99,
}

MOCK_PAGES = [
    {
        "url": "chapter-1-notes",
        "title": "Chapter 1 Notes",
        "body": None,  # Canvas list response does not embed body
        "html_url": "https://canvas.ucsd.edu/courses/12345/pages/chapter-1-notes",
    },
]

MOCK_PAGE_FULL = {
    "url": "chapter-1-notes",
    "title": "Chapter 1 Notes",
    "body": (
        "<p>Chapter 1 covers matter and measurements. "
        "Significant figures rules apply to all measurements.</p>"
    ),
    "html_url": "https://canvas.ucsd.edu/courses/12345/pages/chapter-1-notes",
}


def _mock_response(body):
    """Build a context-manager mock mimicking urllib.request.urlopen response."""
    mock = MagicMock()
    mock.read.return_value = json.dumps(body).encode("utf-8")
    mock.headers.get.return_value = ""  # no Link header → no pagination
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _make_url_dispatcher(assignments, pages=None, pages_full=None):
    """
    Return a side_effect function that routes urllib calls to the right fixture
    based on the URL path. assignments is the list fixture; individual fetches
    are resolved by id from that same list.
    """
    pages = pages if pages is not None else MOCK_PAGES
    pages_full = pages_full if pages_full is not None else MOCK_PAGE_FULL

    def side_effect(req, timeout=None, context=None):
        url = req.full_url
        path = url.split("?")[0]  # strip query string for routing

        # Individual assignment fetch: .../assignments/<id>
        if re.search(r"/assignments/(\d+)$", path):
            m = re.search(r"/assignments/(\d+)$", path)
            aid = int(m.group(1))
            item = next((a for a in assignments if a.get("id") == aid), {})
            return _mock_response(item)

        # Assignment list: .../assignments
        if path.endswith("/assignments"):
            return _mock_response(assignments)

        # Individual page fetch: .../pages/<slug>
        if re.search(r"/pages/[^/]+$", path) and not path.endswith("/pages"):
            return _mock_response(pages_full)

        # Page list: .../pages
        if path.endswith("/pages"):
            return _mock_response(pages)

        return _mock_response([])

    return side_effect


# ─── TEST CASES ───────────────────────────────────────────────────────────────

class TestCanvasContext(unittest.TestCase):

    def setUp(self):
        """Reset module state and clear the cache file before each test."""
        canvas_context._session_expired = False
        canvas_context._COURSE_ID = "12345"
        canvas_context._CACHE_PATH = Path(_TEMP_CACHE)
        # Point to nonexistent paths so is_configured() returns False by default;
        # individual tests opt in by patching _load_cookies or overriding these.
        canvas_context._COOKIES_PATH = Path("/no-such-file/canvas_cookies.json")
        canvas_context._LOCAL_CACHE_DIR = Path("/no-such-dir/canvas-cache")
        Path(_TEMP_CACHE).unlink(missing_ok=True)

    def tearDown(self):
        Path(_TEMP_CACHE).unlink(missing_ok=True)

    # ── Scenario 1: Happy path — retrieve matches and ranks the right assignment

    @patch("canvas_context._load_cookies", return_value="canvas_session=test-session")
    @patch("urllib.request.urlopen")
    def test_happy_path_retrieves_and_ranks(self, mock_urlopen, mock_load_cookies):
        """retrieve() returns the best-matching assignment, carrying Cookie header not Authorization."""
        captured = []
        base_dispatch = _make_url_dispatcher(MOCK_ASSIGNMENTS)

        def capturing_side_effect(req, timeout=None, context=None):
            captured.append(req)
            return base_dispatch(req, timeout=timeout)

        mock_urlopen.side_effect = capturing_side_effect

        result = canvas_context.retrieve(
            "What is the Fahrenheit temperature conversion with significant figures?",
            tags=["chapter_1", "homework"],
            course_id="12345",
        )

        self.assertTrue(result["found"], "Expected found=True for a matching assignment")
        self.assertFalse(result["is_assessment"])
        self.assertIsInstance(result["context_block"], str)
        self.assertGreater(len(result["context_block"]), 0)
        self.assertGreater(len(result["excerpts"]), 0)

        # Chapter 1 Homework must rank first — its title should appear in context_block
        self.assertIn("Chapter 1 Homework", result["context_block"])

        # Verify the excerpt fields are complete
        first = result["excerpts"][0]
        self.assertIn("title", first)
        self.assertIn("type", first)
        self.assertIn("text", first)
        self.assertIn("url", first)

        # Verify Cookie header used, Authorization header absent
        self.assertGreater(len(captured), 0)
        req = captured[0]
        self.assertIsNotNone(req.get_header("Cookie"))
        self.assertIsNone(req.get_header("Authorization"))

    # ── Scenario 2: context_block is bounded to ~8000 chars

    @patch("canvas_context._load_cookies", return_value="canvas_session=test-session")
    @patch("urllib.request.urlopen")
    def test_context_block_bounded(self, mock_urlopen, mock_load_cookies):
        """context_block never exceeds _MAX_CONTEXT_CHARS even with a 50k-char assignment."""
        big_text = "chemistry stoichiometry " * 2100  # ~50 000 chars
        big_assignment = {
            "id": 10,
            "name": "Very Long Chemistry Assignment",
            "description": f"<p>{big_text}</p>",
            "html_url": "https://canvas.ucsd.edu/courses/12345/assignments/10",
            "submission_types": ["online_text_entry"],
            "quiz_id": None,
        }
        mock_urlopen.side_effect = _make_url_dispatcher(
            [big_assignment], pages=[], pages_full={}
        )

        result = canvas_context.retrieve(
            "chemistry stoichiometry long assignment",
            course_id="12345",
        )

        self.assertTrue(result["found"])
        self.assertLessEqual(
            len(result["context_block"]),
            canvas_context._MAX_CONTEXT_CHARS,
            f"context_block length {len(result['context_block'])} exceeds {canvas_context._MAX_CONTEXT_CHARS}",
        )

    # ── Scenario 3: no cookies + no local cache → empty dict, zero network calls

    @patch("urllib.request.urlopen")
    def test_unconfigured_returns_empty_no_network(self, mock_urlopen):
        """When cookies file is absent AND local cache dir is absent, retrieve() returns empty, zero calls."""
        # setUp already sets both to nonexistent paths
        result = canvas_context.retrieve("any chemistry question about stoichiometry")

        self.assertFalse(result["found"])
        self.assertFalse(result["is_assessment"])
        self.assertEqual(result["context_block"], "")
        self.assertEqual(result["excerpts"], [])
        mock_urlopen.assert_not_called()

    # ── Scenario 4: assessment artifact returns is_assessment=True

    @patch("canvas_context._load_cookies", return_value="canvas_session=test-session")
    @patch("urllib.request.urlopen")
    def test_assessment_detection(self, mock_urlopen, mock_load_cookies):
        """An assignment with quiz_id / online_quiz submission_types → is_assessment=True."""
        mock_urlopen.side_effect = _make_url_dispatcher(
            [MOCK_QUIZ_ASSIGNMENT], pages=[], pages_full={}
        )

        result = canvas_context.retrieve(
            "midterm exam review quiz practice problems",
            course_id="12345",
        )

        self.assertTrue(result["found"], "Expected found=True for the quiz assignment")
        self.assertTrue(result["is_assessment"], "Expected is_assessment=True for a quiz")

    # ── Scenario 5: second call within TTL does not re-hit the API (cache hit)

    @patch("canvas_context._load_cookies", return_value="canvas_session=test-session")
    @patch("urllib.request.urlopen")
    def test_cache_hit_prevents_second_network_call(self, mock_urlopen, mock_load_cookies):
        """Second retrieve() call within TTL uses cache — no additional network calls."""
        mock_urlopen.side_effect = _make_url_dispatcher(MOCK_ASSIGNMENTS)

        question = "Fahrenheit temperature significant figures chapter homework"

        # First call — fetches from network and populates cache
        result1 = canvas_context.retrieve(question, tags=["chapter_1"], course_id="12345")
        calls_after_first = mock_urlopen.call_count

        self.assertTrue(result1["found"])
        self.assertGreater(calls_after_first, 0, "First call should have made network calls")

        # Second call — all list and item fetches must be cache hits
        result2 = canvas_context.retrieve(question, tags=["chapter_1"], course_id="12345")
        calls_after_second = mock_urlopen.call_count

        self.assertEqual(
            calls_after_first,
            calls_after_second,
            f"Second call within TTL made {calls_after_second - calls_after_first} "
            f"unexpected network call(s)",
        )
        self.assertTrue(result2["found"])

    # ── Scenario 6: Cookie header injected, Authorization header absent

    @patch("canvas_context._load_cookies", return_value="canvas_session=test-abc; _csrf_token=xyz")
    @patch("urllib.request.urlopen")
    def test_cookie_header_injected(self, mock_urlopen, mock_load_cookies):
        """Requests carry Cookie header with full cookie string; Authorization header is absent."""
        captured = []

        def side_effect(req, timeout=None, context=None):
            captured.append(req)
            return _mock_response([])

        mock_urlopen.side_effect = side_effect

        canvas_context.retrieve("some chemistry question", course_id="12345")

        self.assertGreater(len(captured), 0, "Expected at least one network request")
        req = captured[0]
        cookie_val = req.get_header("Cookie")
        self.assertIsNotNone(cookie_val, "Cookie header should be set")
        self.assertIn("canvas_session=test-abc", cookie_val)
        self.assertIsNone(req.get_header("Authorization"), "Authorization header must be absent")

    # ── Scenario 7: 401 from Canvas → _session_expired=True → falls back to local cache

    def test_expired_session_401_falls_back_to_local_cache(self):
        """A 401 from Canvas sets _session_expired; retrieve then serves from local file cache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Build local cache: CHEM_11 subdir with a relevant .txt file
            chem_dir = Path(tmpdir) / "CHEM_11"
            chem_dir.mkdir()
            (chem_dir / "significant_figures.txt").write_text(
                "Significant figures rules: all non-zero digits are significant. "
                "Zeros between non-zero digits count as significant figures.",
                encoding="utf-8",
            )

            # Write a valid cookies file so _load_cookies() returns a string
            cookies_file = Path(tmpdir) / "canvas_cookies.json"
            _write_temp_cookies(cookies_file)

            canvas_context._COOKIES_PATH = cookies_file
            canvas_context._LOCAL_CACHE_DIR = Path(tmpdir)
            canvas_context._session_expired = False

            http_err = urllib.error.HTTPError(
                "https://canvas.ucsd.edu/api/v1/courses/12345/assignments",
                401,
                "Unauthorized",
                {},
                None,
            )

            with patch("urllib.request.urlopen", side_effect=http_err):
                result = canvas_context.retrieve(
                    "significant figures homework",
                    tags=["chapter_1"],
                    course_id="12345",
                )

        self.assertTrue(result["found"], "Expected local cache to satisfy the query after 401")
        self.assertGreater(len(result["context_block"]), 0)
        self.assertIsInstance(result["excerpts"], list)
        self.assertGreater(len(result["excerpts"]), 0)

    # ── Scenario 8: no cookies → zero network calls, local cache serves result

    def test_no_cookies_no_network_but_local_cache(self):
        """Without a cookies file, retrieve makes zero network calls and uses local cache only."""
        with tempfile.TemporaryDirectory() as tmpdir:
            chem_dir = Path(tmpdir) / "CHEM_11"
            chem_dir.mkdir()
            (chem_dir / "chemistry_homework.txt").write_text(
                "Chemistry homework covers stoichiometry and mole calculations "
                "as well as unit conversions.",
                encoding="utf-8",
            )

            # No cookies file — _COOKIES_PATH points to nonexistent file
            canvas_context._COOKIES_PATH = Path(tmpdir) / "no_cookies.json"
            canvas_context._LOCAL_CACHE_DIR = Path(tmpdir)

            with patch("urllib.request.urlopen") as mock_urlopen:
                result = canvas_context.retrieve("chemistry homework")

            self.assertTrue(result["found"], "Expected local cache to provide a result")
            mock_urlopen.assert_not_called()

    # ── Additional: _looks_like_assessment coverage

    def test_looks_like_assessment_quiz_id(self):
        self.assertTrue(canvas_context._looks_like_assessment({"quiz_id": 5, "name": "HW"}))

    def test_looks_like_assessment_online_quiz_type(self):
        self.assertTrue(canvas_context._looks_like_assessment({
            "name": "Week 3 Assessment",
            "submission_types": ["online_quiz"],
        }))

    def test_looks_like_assessment_title_keyword(self):
        for title in ("Chapter Final", "Week 2 Exam", "Practice Test", "Midterm Review"):
            with self.subTest(title=title):
                self.assertTrue(canvas_context._looks_like_assessment({"name": title}))

    def test_looks_like_assessment_false_for_homework(self):
        self.assertFalse(canvas_context._looks_like_assessment({
            "name": "Chapter 1 Homework",
            "submission_types": ["online_text_entry"],
            "quiz_id": None,
        }))

    # ── Additional: retrieve() never raises on bad inputs

    @patch("canvas_context._load_cookies", return_value="canvas_session=test-session")
    @patch("urllib.request.urlopen")
    def test_retrieve_never_raises_on_empty_question(self, mock_urlopen, mock_load_cookies):
        """retrieve() with empty string returns empty result without raising."""
        mock_urlopen.side_effect = _make_url_dispatcher([])
        result = canvas_context.retrieve("", course_id="12345")
        self.assertFalse(result["found"])
        self.assertIn("context_block", result)
        self.assertIn("excerpts", result)


if __name__ == "__main__":
    sys.exit(unittest.main())
