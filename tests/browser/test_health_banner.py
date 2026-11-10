"""The stale / failed export banner in backlog.html (Playwright + Chromium), served by backlog_server.py.

One dismissible banner when the last export run failed or the library is over 10 days old; nothing when healthy.
Dismissal lasts for the session. Set REQUIRE_BROWSER=1 (CI does) to make a missing Playwright an error.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _support import Server, load_fixture_library, seed_library  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

REQUIRE = os.environ.get("REQUIRE_BROWSER") == "1"
NOW = datetime.datetime.now(datetime.timezone.utc)


def stamp(delta: datetime.timedelta, z: bool = False) -> str:
    t = NOW - delta
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if z else t.isoformat(timespec="seconds")


FAIL_STUB = r'''
set -eu
echo "$(date '+%F %T') starting export"
printf '{"version":1,"last_attempt":"%s","last_ok":null,"consecutive_failures":1,"error_class":"details"}' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$EXPORT_DIR/run_status.json"
exit 1
'''


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class HealthBanner(unittest.TestCase):
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

    def serve(self, exported_ago: datetime.timedelta, run: dict | None = None, export_cmd="noop") -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        lib = load_fixture_library()
        lib["persona"] = "Test Persona"
        lib["exportedAt"] = stamp(exported_ago)
        seed_library(data, lib)
        if run is not None:
            (data / "run_status.json").write_text(json.dumps(run))
        self.server = Server(data_dir=data, export_cmd=None)
        self.server.cmd += ["--export-cmd", str(self.server.stub("stub.sh", export_cmd if export_cmd != "noop" else "exit 0\n"))]
        self.addCleanup(self.server.cleanup)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.port}/"
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.context.route(lambda u: not u.startswith(self.url.rstrip("/")),
                           lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        self.problems: list[str] = []
        return self.url

    def open(self, init_script: str | None = None):
        if init_script:
            self.context.add_init_script(init_script)
        page = self.context.new_page()
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.goto(self.url)
        page.wait_for_function("document.getElementById('who').textContent.includes('Test Persona')", timeout=15000)
        page.wait_for_timeout(200)  # run-status is fetched just before the library
        return page

    @staticmethod
    def banner(page) -> str | None:
        if not page.is_visible("#healthBanner"):
            return None
        return " ".join(page.inner_text("#healthText").split())

    @staticmethod
    def local(page, iso: str) -> str:
        return page.evaluate("(s) => new Date(s).toLocaleString()", iso)

    @staticmethod
    def status_obj(**kw) -> dict:
        r = {"version": 1, "last_attempt": stamp(datetime.timedelta(hours=1), True),
             "last_ok": stamp(datetime.timedelta(hours=1), True), "consecutive_failures": 0, "error_class": None}
        r.update(kw)
        return r

    def test_hidden_when_healthy(self):
        self.serve(datetime.timedelta(hours=2), self.status_obj())
        page = self.open()
        self.assertIsNone(self.banner(page))
        self.assertEqual(self.problems, [])

    def test_hidden_when_fresh_and_no_status_file_yet(self):
        self.serve(datetime.timedelta(days=2), None)
        self.assertIsNone(self.banner(self.open()))

    def test_failed_run(self):
        exported = datetime.timedelta(days=2)
        self.serve(exported, self.status_obj(consecutive_failures=2, error_class="hltb"))
        page = self.open()
        when = self.local(page, stamp(exported))
        self.assertEqual(self.banner(page), f"Library data last refreshed {when}. The last refresh failed (hltb); "
                                            "see the export log on the server.")
        self.assertEqual(page.locator("#healthBanner:visible").count(), 1)
        self.assertEqual(self.problems, [])

    def test_stale_with_no_status_file(self):
        self.serve(datetime.timedelta(days=14, hours=3), None)
        page = self.open()
        self.assertEqual(self.banner(page), "Library data was last refreshed 14 days ago; see the export log on the server.")

    def test_not_stale_at_nine_days_stale_past_ten(self):
        self.serve(datetime.timedelta(days=9, hours=20), self.status_obj())
        self.assertIsNone(self.banner(self.open()))

    def test_stale_but_last_run_ok_still_says_stale(self):
        self.serve(datetime.timedelta(days=12), self.status_obj())
        self.assertIn("12 days ago", self.banner(self.open()))

    def test_failed_and_stale_is_one_banner_with_the_failure(self):
        self.serve(datetime.timedelta(days=30), self.status_obj(consecutive_failures=3, error_class="steam"))
        page = self.open()
        text = self.banner(page)
        self.assertIn("The last refresh failed (steam)", text)
        self.assertNotIn("days ago", text)
        self.assertEqual(page.locator(".banner:visible").count(), 1)

    def test_sample_library_has_no_health_banner(self):
        self.serve(datetime.timedelta(days=30), None)
        (self.server.data / "steam_backlog.json").unlink()
        page = self.context.new_page()
        page.goto(self.url)
        page.wait_for_selector("#sampleBanner:visible", timeout=15000)
        self.assertFalse(page.is_visible("#healthBanner"))

    def test_dismiss_is_remembered_for_the_session(self):
        self.serve(datetime.timedelta(days=2), self.status_obj(consecutive_failures=1, error_class="other"))
        page = self.open()
        self.assertIsNotNone(self.banner(page))
        page.click("#healthDismiss")
        self.assertIsNone(self.banner(page))
        page.reload()
        page.wait_for_function("document.getElementById('who').textContent.includes('Test Persona')", timeout=15000)
        page.wait_for_timeout(200)
        self.assertIsNone(self.banner(page), "still dismissed after a reload in the same tab")
        self.assertEqual(self.problems, [])

    def test_a_different_failure_shows_again_after_a_dismissal(self):
        self.serve(datetime.timedelta(days=2), self.status_obj(consecutive_failures=1, error_class="other"))
        page = self.open()
        page.click("#healthDismiss")
        (self.server.data / "run_status.json").write_text(json.dumps(self.status_obj(consecutive_failures=2, error_class="hltb")))
        page.reload()
        page.wait_for_function("document.getElementById('who').textContent.includes('Test Persona')", timeout=15000)
        page.wait_for_timeout(200)
        self.assertIn("(hltb)", self.banner(page) or "")

    def test_dismiss_works_with_session_storage_unavailable(self):
        self.serve(datetime.timedelta(days=2), self.status_obj(consecutive_failures=1, error_class="other"))
        page = self.open("Object.defineProperty(window, 'sessionStorage', {get() { throw new Error('blocked'); }});")
        self.assertIsNotNone(self.banner(page))
        page.click("#healthDismiss")
        self.assertIsNone(self.banner(page), "dismissed in memory")
        self.assertEqual(self.problems, [])

    def test_refresh_that_fails_raises_the_banner(self):
        self.serve(datetime.timedelta(hours=2), self.status_obj(), export_cmd=FAIL_STUB)
        page = self.open()
        self.assertIsNone(self.banner(page))
        page.click("#refreshBtn")
        page.wait_for_selector("#healthBanner:visible", timeout=30000)
        self.assertIn("The last refresh failed (details)", self.banner(page))


if __name__ == "__main__":
    unittest.main()
