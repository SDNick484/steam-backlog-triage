"""backlog.html in a real browser (Playwright + Chromium), served by backlog_server.py.

What it proves: the page loads with no console errors or failed requests; the lane marks, an Up next order and a
note survive a second page load (they are saved by the server - contract sections 5 and 6 in
docs/backlog-api-contract.md in SDNick484/mcp-server-steam-backlog); and the Refresh library button runs the (stub) export and the page then shows the
new library.

Set BROWSER_EXECUTABLE to use an installed Chrome instead of Playwright's own Chromium, and REQUIRE_BROWSER=1
(CI does) to make a missing Playwright an error instead of a skip.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _support import Server, load_fixture_library  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

REQUIRE = os.environ.get("REQUIRE_BROWSER") == "1"
NAMES = {1001: "Test Game Alpha", 1002: "Test Game Bravo", 1003: "Test Game Charlie", 1004: "Test Game Delta",
         1005: "Test Game Echo", 1006: "Test Game Foxtrot"}

REFRESH_STUB = r'''
set -eu
echo "$(date '+%F %T') starting export"
echo "Refreshed Persona: 2 games"
cp "$STUB_NEW_LIBRARY" "$EXPORT_DIR/.stub.tmp"
mv "$EXPORT_DIR/.stub.tmp" "$EXPORT_DIR/steam_backlog.json"
echo "Wrote $EXPORT_DIR/steam_backlog.json: 2 games, 0 never launched, 0 with HLTB times."
'''


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class PageSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise RuntimeError("REQUIRE_BROWSER=1 but playwright is not installed")
        cls.pw = sync_playwright().start()
        exe = os.environ.get("BROWSER_EXECUTABLE")
        cls.browser = cls.pw.chromium.launch(**({"executable_path": exe} if exe else {}),
                                             args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lib = load_fixture_library()
        lib["persona"] = "Test Persona"
        new = dict(lib, persona="Refreshed Persona", exportedAt="2031-02-03T04:05:06+00:00",
                   games=lib["games"][:2])
        self.new_lib = Path(tmp.name) / "new.json"
        self.new_lib.write_text(json.dumps(new))
        self.server = Server(env={"STUB_NEW_LIBRARY": str(self.new_lib)}, export_cmd=None)
        stub = self.server.stub("refresh-stub.sh", REFRESH_STUB)
        self.server.cmd += ["--export-cmd", str(stub)]
        self.addCleanup(self.server.cleanup)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.port}/"
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        # Hermetic: the page links Google Fonts. Answer every request that isn't the local server with an empty
        # stylesheet, so the run neither needs the internet nor sees errors from it.
        self.context.route(lambda u: not u.startswith(self.url.rstrip("/")),
                           lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        self.problems: list[str] = []

    def open(self):
        page = self.context.new_page()
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"request failed: {r.url} {r.failure}"))
        page.on("response", lambda r: self.problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)
        page.goto(self.url)
        page.wait_for_function("document.getElementById('who').textContent.includes('Test Persona')", timeout=15000)
        return page

    @staticmethod
    def names(page) -> list[str]:
        return page.eval_on_selector_all("#list .row:not(.head) .g-name a", "els => els.map(e => e.textContent)")

    @staticmethod
    def pressed(page, appid: int) -> list[str]:
        return page.eval_on_selector_all(f'#list [data-appid="{appid}"][aria-pressed="true"]',
                                         "els => els.map(e => e.dataset.lane)")

    def wait_saved(self, page):
        """Wait until the page's queued saves have all reached the server (the toast only appears on failure)."""
        page.wait_for_timeout(300)
        self.assertEqual(self.problems, [])

    def test_loads_cleanly_from_the_server(self):
        page = self.open()
        self.assertIn("Test Persona", page.text_content("#who"))
        page.select_option("#view", "all")
        self.assertEqual(len(self.names(page)), 6)
        self.assertTrue(page.is_visible("#refreshBtn"), "API 2 server: the Refresh library button is offered")
        self.assertEqual(page.inner_text("#refreshLabel").strip(), "Refresh library")
        self.assertEqual(self.problems, [])

    def test_lanes_order_and_notes_survive_a_reload(self):
        page = self.open()
        page.select_option("#view", "all")
        for appid in (1003, 1001, 1005):                       # marked in this order -> Up next order
            page.click(f'[data-appid="{appid}"][data-lane="next"]')
        page.click('[data-appid="1002"][data-lane="skip"]')
        page.click('[data-appid="1004"][data-lane="done"]')
        page.select_option("#view", "next")
        before = self.names(page)
        self.assertEqual(sorted(before), sorted(NAMES[i] for i in (1003, 1001, 1005)))
        last = before[-1]
        page.click(f'#list .row:has-text("{last}") [data-move][data-dir="-1"]')   # move the last one up
        moved = self.names(page)
        self.assertNotEqual(moved, before)
        self.assertEqual(sorted(moved), sorted(before))
        page.select_option("#view", "all")
        page.click('#list .row:has-text("Test Game Bravo") [data-note-open]')
        page.fill('textarea[data-note-for="1002"]', "play after the patch lands")
        page.click('[data-note-save="1002"]')
        self.assertIn("play after the patch lands", page.inner_text("#list"))
        self.wait_saved(page)

        # A second load of the same server data in a brand-new page.
        page2 = self.open()
        page2.select_option("#view", "all")
        self.assertEqual(self.pressed(page2, 1003), ["next"])
        self.assertEqual(self.pressed(page2, 1001), ["next"])
        self.assertEqual(self.pressed(page2, 1005), ["next"])
        self.assertEqual(self.pressed(page2, 1002), ["skip"])
        self.assertEqual(self.pressed(page2, 1004), ["done"])
        self.assertEqual(self.pressed(page2, 1006), [])
        self.assertIn("play after the patch lands", page2.inner_text("#list"))
        page2.select_option("#view", "next")
        self.assertEqual(self.names(page2), moved, "Up next order persisted")
        self.assertEqual(self.problems, [])

    def test_refresh_library_runs_the_export_and_reloads(self):
        page = self.open()
        page.select_option("#view", "all")
        self.assertEqual(len(self.names(page)), 6)
        page.click("#refreshBtn")
        page.wait_for_function("document.getElementById('who').textContent.includes('Refreshed Persona')", timeout=30000)
        page.wait_for_function("document.getElementById('refreshLabel').textContent.trim() === 'Refresh library'", timeout=15000)
        page.select_option("#view", "all")
        self.assertEqual(self.names(page), ["Test Game Alpha", "Test Game Bravo"])
        self.assertTrue(page.is_enabled("#refreshBtn"))
        self.assertIn("Library refreshed", page.inner_text("#toast"))
        self.assertEqual(self.problems, [])


if __name__ == "__main__":
    unittest.main()
