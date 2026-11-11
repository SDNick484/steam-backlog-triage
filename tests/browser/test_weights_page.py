"""The Tune picks panel, the custom-weights badge and the score breakdown on each pick card (backlog.html).

What it proves: weights saved in prefs.json rank the picks and the Score sort exactly as backlog_query does, and each
card's breakdown line equals backlog_query.explain(score_breakdown(...)) (the page-vs-Python label check); dragging a
slider re-ranks the picks without a network write and Cancel restores the saved order; Save sends one PUT with
If-Match and survives a reload; Reset needs a Save; a stale tab gets the 412 message and the newer weights; without
the prefs route the Tune button is hidden. Run in a light and a dark colour scheme at 390 px.

Set BROWSER_EXECUTABLE to use an installed Chrome, and REQUIRE_BROWSER=1 (CI does) to make a missing Playwright an
error instead of a skip.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import backlog_query as q  # noqa: E402
from _support import Server, load_fixture_library  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

REQUIRE = os.environ.get("REQUIRE_BROWSER") == "1"
# Tuned so the fixture's default and weighted pick orders differ (the test asserts that first).
W = {"play": 1, "deck": 0, "length": 0, "metacritic": 3, "deck_played": 1, "controller": 1, "next": 1}


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class WeightsPageBase:
    SCHEME = "light"

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

    def setUp(self):
        self.server = Server()
        self.addCleanup(self.server.cleanup)
        self.lib = load_fixture_library()
        self.games = [q.normalize(g, i) for i, g in enumerate(self.lib["games"])]
        self.problems: list[str] = []
        self.allow_status = None          # one expected api/prefs refusal (404 or 412) a test may allow
        self.allowed: list[str] = []      # the console lines that allowance matched
        self.requests: list[tuple[str, str, dict]] = []

    def start(self, weights=None):
        if weights is not None:
            (self.server.data / "prefs.json").write_text(json.dumps({"version": 1, "weights": weights}))
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.port}/"
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844}, color_scheme=self.SCHEME)
        self.addCleanup(self.context.close)
        self.context.route(lambda u: not u.startswith(self.url.rstrip("/")),
                           lambda route: route.fulfill(status=200, content_type="text/css", body=""))

    def open(self, expect_tune=True):
        page = self.context.new_page()
        page.on("console", self.on_console)
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"request failed: {r.url} {r.failure}"))
        page.on("request", lambda r: self.requests.append((r.method, r.url, r.headers)))
        page.goto(self.url)
        page.wait_for_function("document.getElementById('who').textContent.includes('Persona')", timeout=15000)
        if expect_tune:
            page.wait_for_selector("#tuneBtn:not([hidden])", timeout=5000)
        return page

    def on_console(self, m):
        if m.type not in ("error", "warning"):
            return
        # The browser logs every non-2xx response. Only the one status a test allows, for /api/prefs, is let through.
        if (self.allow_status and m.text.startswith("Failed to load resource")
                and f"status of {self.allow_status}" in m.text
                and (m.location.get("url") or "").endswith("/api/prefs")):
            self.allowed.append(m.text)
            return
        self.problems.append(f"console {m.type}: {m.text}")

    def check_clean(self, page):
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
        self.assertEqual(self.problems, [])

    @staticmethod
    def picks(page) -> list[str]:
        return page.eval_on_selector_all("#picks .pick h3", "els => els.map(e => e.textContent)")

    def expected(self, weights) -> list[str]:
        return [g.name for g in q.picks(self.games, q.clean({}), {}, {}, weights=weights)[:6]]

    def puts(self):
        return [r for r in self.requests if r[0] == "PUT" and r[1].endswith("/api/prefs")]

    def test_saved_weights_rank_the_picks(self):
        default, weighted = self.expected(q.DEFAULT_WEIGHTS), self.expected(W)
        self.assertNotEqual(default, weighted)           # so this can't pass vacuously
        self.start(W)
        page = self.open()
        self.assertEqual(self.picks(page), weighted)
        self.assertTrue(page.is_visible("#wBadge"))
        top = q.picks(self.games, q.clean({}), {}, {}, weights=W)[:6]
        # explain() joins with ", "; the page joins the same "label +points" terms with " · ".
        want = [q.explain(q.score_breakdown(g, "backlog", weights=W)).replace(", ", " · ") for g in top]
        got = page.eval_on_selector_all("#picks .pick .terms", "els => els.map(e => e.textContent)")
        self.assertEqual(got, want)
        page.select_option("#view", "all")
        page.select_option("#sort", "score")
        rows = page.eval_on_selector_all("#list .row:not(.head) .g-name a", "els => els.map(e => e.textContent)")
        self.assertEqual(rows, [g.name for g in q.sort_games(self.games, "score", {}, weights=W)])
        self.check_clean(page)

    def test_default_weights_show_no_badge(self):
        self.start()
        page = self.open()
        self.assertEqual(self.picks(page), self.expected(q.DEFAULT_WEIGHTS))
        self.assertFalse(page.is_visible("#wBadge"))
        self.check_clean(page)

    def test_drag_reorders_without_saving_then_cancel_restores(self):
        self.start(W)
        page = self.open()
        saved = self.picks(page)
        page.click("#tuneBtn")
        self.assertTrue(page.is_visible("#tunePanel"))
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)   # panel open
        page.fill("#w-deck", "3")
        page.fill("#w-metacritic", "0")
        changed = {**W, "deck": 3, "metacritic": 0}
        page.wait_for_function("(o) => JSON.stringify([...document.querySelectorAll('#picks .pick h3')].map(e => e.textContent)) === o",
                               arg=json.dumps(self.expected(changed), separators=(",", ":")))
        self.assertEqual(self.picks(page), self.expected(changed))
        self.assertNotEqual(self.picks(page), saved)
        self.assertEqual(self.puts(), [])
        page.click("#tuneCancel")
        self.assertFalse(page.is_visible("#tunePanel"))
        self.assertEqual(self.picks(page), saved)
        self.assertEqual(self.puts(), [])
        page.click("#tuneBtn")                           # Escape closes it too, and restores
        page.fill("#w-deck", "3")
        page.keyboard.press("Escape")
        self.assertFalse(page.is_visible("#tunePanel"))
        self.assertEqual(self.picks(page), saved)
        self.check_clean(page)

    def test_save_persists_and_reset_needs_save(self):
        self.start()
        page = self.open()
        page.click("#tuneBtn")
        page.fill("#w-deck", "0")
        page.fill("#w-metacritic", "3")
        page.fill("#w-length", "0")
        page.click("#tuneSave")
        page.wait_for_selector("#tunePanel", state="hidden")
        self.assertEqual(len(self.puts()), 1)
        self.assertIn("if-match", self.puts()[0][2])
        order = self.picks(page)
        self.assertEqual(order, self.expected({**q.DEFAULT_WEIGHTS, "deck": 0, "metacritic": 3, "length": 0}))
        self.assertTrue(page.is_visible("#wBadge"))
        page2 = self.open()
        self.assertEqual(self.picks(page2), order)
        page2.click("#tuneBtn")
        self.assertEqual(page2.input_value("#w-metacritic"), "3")
        self.assertEqual(page2.input_value("#w-deck"), "0")
        self.assertEqual(page2.get_attribute("#w-metacritic", "aria-valuetext"), "×3")
        n = len(self.puts())
        page2.click("#tuneReset")
        for k in q.WEIGHT_KEYS:
            self.assertEqual(float(page2.input_value(f"#w-{k}")), 1.0)
        self.assertEqual(self.picks(page2), self.expected(q.DEFAULT_WEIGHTS))
        self.assertEqual(len(self.puts()), n)            # no PUT until Save
        page2.click("#tuneSave")
        page2.wait_for_selector("#tunePanel", state="hidden")
        self.assertEqual(len(self.puts()), n + 1)
        self.assertFalse(page2.is_visible("#wBadge"))
        self.check_clean(page2)

    def test_stale_tab_gets_412(self):
        self.start()
        self.allow_status = 412
        a, b = self.open(), self.open()
        a.click("#tuneBtn"); a.fill("#w-deck", "2"); a.click("#tuneSave")
        a.wait_for_selector("#tunePanel", state="hidden")
        b.click("#tuneBtn"); b.fill("#w-deck", "0.5"); b.click("#tuneSave")
        b.wait_for_function("document.getElementById('tuneMsg').textContent.includes('changed in another window')")
        self.assertEqual(b.inner_text("#tuneMsg").strip(), "The weights changed in another window; reloaded them.")
        self.assertEqual(float(b.input_value("#w-deck")), 2.0)
        self.assertTrue(b.is_visible("#tunePanel"))
        saved = json.loads((self.server.data / "prefs.json").read_text())
        self.assertEqual(saved["weights"]["deck"], 2)
        self.assertEqual(self.picks(b), self.expected({**q.DEFAULT_WEIGHTS, "deck": 2}))
        self.assertEqual(len(self.allowed), 1)           # exactly the one expected refusal
        self.check_clean(b)

    def test_hidden_without_prefs_route(self):
        self.start(W)
        self.allow_status = 404
        self.context.route("**/api/prefs", lambda r: r.fulfill(status=404, content_type="application/json",
                                                              body='{"error":"not found"}'))
        page = self.open(expect_tune=False)
        page.wait_for_selector("#picks .pick")
        self.assertFalse(page.is_visible("#tuneBtn"))
        self.assertFalse(page.is_visible("#wBadge"))
        self.assertEqual(self.picks(page), self.expected(q.DEFAULT_WEIGHTS))
        self.assertEqual(len(self.allowed), 1)           # exactly the one expected 404
        self.check_clean(page)


class WeightsPageLight(WeightsPageBase, unittest.TestCase):
    SCHEME = "light"


class WeightsPageDark(WeightsPageBase, unittest.TestCase):
    SCHEME = "dark"


if __name__ == "__main__":
    unittest.main()
