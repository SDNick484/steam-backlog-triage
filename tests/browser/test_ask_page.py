"""The Ask view (/ask) of backlog.html in a real browser, served by backlog_server.py.

What it proves: the list's rows are unchanged by the shared row renderer (a golden file recorded before the
refactor); the Ask pill appears only when api/ask/status says so; an answer is shown as text, its games as rows
whose lane buttons save; Retry, the turn limit, New question and "Ask instead" behave as spec section 10 says; the
thread is never written to browser storage; no horizontal scroll at 390 and 360 px; and the claude.ai artifact's
"Ask Claude for a pick" panel still works. api/ask and api/ask/status are stubbed with page.route, so no request
ever reaches Anthropic.

Set BROWSER_EXECUTABLE to use an installed Chrome, REQUIRE_BROWSER=1 (CI does) to make a missing Playwright an error
instead of a skip, and RECORD_GOLDEN=1 to rewrite tests/fixtures/list_rows_golden.json.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _support import ROOT, Server, atomic_write_json, load_fixture_library  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

REQUIRE = os.environ.get("REQUIRE_BROWSER") == "1"
GOLDEN = HERE.parent / "fixtures" / "list_rows_golden.json"
GOLDEN_HASHES = ["", "#view=all&sort=name", "#view=next"]
FROZEN = "2026-11-07T12:00:00Z"
SCHEMES = ("light", "dark")


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class BrowserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise RuntimeError("REQUIRE_BROWSER=1 but playwright is not installed")
        cls.pw = sync_playwright().start()
        exe = os.environ.get("BROWSER_EXECUTABLE")
        cls.browser = cls.pw.chromium.launch(**({"executable_path": exe} if exe else {}), args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def serve(self, triage=None, notes=None):
        """Start backlog_server.py on the fixture library (persona "Test Persona")."""
        server = Server(seed=False)
        self.addCleanup(server.cleanup)
        lib = load_fixture_library()
        lib["persona"] = "Test Persona"
        atomic_write_json(server.data / "steam_backlog.json", lib)
        if triage:
            atomic_write_json(server.data / "triage.json", {str(k): v for k, v in triage.items()})
        if notes:
            atomic_write_json(server.data / "notes.json", {str(k): v for k, v in notes.items()})
        server.start()
        return server, f"http://127.0.0.1:{server.port}"

    def new_context(self, base, width=390, height=844, scheme="light"):
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, locale="en-US")
        self.addCleanup(ctx.close)
        # Hermetic: the page links Google Fonts. Anything not from the local server gets an empty stylesheet.
        ctx.route(lambda u: not u.startswith(base), lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        return ctx

    def watch(self, page):
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"request failed: {r.url} {r.failure}"))
        page.on("response", lambda r: self.problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)
        return page

    def setUp(self):
        self.problems: list[str] = []


class ListUnchanged(BrowserCase):
    """#list's HTML for three filter states, recorded before gameRowHtml() was lifted out of renderList()."""

    def capture(self) -> dict:
        _, base = self.serve(triage={1001: "next", 1003: "skip"}, notes={1002: "placeholder note"})
        ctx = self.new_context(base, width=1280, height=900)
        out = {}
        for h in GOLDEN_HASHES:
            page = self.watch(ctx.new_page())
            page.clock.set_fixed_time(FROZEN)
            page.goto(base + "/" + h)
            page.wait_for_function("document.getElementById('who').textContent.includes('Test Persona')", timeout=15000)
            out[h] = page.inner_html("#list")
            page.close()
        self.assertEqual(self.problems, [])
        return out

    def test_list_rows_match_golden(self):
        got = self.capture()
        if os.environ.get("RECORD_GOLDEN") == "1":
            GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        want = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for h in GOLDEN_HASHES:
            with self.subTest(hash=h):
                self.assertEqual(got[h], want[h])



ALPHA, BRAVO = {"appid": 1001, "name": "Test Game Alpha"}, {"appid": 1002, "name": "Test Game Bravo"}
ANSWER = {"answer": "Two <b>ideas</b>.", "items": [ALPHA, BRAVO], "turns_left": 4}
NO_RETRY_AFTER = {}


def is_expected_ask_error(p: str) -> bool:
    """The problems a deliberate api/ask error response leaves: the response itself and Chrome's console line."""
    return "/api/ask" in p or "Failed to load resource: the server responded with a status of" in p


class AskPage(BrowserCase):
    def setUp(self):
        super().setUp()
        self.reset()

    def reset(self):
        self.problems = []
        self.status = {"ask": True, "turns": 5}
        self.replies = [(200, ANSWER, None)]      # api/ask answers, in order; the last one repeats
        self.posts = []                           # request bodies api/ask received

    def _ask(self, route):
        if route.request.method != "POST":
            return route.fulfill(status=405, json={"error": "method not allowed"})
        self.posts.append(route.request.post_data_json)
        status, body, headers = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        route.fulfill(status=status, json=body, headers=headers or {})

    def each_scheme(self, fn):
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme):
                self.reset()
                fn(scheme)

    def context(self, scheme, width=390, height=844):
        self.server, self.base = self.serve()
        ctx = self.new_context(self.base, width, height, scheme)
        ctx.route("**/api/ask/status", lambda r: r.fulfill(status=200, json=self.status))
        ctx.route("**/api/ask", self._ask)
        return ctx

    def open(self, ctx, path="/ask"):
        page = self.watch(ctx.new_page())
        page.goto(self.base + path)
        if path.startswith("/ask"):
            page.wait_for_function("!document.getElementById('chatQ').disabled", timeout=15000)
        else:
            page.wait_for_function("document.getElementById('who').textContent.includes('Test Persona')", timeout=15000)
        return page

    def ask(self, page, q="something short for tonight", wait=".chat-a"):
        n = page.locator(wait).count()
        page.fill("#chatQ", q)
        page.press("#chatQ", "Enter")
        page.wait_for_function(f"document.querySelectorAll({json.dumps(wait)}).length > {n}", timeout=10000)
        page.wait_for_function("!document.querySelector('#chatThread .chat-status')", timeout=10000)

    def assert_clean(self, allow_ask_errors=False):
        left = [p for p in self.problems if not (allow_ask_errors and is_expected_ask_error(p))]
        self.assertEqual(left, [])

    # ---- the pill
    def test_pill_visible_and_current(self):
        def run(scheme):
            ctx = self.context(scheme)
            page = self.open(ctx, "/#view=all")
            page.wait_for_selector("#chatTop", state="visible")
            self.assertIsNone(page.get_attribute("#chatTop", "aria-current"))
            self.assertEqual(page.eval_on_selector("#chatTop", "a => a.href"), self.base + "/ask#view=all")
            self.assertIn("Ask", page.inner_text("#chatTop"))
            self.assertEqual(page.locator("#chatTop svg").count(), 1)
            page.select_option("#view", "next")
            self.assertEqual(page.eval_on_selector("#chatTop", "a => a.href"), self.base + "/ask#view=next")
            page2 = self.open(ctx, "/ask#view=next")
            page2.wait_for_selector("#chatTop", state="visible")
            self.assertEqual(page2.get_attribute("#chatTop", "aria-current"), "page")
            self.assertEqual(page2.title(), "Ask your backlog")
            self.assert_clean()
        self.each_scheme(run)

    def test_pill_hidden_when_off(self):
        def run(scheme):
            self.status = {"ask": False}
            ctx = self.context(scheme)
            page = self.open(ctx, "/")
            page.wait_for_timeout(200)
            self.assertEqual(page.locator("#chatTop").count(), 0)
            page.fill("#q", "zzzz")
            page.wait_for_selector("#list .empty")
            self.assertEqual(page.locator("#chatInstead").count(), 0, "no Ask instead without the pill")
            page2 =self.watch(ctx.new_page())
            page2.goto(self.base + "/ask")
            page2.wait_for_function("document.getElementById('chatThread').textContent.includes(\"Ask isn't available here.\")",
                                    timeout=15000)
            self.assertEqual(page2.locator("#chatTop").count(), 0)
            self.assertEqual(page2.eval_on_selector("#chatThread a", "a => a.href"), self.base + "/")
            self.assertTrue(page2.is_disabled("#chatQ"))
            self.assertEqual(self.posts, [])
            self.assert_clean()
        self.each_scheme(run)

    # ---- answers and rows
    def test_answer_is_text_and_rows_render(self):
        def run(scheme):
            page = self.open(self.context(scheme))
            self.ask(page, "two short ones")
            self.assertEqual(page.eval_on_selector(".chat-a", "e => e.textContent"), "Two <b>ideas</b>.")
            self.assertEqual(page.locator(".chat-a b").count(), 0)
            self.assertEqual(page.eval_on_selector(".chat-q", "e => e.textContent"), "two short ones")
            rows = page.locator("#chatThread .chat-turn .row")
            self.assertEqual(rows.count(), 2)
            for i in range(2):
                self.assertEqual(rows.nth(i).locator("[data-lane]").count(), 4)
            self.assertEqual(page.locator("#chatThread [data-move]").count(), 0)
            self.assertEqual(page.locator("#chatThread [draggable]").count(), 0)
            self.assertEqual(self.posts, [{"thread": [{"q": "two short ones"}]}])
            self.assertIn("4 left", page.inner_text("#chatMeta"))
            self.assert_clean()
        self.each_scheme(run)

    def test_next_on_ask_row_saves(self):
        def run(scheme):
            page = self.open(self.context(scheme))
            self.ask(page)
            with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/triage")) as req:
                page.click('#chatThread [data-appid="1002"][data-lane="next"]')
            self.assertEqual(req.value.post_data_json, {"appid": 1002, "lane": "next"})
            page.wait_for_selector('#chatThread [data-appid="1002"][data-lane="next"][aria-pressed="true"]')
            self.assertEqual(page.locator(".chat-a").count(), 1, "the thread survives the re-render")
            # The note editor opens in place (renderRows() is mode-aware) and has no Move buttons.
            page.click('#chatThread [data-note-open="1002"]')
            page.wait_for_selector('#chatThread textarea[data-note-for="1002"]')
            self.assertEqual(page.locator("#chatThread [data-move]").count(), 0)
            page.click('#chatThread [data-note-cancel="1002"]')
            page.wait_for_selector('#chatThread [data-note-open="1002"]')
            page.wait_for_timeout(300)
            self.assert_clean()
        self.each_scheme(run)

    def test_unknown_item_plain_text(self):
        def run(scheme):
            self.replies = [(200, {"answer": "One.", "items": [{"appid": 999999, "name": "<i>Gone</i> & Co"}], "turns_left": 4}, None)]
            page = self.open(self.context(scheme))
            self.ask(page)
            self.assertEqual(page.eval_on_selector(".chat-missing", "e => e.textContent"), "<i>Gone</i> & Co")
            self.assertEqual(page.locator(".chat-missing i").count(), 0)
            self.assertEqual(page.locator("#chatThread .row").count(), 0)
            self.assert_clean()
        self.each_scheme(run)

    def test_no_horizontal_scroll(self):
        width = "document.documentElement.scrollWidth"

        def run(scheme):
            ctx = self.context(scheme)
            self.assertLessEqual(self.open(ctx, "/").evaluate(width), 390)
            page = self.open(ctx)
            self.ask(page)
            self.assertLessEqual(page.evaluate(width), 390)
            for path in ("/", "/ask"):
                small = self.watch(ctx.new_page())
                small.set_viewport_size({"width": 360, "height": 740})
                small.goto(self.base + path)
                small.wait_for_selector("#chatTop", state="visible")
                if path == "/ask":
                    self.ask(small)
                self.assertLessEqual(small.evaluate(width), 360, path)
                pill, h1 = small.locator("#chatTop").bounding_box(), small.locator("h1").bounding_box()
                mid = pill["y"] + pill["height"] / 2
                self.assertTrue(h1["y"] <= mid <= h1["y"] + h1["height"], (path, pill, h1))
                self.assertLessEqual(pill["x"] + pill["width"], 360)
            self.assert_clean()
        self.each_scheme(run)

    # ---- states
    def test_retry_after_502_resends_thread(self):
        def run(scheme):
            self.replies = [(502, {"error": "AI search is unavailable right now, try again"}, None), (200, ANSWER, None)]
            page = self.open(self.context(scheme))
            self.ask(page, "retry me", wait=".chat-error")
            self.assertIn("AI search is unavailable right now, try again", page.inner_text(".chat-error"))
            self.assertEqual(page.get_attribute(".chat-error", "role"), "alert")
            page.click("#chatRetry")
            page.wait_for_selector(".chat-a")
            self.assertEqual(self.posts, [{"thread": [{"q": "retry me"}]}] * 2)
            self.assertEqual(page.locator(".chat-error").count(), 0)
            self.assertEqual(page.locator(".chat-q").count(), 1)
            self.assert_clean(allow_ask_errors=True)
        self.each_scheme(run)

    def test_no_retry_on_cap_429(self):
        def run(scheme):
            self.replies = [(429, {"error": "Daily AI search limit reached; try again tomorrow."}, NO_RETRY_AFTER)]
            page = self.open(self.context(scheme))
            self.ask(page, wait=".chat-error")
            self.assertIn("Daily AI search limit reached", page.inner_text(".chat-error"))
            self.assertEqual(page.locator("#chatRetry").count(), 0)
            self.assert_clean(allow_ask_errors=True)
        self.each_scheme(run)

    def test_retry_on_busy_429(self):
        def run(scheme):
            self.replies = [(429, {"error": "Ask is busy; try again in a moment."}, {"Retry-After": "5"}), (200, ANSWER, None)]
            page = self.open(self.context(scheme))
            self.ask(page, "busy now", wait=".chat-error")
            self.assertIn("Ask is busy", page.inner_text(".chat-error"))
            page.click("#chatRetry")
            page.wait_for_selector(".chat-a")
            self.assertEqual(self.posts, [{"thread": [{"q": "busy now"}]}] * 2)
            self.assert_clean(allow_ask_errors=True)
        self.each_scheme(run)

    def test_turn_limit_disables_input(self):
        def run(scheme):
            self.replies = [(200, dict(ANSWER, turns_left=1), None), (200, dict(ANSWER, answer="Last one.", turns_left=0), None)]
            page = self.open(self.context(scheme))
            self.ask(page, "first")
            self.assertIn("1 left", page.inner_text("#chatMeta"))
            self.ask(page, "second")
            self.assertEqual(self.posts[1], {"thread": [{"q": "first"}, {"a": "Two <b>ideas</b>."}, {"q": "second"}]})
            self.assertTrue(page.is_disabled("#chatQ"))
            self.assertTrue(page.is_disabled("#chatSend"))
            self.assertEqual(page.get_attribute("#chatQ", "placeholder"), "Start a new question to keep going")
            self.assertIn("0 left", page.inner_text("#chatMeta"))
            self.assert_clean()
        self.each_scheme(run)

    def test_new_question_clears(self):
        def run(scheme):
            page = self.open(self.context(scheme))
            self.ask(page)
            page.click("#chatNew")
            self.assertEqual(page.locator(".chat-q").count(), 0)
            self.assertEqual(page.locator(".chat-a").count(), 0)
            self.assertEqual(page.locator("#chatThread .row").count(), 0)
            self.assertTrue(page.is_enabled("#chatQ"))
            self.assertEqual(page.input_value("#chatQ"), "")
            self.assertIn("5 left", page.inner_text("#chatMeta"))
            self.assertTrue(page.evaluate("document.activeElement.id") == "chatQ")
            self.assert_clean()
        self.each_scheme(run)

    def test_ask_instead_prefills_not_sends(self):
        def run(scheme):
            ctx = self.context(scheme)
            page = self.open(ctx, "/")
            page.fill("#q", "zzzz")
            page.wait_for_selector("#chatInstead", state="visible")
            page.click("#chatInstead")
            page.wait_for_url("**/ask**")
            page.wait_for_function("!document.getElementById('chatQ').disabled", timeout=15000)
            self.assertEqual(page.input_value("#chatQ"), "zzzz")
            self.assertEqual(page.evaluate("location.search"), "")
            page.wait_for_timeout(300)
            self.assertEqual(self.posts, [])
            self.assertEqual(page.locator(".chat-q").count(), 0)
            self.assert_clean()
        self.each_scheme(run)

    def test_needs_library_export(self):
        def run(scheme):
            self.server, self.base = self.serve_empty()
            ctx = self.new_context(self.base, 390, 844, scheme)
            ctx.route("**/api/ask/status", lambda r: r.fulfill(status=200, json=self.status))
            page = self.watch(ctx.new_page())
            page.goto(self.base + "/ask")
            page.wait_for_function("document.getElementById('chatThread').textContent.includes('Ask needs a library export first.')",
                                   timeout=15000)
            self.assertTrue(page.is_disabled("#chatQ"))
            self.assertEqual(self.posts, [])
            self.problems = [p for p in self.problems if "api/library" not in p and "404" not in p]
            self.assert_clean()
        self.each_scheme(run)

    def serve_empty(self):
        server = Server(seed=False)
        self.addCleanup(server.cleanup)
        server.start()
        return server, f"http://127.0.0.1:{server.port}"

    def test_waits_for_library(self):
        def run(scheme):
            ctx = self.context(scheme)
            held = []
            ctx.route("**/api/library", lambda r: held.append(r))
            page = self.watch(ctx.new_page())
            page.goto(self.base + "/ask", wait_until="domcontentloaded")
            page.wait_for_function("document.getElementById('chatQ')", timeout=5000)
            self.assertTrue(page.is_disabled("#chatQ"))
            self.assertEqual(page.get_attribute("#chatQ", "placeholder"), "Loading your library…")
            page.wait_for_timeout(1000)
            self.assertTrue(page.is_disabled("#chatQ"))
            self.assertTrue(page.is_disabled("#chatSend"))
            self.assertEqual(len(held), 1)
            held[0].continue_()
            page.wait_for_function("!document.getElementById('chatQ').disabled", timeout=15000)
            self.assertNotEqual(page.get_attribute("#chatQ", "placeholder"), "Loading your library…")
            self.assert_clean()
        self.each_scheme(run)

    def test_no_console_errors_all_modes(self):
        def run(scheme):
            for w, h in ((1280, 900), (390, 844)):
                ctx = self.context(scheme, w, h)
                for path in ("/", "/stats", "/ask"):
                    page = self.open(ctx, path)
                    page.wait_for_timeout(200)
                    if path == "/ask":
                        self.assertTrue(page.is_visible("#chatSec"))
                        self.assertFalse(page.is_visible("#list"))
                        self.assertFalse(page.is_visible("#picks"))
                        self.assertFalse(page.is_visible("#listH"))
                        self.assertFalse(page.is_visible("#statsSec"))
                    else:
                        self.assertFalse(page.is_visible("#chatSec"))
                    page.close()
            self.assert_clean()
        self.each_scheme(run)

    def test_thread_not_in_storage(self):
        def run(scheme):
            page = self.open(self.context(scheme))
            self.ask(page, "storage-marker question")
            dump = page.evaluate("""() => {
                const all = (s) => { const o = {}; for (let i = 0; i < s.length; i++) o[s.key(i)] = s.getItem(s.key(i)); return o; };
                return JSON.stringify([all(localStorage), all(sessionStorage)]);
            }""")
            self.assertNotIn("storage-marker", dump)
            self.assertNotIn("ideas", dump)
            self.assert_clean()
        self.each_scheme(run)


ARTIFACT_ORIGIN = "http://artifact.invalid"
CLAUDE_STUB = """window.claude = {use: async (n) => n === "sample" ? {json: async () => ({picks: [
  {id: %s, why: "a"}, {id: %s, why: "b"}, {id: %s, why: "c"}]})} : null};"""


class ArtifactPanel(BrowserCase):
    """The page as a claude.ai artifact: no backlog_server.py, window.claude provides the sample model."""

    def test_ask_panel_still_works(self):
        html = (ROOT / "backlog.html").read_text(encoding="utf-8")

        def handle(route):
            url = route.request.url
            if url == ARTIFACT_ORIGIN + "/":
                return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            if url.startswith(ARTIFACT_ORIGIN + "/api/library"):
                return route.fulfill(status=404, content_type="text/plain", body="not found")
            return route.fulfill(status=200, content_type="text/css", body="")

        ctx = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(ctx.close)
        ctx.route("**/*", handle)
        # The brief's 620, 413150 and 1145360 are "played" sample games, which the panel drops before asking
        # (it only offers backlog games), so the stub answers with three never-launched sample games instead.
        ctx.add_init_script(CLAUDE_STUB % (1092790, 753640, 653530))
        page = self.watch(ctx.new_page())
        page.goto(ARTIFACT_ORIGIN + "/")
        page.wait_for_selector("#askPanel", state="visible", timeout=15000)
        page.fill("#askMood", "something cozy")
        page.click("#askBtn")
        page.wait_for_function("document.querySelectorAll('#askOut .pick').length === 3", timeout=10000)
        self.assertEqual(page.locator("#chatTop").count(), 0)
        self.assertFalse(page.is_visible("#chatSec"))
        left = [p for p in self.problems if "/api/library" not in p and "status of 404" not in p]
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
