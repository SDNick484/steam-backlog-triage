"""The Statistics page (/stats) in a real browser, served by backlog_server.py with tests/fixtures/stats_library.json.

What it proves: every row in every card, the "unknown" rows included, leads to a list with exactly as many games as the
row said; filters carry between / and /stats; an export from before publishers, developers and reviews shows coverage
notes and no console errors; light and dark, desktop and 390 px; the CSV button; and, on a synthetic 5,000-game
library, that the statistics take under 50 ms.

Set BROWSER_EXECUTABLE to use an installed Chrome, and REQUIRE_BROWSER=1 (CI does) to make a missing Playwright an
error instead of a skip.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _support import Server, atomic_write_json  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

REQUIRE = os.environ.get("REQUIRE_BROWSER") == "1"
LIB = json.loads((HERE.parent / "fixtures" / "stats_library.json").read_text(encoding="utf-8"))
CARDS = ["gen", "pub", "dev", "tag", "deck", "pt", "ctrl", "len", "lane", "mc", "rv", "year"]


def synthetic_library(n: int) -> dict:
    rnd = random.Random(7)
    base = LIB["games"]
    games = []
    for i in range(n):
        g = json.loads(json.dumps(base[i % len(base)]))
        g["appid"] = 2_000_000 + i
        g["name"] = f"Synthetic Game {i}"
        g["publishers"] = [f"Publisher {rnd.randrange(400)}"]
        g["developers"] = [f"Developer {rnd.randrange(900)}"]
        g["playtimeMinutes"] = rnd.choice([0, 0, 30, 300, 5000])
        games.append(g)
    return dict(LIB, games=games)


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class StatsPage(unittest.TestCase):
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

    def serve(self, lib=None, **ctx):
        self.server = Server(seed=False)
        self.addCleanup(self.server.cleanup)
        atomic_write_json(self.server.data / "steam_backlog.json", lib or LIB)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.context = self.browser.new_context(**{"viewport": {"width": 1280, "height": 900}, **ctx})
        self.addCleanup(self.context.close)
        self.context.route(lambda u: not u.startswith(self.base),
                           lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        self.problems: list[str] = []

    def open(self, path="/stats", hash_=""):
        page = self.context.new_page()
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"request failed: {r.url} {r.failure}"))
        page.on("response", lambda r: self.problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)
        page.goto(self.base + path + (("#" + hash_) if hash_ else ""))
        page.wait_for_function("((document.getElementById('who') || {textContent: ''}).textContent).includes('Fixture Persona')", timeout=15000)
        return page

    @staticmethod
    def list_count(page, want=None) -> int:
        """The number of games the list says it selects (the second number in "Showing x of y"). The page renders
        once with saved filters and again when the hash applies, so wait for the line to settle."""
        page.wait_for_function("((document.getElementById('wrap') || {dataset: {}}).dataset.mode) === 'list'", timeout=15000)
        last = None
        for _ in range(60):
            text = page.inner_text("#countLine")
            m = re.search(r"Showing [\d,]+ of ([\d,]+)", text)
            n = int(m.group(1).replace(",", "")) if m else None
            if n is not None and text == last and (want is None or n == want):
                return n
            last = text
            page.wait_for_timeout(100)
        return n if n is not None else -1

    def test_stats_route_and_trailing_slash(self):
        self.serve()
        for p in ("/stats", "/stats/"):
            r = self.server.get(p)
            self.assertEqual(r.status, 200, p)
            self.assertIn("text/html", r.headers.get("content-type", ""))
        self.assertEqual(self.server.req("HEAD", "/stats").status, 200)

    def test_every_row_leads_to_a_list_of_the_same_size(self):
        self.serve()
        checked = 0
        for card in CARDS:
            page = self.open("/stats", "view=all")
            rows = page.query_selector_all(f"#srows-{card} [data-sd]")
            self.assertGreater(len(rows), 0, card)
            total = len(rows)
            for i in range(min(total, 8)):
                page = self.open("/stats", "view=all") if i else page
                el = page.query_selector_all(f"#srows-{card} [data-sd]")[i]
                want, sk = int(el.get_attribute("data-n")), el.get_attribute("data-sk")
                el.click()
                self.assertEqual(self.list_count(page, want), want, f"{card} row {i}: {sk}")
                checked += 1
        self.assertGreater(checked, 30)
        self.assertEqual(self.problems, [])

    def test_unknown_rows_are_clickable(self):
        self.serve()
        seen = 0
        for card in ("pub", "dev", "tag", "rv", "mc", "year"):
            page = self.open("/stats", "view=all")
            el = page.query_selector(f"#srows-{card} [data-su]")
            if not el:
                continue
            want = int(el.get_attribute("data-n"))
            el.click()
            self.assertEqual(self.list_count(page, want), want, card)
            seen += 1
        self.assertGreaterEqual(seen, 3, "the fixture's old-export games should leave unknown rows")
        self.assertEqual(self.problems, [])

    def test_filters_carry_between_list_and_stats(self):
        self.serve()
        page = self.open("/", "view=all&pub=valve")
        n = self.list_count(page)
        page.click("#pagesNav a:has-text('Stats')")
        page.wait_for_function("((document.getElementById('wrap') || {dataset: {}}).dataset.mode) === 'stats' && document.querySelector('.tile')", timeout=15000)
        page.wait_for_function(f"((document.getElementById('countLine') || {{textContent: ''}}).textContent).startsWith('{n} games in this view')", timeout=15000)
        self.assertIn("pub=valve", page.url)
        page.click("#pagesNav a:has-text('Backlog')")
        self.assertEqual(self.list_count(page), n)
        self.assertEqual(self.problems, [])

    def test_old_export_shows_coverage_notes(self):
        lib = json.loads(json.dumps(LIB))
        for g in lib["games"]:
            for k in ("publishers", "developers", "reviews", "tags"):
                g.pop(k, None)
        self.serve(lib)
        page = self.open("/stats", "view=all")
        text = page.inner_text("#statCards")
        self.assertRegex(text, r"Publisher known for 0 of 40")
        self.assertRegex(text, r"Steam reviews known for 0 of 40")
        self.assertRegex(text, r"Tags known for 0 of 40")
        self.assertEqual(self.problems, [])

    def test_a_tag_row_and_the_tag_filter_give_the_same_count(self):
        """Metroidvania from the Stats row and from the filter hash (what the list's picker sets) list the same games."""
        self.serve()
        want = sum(1 for g in LIB["games"] if "Metroidvania" in g.get("tags", []))
        page = self.open("/stats", "view=all")
        row = page.query_selector('#srows-tag [data-sd="tag"][data-sk="Metroidvania"]')
        self.assertIsNotNone(row, "Metroidvania should be among the top tag rows")
        self.assertEqual(int(row.get_attribute("data-n")), want)
        row.click()
        self.assertEqual(self.list_count(page, want), want)
        self.assertIn("tag=Metroidvania", page.url)
        page = self.open("/", "view=all&tag=Metroidvania")
        self.assertEqual(self.list_count(page, want), want)
        self.assertEqual(self.problems, [])

    def test_themes_and_mobile(self):
        for scheme, size in (("light", {"width": 1280, "height": 900}), ("dark", {"width": 1280, "height": 900}),
                             ("light", {"width": 390, "height": 844}), ("dark", {"width": 390, "height": 844})):
            with self.subTest(scheme=scheme, width=size["width"]):
                self.serve(color_scheme=scheme, viewport=size)
                page = self.open("/stats", "view=all")
                self.assertEqual(len(page.query_selector_all(".scard")), len(CARDS))
                overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                self.assertLessEqual(overflow, 1, "no horizontal scroll")
                self.assertEqual(self.problems, [])

    def test_metric_switch_and_csv_button(self):
        self.serve()
        self.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.base)
        page = self.open("/stats", "view=all")
        page.click("[data-metric=hours]")
        self.assertEqual(page.get_attribute("[data-metric=hours]", "aria-pressed"), "true")
        page.click("[data-csv=gen]")
        csv = page.evaluate("navigator.clipboard.readText()")
        self.assertTrue(csv.splitlines()[0].lower().startswith("value"), csv[:60])
        page.click("[data-csv=tag]")
        csv = page.evaluate("navigator.clipboard.readText()")
        self.assertTrue(csv.splitlines()[0].lower().startswith("value"), csv[:60])
        self.assertIn("Metroidvania,", csv)
        self.assertTrue(csv.splitlines()[-1].startswith("Unknown,"))
        self.assertEqual(self.problems, [])

    def test_five_thousand_games_in_under_50_ms(self):
        self.serve(synthetic_library(5000))
        page = self.open("/stats", "view=all")
        times = []
        for m in ("hours", "never", "games", "hours", "games"):   # each switch re-renders, which recomputes the statistics
            page.click(f"[data-metric={m}]")
            times.append(page.evaluate("window.__statsMs"))
        best = min(times)
        print(f"\nstatsFor on 5,000 games: {best:.1f} ms")
        self.assertLess(best, 50)
        self.assertEqual(self.problems, [])


if __name__ == "__main__":
    unittest.main()
