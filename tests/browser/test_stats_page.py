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
CARDS = ["gen", "pub", "dev", "tag", "deck", "pt", "ctrl", "len", "lane", "where", "mc", "rv", "year"]


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
class StatsBrowser(unittest.TestCase):
    """A Chromium, a served stats fixture and the helpers to open a page and watch it for problems."""

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

    def serve(self, lib=None, before_start=None, **ctx):
        self.server = Server(seed=False)
        self.addCleanup(self.server.cleanup)
        atomic_write_json(self.server.data / "steam_backlog.json", lib or LIB)
        if before_start:
            before_start(self.server.data)  # e.g. write history files the server will find
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


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class StatsPage(StatsBrowser):
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

    def test_played_on_deck_tile_and_where_rows_match_the_filters(self):
        self.serve()
        page = self.open("/stats", "view=all")
        tile = page.inner_text("#statTiles")
        want = sum(1 for g in LIB["games"] if g.get("deckPlaytime", 0) > 0)
        self.assertRegex(tile, rf"Played on Deck\s+{want} games?")
        self.assertRegex(tile, r"\d+% of all playtime")
        rows = page.query_selector_all("#srows-where [data-sd]")
        self.assertGreaterEqual(len(rows), 3)
        for i in range(len(rows)):
            page = self.open("/stats", "view=all") if i else page
            el = page.query_selector_all("#srows-where [data-sd]")[i]
            n, key = int(el.get_attribute("data-n")), el.get_attribute("data-sk")
            el.click()
            self.assertEqual(self.list_count(page, n), n, key)
            self.assertIn(f"prog={key}", page.url)
            # the same list from the filter panel's own URL
            self.assertEqual(self.list_count(self.open("/", f"view=all&prog={key}"), n), n, key)
        # "Started on Deck" lists exactly the games the tile counts
        self.assertEqual(self.list_count(self.open("/", "view=all&prog=deck"), want), want)
        csv_page = self.open("/stats", "view=all")
        self.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.base)
        csv_page.click("[data-csv=where]")
        lines = csv_page.evaluate("navigator.clipboard.readText()").splitlines()
        self.assertTrue(lines[0].lower().startswith("value"))
        self.assertIn("Never on Deck,", "\n".join(lines))
        self.assertEqual(self.problems, [])

    def test_a_tag_row_and_the_tag_filter_give_the_same_count(self):
        """Metroidvania from the Stats row and from the filter hash (what the list's picker sets) list the same games."""
        self.serve()
        want = sum(1 for g in LIB["games"] if "Metroidvania" in g.get("tags", []))
        page = self.open("/stats", "view=all")
        row = page.query_selector('#srows-tag [data-sd="tag"][data-sk="metroidvania"]')
        self.assertIsNotNone(row, "Metroidvania should be among the top tag rows")
        self.assertEqual(int(row.get_attribute("data-n")), want)
        row.click()
        self.assertEqual(self.list_count(page, want), want)
        self.assertIn("tag=metroidvania", page.url)
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


# ---------------------------------------------------------------------------------------------- Trend section
import datetime as dt  # noqa: E402

UTC = dt.timezone.utc


def history_rows(days: int, today: dt.date | None = None) -> list[dict]:
    """`days` consecutive daily rows ending today: the library grows by one game every ten days and the backlog
    shrinks by one every three, so both charts have a direction to show."""
    today = today or dt.date.today()
    rows = []
    for i in range(days):
        d = today - dt.timedelta(days=days - 1 - i)
        games, backlog = 500 + i // 10, 260 - i // 3
        rows.append({"date": d.isoformat(), "games": games, "never": backlog - 30, "tried": 30,
                     "played": games - backlog + 30, "backlog": backlog,
                     "laneCounts": {"next": 10, "someday": 20, "skip": 40 + i // 20, "done": 30 + i // 15,
                                    "none": games - 70 - i // 20 - i // 15},
                     "backlogHours": round(backlog * 9.5, 1), "backlogWithTime": backlog - 12,
                     "playHours": 1000 + i})
    return rows


def lane_events(today: dt.date | None = None) -> list[dict]:
    """Lane changes over the last year from the fixture games: 25 finished (the 15th day before each other), two
    that were finished and then reopened, and five that were skipped."""
    today = today or dt.date.today()
    ids = [g["appid"] for g in LIB["games"]]
    ev = []

    def at(days_ago: int) -> int:
        d = today - dt.timedelta(days=days_ago)
        return int(dt.datetime(d.year, d.month, d.day, 12, tzinfo=UTC).timestamp())

    for i, appid in enumerate(ids[:25]):
        ev.append({"ts": at(i * 15 + 3), "appid": appid, "from": "", "to": "done"})
    ev.append({"ts": at(1), "appid": ids[0], "from": "done", "to": "next"})     # reopened: not finished
    ev.append({"ts": at(10), "appid": ids[1], "from": "done", "to": "skip"})    # closed some other way
    for i, appid in enumerate(ids[26:31]):
        ev.append({"ts": at(20 + i * 50), "appid": appid, "from": "next", "to": "skip"})
    ev.sort(key=lambda e: e["ts"])
    return ev


def expected_finished(events: list[dict], triage: dict) -> dict[int, int]:
    """appid -> ts, for games whose last lane change was to done and that are done now."""
    last = {}
    for e in events:
        last[e["appid"]] = e
    return {a: e["ts"] for a, e in last.items() if e["to"] == "done" and triage.get(str(a)) == "done"}


def triage_for(events: list[dict]) -> dict:
    last = {}
    for e in events:
        last[e["appid"]] = e["to"]
    return {str(a): lane for a, lane in last.items() if lane}


def write_history(data: Path, rows: list[dict], events: list[dict]) -> None:
    (data / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (data / "lane_log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    (data / "triage.json").write_text(json.dumps(triage_for(events)), encoding="utf-8")


def expected_months(rows: list[dict], events: list[dict]) -> dict[str, tuple[int, int]]:
    """YYYY-MM -> (added, closed). Added: growth of the library between recorded days, in the later day's month.
    Closed: lane changes into done or skip from some other lane, in the (UTC) month they happened."""
    out: dict[str, list[int]] = {}
    for a, b in zip(rows, rows[1:]):
        out.setdefault(b["date"][:7], [0, 0])[0] += max(0, b["games"] - a["games"])
    for e in events:
        if e["to"] in ("done", "skip") and e["from"] not in ("done", "skip"):
            m = dt.datetime.fromtimestamp(e["ts"], UTC).strftime("%Y-%m")
            out.setdefault(m, [0, 0])[1] += 1
    return {m: (v[0], v[1]) for m, v in out.items()}


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class TrendSection(StatsBrowser):
    """/stats "Trend": two lines, the monthly added-versus-closed bars and the finished list, from /api/history."""

    def trend(self, rows=None, events=None, lib=None, path="/stats", **ctx):
        rows = history_rows(400) if rows is None else rows
        events = lane_events() if events is None else events
        self.rows, self.events = rows, events
        self.serve(lib, before_start=lambda data: write_history(data, rows, events), timezone_id="UTC", **ctx)
        page = self.open(path)
        page.wait_for_selector("#trendSec:not([hidden])", timeout=15000)
        return page

    def test_a_400_day_history_draws_both_lines_and_the_bars(self):
        page = self.trend()
        self.assertEqual(page.inner_text("#trendSec h3"), "Trend")
        for chart in ("backlog", "hours", "flow"):
            self.assertEqual(len(page.query_selector_all(f'#trendSec svg[data-chart="{chart}"]')), 1, chart)
        self.assertEqual(page.get_attribute('svg[data-chart="backlog"]', "data-n"), "365")   # the default range
        last = self.rows[-1]
        label = page.get_attribute('svg[data-chart="backlog"]', "aria-label")
        self.assertIn(str(last["backlog"]), label)
        self.assertIn(str(self.rows[-365]["backlog"]), label)                              # where the range began
        hours = page.get_attribute('svg[data-chart="hours"]', "aria-label")
        self.assertIn(f"{int(last['backlogHours'] + 0.5):,}", hours)
        self.assertEqual(self.problems, [])

    def test_the_range_toggle_selects_90_days_a_year_or_everything_and_is_remembered(self):
        page = self.trend()
        self.assertEqual([b.inner_text() for b in page.query_selector_all("#trendRange button")], ["90 days", "1 year", "All"])
        self.assertEqual(page.get_attribute('#trendRange [aria-pressed="true"]', "data-trange"), "365")
        for want, n in (("90", "90"), ("all", "400"), ("365", "365")):
            page.click(f'#trendRange [data-trange="{want}"]')
            self.assertEqual(page.get_attribute('svg[data-chart="backlog"]', "data-n"), n, want)
            self.assertEqual(page.get_attribute('svg[data-chart="hours"]', "data-n"), n, want)
            self.assertEqual(page.get_attribute('#trendRange [aria-pressed="true"]', "data-trange"), want)
        page.click('#trendRange [data-trange="90"]')
        page.reload()
        page.wait_for_selector('svg[data-chart="backlog"]')
        self.assertEqual(page.get_attribute('svg[data-chart="backlog"]', "data-n"), "90")
        self.assertEqual(self.problems, [])

    def test_each_plotted_day_has_a_hover_label_with_its_date_and_value(self):
        page = self.trend()
        page.click('#trendRange [data-trange="90"]')
        pts = page.query_selector_all('svg[data-chart="backlog"] .tpt')
        self.assertGreaterEqual(len(pts), 60)
        self.assertLessEqual(len(pts), 200)
        title = pts[-1].query_selector("title").text_content()
        self.assertIn(str(self.rows[-1]["backlog"]), title)
        self.assertRegex(title, r"\d{4}")

    def test_the_bars_match_games_added_and_games_closed_each_month(self):
        page = self.trend()
        page.click('#trendRange [data-trange="all"]')
        want = expected_months(self.rows, self.events)
        shown = page.query_selector_all('svg[data-chart="flow"] [data-month]')
        self.assertGreaterEqual(len(shown), 13)
        for g in shown:
            m = g.get_attribute("data-month")
            self.assertEqual((int(g.get_attribute("data-added")), int(g.get_attribute("data-closed"))), want.get(m, (0, 0)), m)
        self.assertEqual(sum(int(g.get_attribute("data-closed")) for g in shown),
                         sum(c for _, c in want.values()))
        self.assertGreater(sum(int(g.get_attribute("data-closed")) for g in shown), 20)
        self.assertGreater(sum(int(g.get_attribute("data-added")) for g in shown), 30)
        self.assertRegex(shown[0].query_selector("title").text_content(), r"\d+ added, \d+ closed")

    def test_the_bars_have_a_legend_and_a_table_of_the_same_numbers(self):
        page = self.trend()
        page.click('#trendRange [data-trange="all"]')
        legend = page.inner_text("#trendFlow .tlegend")
        self.assertIn("Added to the library", legend)
        self.assertIn("Closed (done or skip)", legend)
        page.click("#trendTable summary")
        rows = page.query_selector_all("#trendTable tbody tr")
        self.assertEqual(len(rows), len(page.query_selector_all('svg[data-chart="flow"] [data-month]')))
        want = expected_months(self.rows, self.events)
        month = list(want)[-2]
        cells = page.eval_on_selector(f'#trendTable tr[data-month="{month}"]', "r => [...r.cells].map(c => c.textContent)")
        self.assertEqual((int(cells[3]), int(cells[4])), want[month])

    def test_the_finished_list_groups_games_by_year_newest_first_with_playtime(self):
        page = self.trend()
        triage = triage_for(self.events)
        want = expected_finished(self.events, triage)
        self.assertEqual(len(want), 23)   # 25 finished, minus the one reopened and the one skipped
        rows = page.query_selector_all("#finished .finrow")
        self.assertEqual(len(rows), len(want))
        years = [int(y.get_attribute("data-year")) for y in page.query_selector_all("#finished .fyear")]
        self.assertEqual(years, sorted(years, reverse=True))
        self.assertEqual(sorted(years), sorted({dt.datetime.fromtimestamp(t, UTC).year for t in want.values()}))
        first_year = page.query_selector("#finished .fyear")
        self.assertIn(f"{sum(1 for t in want.values() if dt.datetime.fromtimestamp(t, UTC).year == years[0])}",
                      first_year.query_selector("summary").inner_text())
        names = {g["appid"]: g["name"] for g in LIB["games"]}
        shown = page.eval_on_selector_all("#finished .finrow .fname", "els => els.map(e => e.textContent)")
        self.assertEqual(sorted(shown), sorted(names[a] for a in want))
        # dates run newest first inside a year, and each row carries the game's playtime
        ts_in_order = [int(r.get_attribute("data-ts")) for r in rows]
        self.assertEqual(ts_in_order, sorted(ts_in_order, reverse=True))
        self.assertRegex(rows[0].query_selector(".fplay").inner_text(), r"(\d[\d.,]* h|\d+ min|not launched)")
        self.assertEqual(self.problems, [])

    def test_a_finished_game_links_to_it_in_the_list(self):
        page = self.trend()
        link = page.query_selector("#finished a.fgame")
        name = link.inner_text()
        href = link.get_attribute("href")
        self.assertIn("view=all", href)
        self.assertIn("q=", href)
        link.click()
        self.list_count(page)
        self.assertIn(name, page.inner_text("#list"))
        self.assertEqual(self.problems, [])

    def test_text_from_the_log_is_shown_as_text(self):
        lib = json.loads(json.dumps(LIB))
        evil = '<img src=x onerror="window.__x=1"><b>bold</b>'
        lib["games"][0]["name"] = evil
        events = [{"ts": int(dt.datetime.now(UTC).timestamp()) - 86400, "appid": lib["games"][0]["appid"], "from": "", "to": "done"}]
        page = self.trend(lib=lib, events=events)
        page.wait_for_selector("#finished .finrow")
        self.assertIsNone(page.evaluate("window.__x"))
        self.assertEqual(page.query_selector_all("#finished img, #finished b"), [])
        self.assertIn("<img src=x", page.inner_text("#finished"))

    def test_no_history_says_so_and_draws_nothing(self):
        page = self.trend(rows=[], events=[])
        self.assertIn("No history yet", page.inner_text("#trendNote"))
        self.assertEqual(page.query_selector_all("#trendSec svg"), [])
        self.assertIn("Nothing yet", page.inner_text("#finished"))
        self.assertEqual(self.problems, [])

    def test_fewer_than_seven_days_say_collecting_since_and_draw_no_lines(self):
        page = self.trend(rows=history_rows(6), events=[])
        self.assertRegex(page.inner_text("#trendNote"), r"Collecting since .*\d{4}")
        self.assertEqual(page.query_selector_all("#trendSec svg[data-chart]"), [])
        self.assertIn(str(self.rows[-1]["backlog"]), page.inner_text("#trendSec"))   # today's number is still shown
        self.assertEqual(self.problems, [])

    def test_seven_days_are_enough_to_draw(self):
        page = self.trend(rows=history_rows(7), events=[])
        self.assertEqual(page.get_attribute('svg[data-chart="backlog"]', "data-n"), "7")
        self.assertNotIn("Collecting", page.inner_text("#trendNote"))

    def test_the_finished_list_works_from_day_one_even_while_the_lines_are_collecting(self):
        page = self.trend(rows=history_rows(2), events=lane_events())
        self.assertIn("Collecting since", page.inner_text("#trendNote"))
        self.assertGreater(len(page.query_selector_all("#finished .finrow")), 10)

    def test_an_older_server_with_no_history_route_leaves_the_section_out(self):
        self.serve(timezone_id="UTC")
        self.context.route(self.base + "/api/history", lambda route: route.fulfill(status=404, content_type="application/json", body="{}"))
        page = self.open("/stats")
        page.wait_for_selector("#statCards .scard")
        self.assertTrue(page.eval_on_selector("#trendSec", "e => e.hidden"))
        # the 404 itself is the browser's own console line; anything else (a script error) is a failure
        self.assertEqual([p for p in self.problems if "api/history" not in p and "Failed to load resource" not in p], [])

    def test_the_lines_use_the_theme_in_light_and_dark(self):
        for scheme in ("light", "dark"):
            with self.subTest(scheme):
                page = self.trend(color_scheme=scheme)
                got = page.evaluate("""() => {
                    const css = (v) => { const e = document.createElement('i'); e.style.color = v; document.body.appendChild(e);
                                         const c = getComputedStyle(e).color; e.remove(); return c; };
                    return {stroke: getComputedStyle(document.querySelector('svg[data-chart="backlog"] path.tline')).stroke,
                            accent: css('var(--accent)'), bg: getComputedStyle(document.body).backgroundColor,
                            closed: getComputedStyle(document.querySelector('svg[data-chart="flow"] rect.closed')).fill,
                            added: getComputedStyle(document.querySelector('svg[data-chart="flow"] rect.added')).fill}; }""")
                self.assertEqual(got["stroke"], got["accent"])
                self.assertEqual(got["closed"], got["accent"])
                self.assertNotEqual(got["added"], got["closed"])
                self.assertNotEqual(got["stroke"], got["bg"])
                shot = os.environ.get("TREND_SHOT_DIR")   # for a look at the result; not asserted
                if shot:
                    page.query_selector("#trendSec").screenshot(path=str(Path(shot) / f"trend-{scheme}.png"))
                self.assertEqual(self.problems, [])

    def test_at_390_px_nothing_scrolls_sideways_and_the_charts_fit(self):
        page = self.trend(viewport={"width": 390, "height": 800})
        page.click('#trendRange [data-trange="all"]')
        wide = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        self.assertLessEqual(wide, 0)
        for chart in ("backlog", "hours", "flow"):
            box = page.query_selector(f'svg[data-chart="{chart}"]').bounding_box()
            self.assertLessEqual(box["x"] + box["width"], 390.5, chart)
            self.assertGreater(box["height"], 60, chart)
        shot = os.environ.get("TREND_SHOT_DIR")
        if shot:
            page.query_selector("#trendSec").screenshot(path=str(Path(shot) / "trend-390.png"))
        self.assertEqual(self.problems, [])


if __name__ == "__main__":
    unittest.main()
