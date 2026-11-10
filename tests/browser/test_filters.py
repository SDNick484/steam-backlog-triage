"""The publisher, developer, Metacritic range, Steam review and "only unknown" filters in a real browser.

The page is served by backlog_server.py with tests/fixtures/stats_library.json (40 games, the first six shaped like
an export from before publishers, developers and reviews were collected). Each filter is set through the URL hash,
as a bookmark would, and the page's count is compared with a count worked out here in Python from the same file.

Set BROWSER_EXECUTABLE to use an installed Chrome, and REQUIRE_BROWSER=1 (CI does) to make a missing Playwright an
error instead of a skip.
"""
from __future__ import annotations

import json
import os
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
GAMES = LIB["games"]
SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "corporation", "gmbh", "sa", "ab", "limited"}


def key(name: str) -> str:
    """An independent, plain implementation of the page's nameKey for ASCII names."""
    toks = re.sub(r"^\W+|\W+$", "", name.lower().strip()).split()
    while len(toks) > 1 and re.sub(r"\W", "", toks[-1]) in SUFFIXES:
        toks.pop()
        toks[-1] = re.sub(r"^\W+|\W+$", "", toks[-1])
    return " ".join(toks)


def keys(g, field):
    return {key(x) for x in g.get(field, []) if key(x)}


REVIEW_LABELS = ["Overwhelmingly Positive", "Very Positive", "Positive", "Mostly Positive", "Mixed", "Mostly Negative",
                 "Negative", "Very Negative", "Overwhelmingly Negative"]


def review_bucket(g):
    r = g.get("reviews")
    if r is None:
        return None
    return r["desc"].lower().replace(" ", "-") if r["desc"] in REVIEW_LABELS else "few"


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class Filters(unittest.TestCase):
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
        self.server = Server(seed=False)
        self.addCleanup(self.server.cleanup)
        atomic_write_json(self.server.data / "steam_backlog.json", LIB)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.port}/"
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.context.route(lambda u: not u.startswith(self.url.rstrip("/")),
                           lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        self.problems: list[str] = []

    def open(self, hash_=""):
        page = self.context.new_page()
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"request failed: {r.url} {r.failure}"))
        page.on("response", lambda r: self.problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)
        page.goto(self.url + (("#" + hash_) if hash_ else ""))
        page.wait_for_function("document.getElementById('who').textContent.includes('Fixture Persona')", timeout=15000)
        return page

    @staticmethod
    def count(page) -> int:
        m = re.search(r"of ([\d,]+) games?", page.inner_text("#countLine"))
        return int(m.group(1).replace(",", "")) if m else 0

    def check(self, hash_, predicate):
        page = self.open("view=all&" + hash_)
        want = sum(1 for g in GAMES if predicate(g))
        self.assertGreater(want, 0, "the fixture should have games for this filter: " + hash_)
        self.assertEqual(self.count(page), want, hash_)
        self.assertEqual(self.problems, [])
        return page

    def test_publisher_groups_spellings_together(self):
        self.check("pub=devolver+digital", lambda g: "devolver digital" in keys(g, "publishers"))

    def test_a_game_with_several_publishers_matches_any_of_them(self):
        self.check("pub=finji&pub=square+enix", lambda g: keys(g, "publishers") & {"finji", "square enix"})

    def test_developer_filter(self):
        self.check("dev=studio+alpha", lambda g: "studio alpha" in keys(g, "developers"))

    def test_publisher_filter_with_unknown_included(self):
        self.check("pub=valve&pubUnk=1", lambda g: not keys(g, "publishers") or "valve" in keys(g, "publishers"))

    def test_only_unknown_publisher(self):
        self.check("unk=pub", lambda g: not keys(g, "publishers"))

    def test_tag_filter_matches_exact_names_any_of(self):
        self.check("tag=Metroidvania", lambda g: "Metroidvania" in g.get("tags", []))
        self.check("tag=Souls-like&tag=Roguelike", lambda g: {"Souls-like", "Roguelike"} & set(g.get("tags", [])))

    def test_tag_filter_forgives_spelling_and_knows_aliases(self):
        for spelling in ("Souls-like", "soulslike", "Souls+like", "SOULSBORNE"):
            self.check("tag=" + spelling, lambda g: "Souls-like" in g.get("tags", []))

    def test_tag_filter_with_unknown_included_and_only_unknown(self):
        self.check("tag=Cozy&tagUnk=1", lambda g: not g.get("tags") or "Cozy" in g["tags"])
        self.check("unk=tag", lambda g: not g.get("tags"))

    def test_tag_picker_lists_tags_with_counts(self):
        page = self.open("view=all")
        buttons = page.query_selector_all('#opts-tag button[data-fa="tag"]')
        from collections import Counter
        counts = Counter(t for g in GAMES for t in set(g.get("tags", [])))
        self.assertEqual(len(buttons), min(30, len(counts)))
        top, n = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        # text_content, not inner_text: the filter panel is collapsed, and inner_text of a hidden element is empty
        self.assertEqual(buttons[0].text_content().split(), top.split() + [str(n)])
        self.assertEqual(self.problems, [])

    def test_only_unknown_works_for_every_dimension(self):
        self.check("unk=dev", lambda g: not keys(g, "developers"))
        self.check("unk=gen", lambda g: not g["genres"])
        self.check("unk=mc", lambda g: not g["metacritic"])
        self.check("unk=yr", lambda g: not g["year"])
        self.check("unk=pt", lambda g: not (g.get("proton") or {}).get("tier"))
        self.check("unk=rv", lambda g: g.get("reviews") is None)

    def test_metacritic_range(self):
        self.check("mc=60&mcmax=69", lambda g: g["metacritic"] and 60 <= g["metacritic"] <= 69)
        self.check("mcmax=59", lambda g: g["metacritic"] and g["metacritic"] <= 59)
        self.check("mc=90", lambda g: g["metacritic"] and g["metacritic"] >= 90)
        self.check("mcmax=59&mcUnk=1", lambda g: not g["metacritic"] or g["metacritic"] <= 59)

    def test_steam_review_levels(self):
        self.check("rv=mixed", lambda g: review_bucket(g) == "mixed")
        self.check("rv=few", lambda g: review_bucket(g) == "few")
        self.check("rv=very-positive&rv=positive&rvUnk=1", lambda g: review_bucket(g) in (None, "very-positive", "positive"))

    def test_invalid_values_in_the_hash_are_ignored(self):
        page = self.open("view=all&mcmax=12&rv=bogus&unk=nothing&mc=60")
        self.assertEqual(self.count(page), sum(1 for g in GAMES if g["metacritic"] and g["metacritic"] >= 60))
        self.assertEqual(self.problems, [])

    def test_the_panel_controls_set_the_filters(self):
        page = self.open("view=all")
        page.click("#fpanel > summary")
        page.fill('input[data-ps="pub"]', "devol")
        self.assertIn("Devolver Digital", page.inner_text("#opts-pub"))
        self.assertNotIn("Square Enix", page.inner_text("#opts-pub"))
        page.click('#opts-pub button[data-fa="pub"]')
        self.assertIn("pub=devolver+digital", page.evaluate("location.hash"))
        self.assertEqual(self.count(page), sum(1 for g in GAMES if "devolver digital" in keys(g, "publishers")))
        # "Only unknown" replaces the chosen values.
        page.check('input[data-fu="pub"]')
        self.assertIn("unk=pub", page.evaluate("location.hash"))
        self.assertNotIn("pub=devolver", page.evaluate("location.hash"))
        self.assertEqual(self.count(page), sum(1 for g in GAMES if not keys(g, "publishers")))
        # Picking a value again turns "only unknown" off.
        page.fill('input[data-ps="pub"]', "valve")
        page.click('#opts-pub button[data-fa="pub"]')
        self.assertNotIn("unk=pub", page.evaluate("location.hash"))
        self.assertEqual(self.problems, [])

    def test_the_picker_labels_use_the_most_common_spelling(self):
        page = self.open("view=all")
        page.click("#fpanel > summary")
        text = page.inner_text("#opts-pub")
        spellings = [p for g in GAMES for p in g.get("publishers", []) if key(p) == "devolver digital"]
        best = max(sorted(set(spellings)), key=spellings.count)
        self.assertIn(best.strip(), text)

    def test_an_old_export_has_no_publisher_data_and_no_errors(self):
        old = {**LIB, "games": [{k: v for k, v in g.items() if k not in ("publishers", "developers", "reviews")} for g in GAMES]}
        atomic_write_json(self.server.data / "steam_backlog.json", old)
        page = self.open("view=all")
        page.click("#fpanel > summary")
        self.assertIn("Re-export with --details", page.inner_text("#opts-pub"))
        self.assertEqual(self.count(page), 40)
        self.assertEqual(self.problems, [])


def fresh_library(drop=()):
    """The fixture with lastPlayed moved to recent dates (days ago: 5, 19, 33, ... so the oldest are past a year), and
    the fields named in `drop` removed from every game."""
    import time
    now = time.time()
    lib = json.loads(json.dumps(LIB))
    k = 0
    for g in lib["games"]:
        if g.get("lastPlayed"):
            g["lastPlayed"] = int(now - (k * 14 + 5) * 86400)
            k += 1
        for f in drop:
            g.pop(f, None)
    return lib


def expected_continue(lib):
    """Plain Python restatement of the rule, newest first, at most 6 (no lanes are set on the served library)."""
    import time
    now = time.time()
    out = []
    for g in lib["games"]:
        h = (g.get("hltb") or {}).get("main") or (g.get("hltb") or {}).get("extra")
        pt, lp = g.get("playtime") or 0, g.get("lastPlayed") or 0
        if pt > 0 and h and pt < 0.8 * h * 60 and lp > 0 and now - lp <= 365 * 86400:
            out.append((lp, g["name"]))
    return [n for _, n in sorted(out, key=lambda t: (-t[0], t[1]))][:6]


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class ContinueShelf(unittest.TestCase):
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

    def serve(self, lib, **ctx):
        self.server = Server(seed=False)
        self.addCleanup(self.server.cleanup)
        atomic_write_json(self.server.data / "steam_backlog.json", lib)
        self.server.start()
        self.url = f"http://127.0.0.1:{self.server.port}/"
        self.context = self.browser.new_context(**{"viewport": {"width": 1280, "height": 900}, **ctx})
        self.addCleanup(self.context.close)
        self.context.route(lambda u: not u.startswith(self.url.rstrip("/")),
                           lambda route: route.fulfill(status=200, content_type="text/css", body=""))
        self.problems: list[str] = []

    def open(self, path="", hash_=""):
        page = self.context.new_page()
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"request failed: {r.url} {r.failure}"))
        page.on("response", lambda r: self.problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)
        page.goto(self.url.rstrip("/") + "/" + path + (("#" + hash_) if hash_ else ""))
        page.wait_for_function("document.getElementById('who').textContent.includes('Fixture Persona')", timeout=15000)
        return page

    @staticmethod
    def shelf(page):
        return page.eval_on_selector_all("#cont .pick h3", "els => els.map(e => e.textContent.trim())")

    def test_shelf_lists_the_in_progress_games_newest_first(self):
        lib = fresh_library()
        want = expected_continue(lib)
        self.assertGreaterEqual(len(want), 3, "the fixture should give the shelf several games")
        self.serve(lib)
        page = self.open()
        self.assertTrue(page.is_visible("#contSec"))
        self.assertEqual(self.shelf(page), want)
        card = page.inner_text("#cont .pick")
        self.assertRegex(card, r" of ~\d+(\.\d)? h")
        self.assertRegex(card, r"\d+% on Deck")
        self.assertEqual(self.problems, [])

    def test_shelf_follows_the_filters_and_hides_when_empty(self):
        lib = fresh_library()
        self.serve(lib)
        page = self.open("", "play=never")           # every in-progress game has been launched
        self.assertFalse(page.is_visible("#contSec"))
        self.assertEqual(self.shelf(page), [])
        page = self.open("", "prog=inprog")           # the filter itself keeps them
        self.assertEqual(self.shelf(page), expected_continue(lib))
        self.assertEqual(self.problems, [])

    def test_lane_buttons_remove_a_game_from_the_shelf(self):
        lib = fresh_library()
        want = expected_continue(lib)
        self.serve(lib)
        page = self.open()
        page.click("#cont .pick:first-child [data-lane=skip]")
        page.wait_for_function(f"document.querySelector('#cont .pick h3').textContent.trim() !== {json.dumps(want[0])}")
        self.assertEqual(self.shelf(page)[:len(want) - 1], want[1:7])
        self.assertEqual(self.problems, [])

    def test_export_without_play_data_has_no_shelf(self):
        self.serve(fresh_library(drop=("deckPlaytime", "lastPlayed")))
        page = self.open()
        self.assertFalse(page.is_visible("#contSec"))
        self.assertEqual(self.problems, [])

    def test_missing_deck_playtime_alone_shows_zero_percent(self):
        lib = fresh_library(drop=("deckPlaytime",))
        self.serve(lib)
        page = self.open()
        self.assertEqual(self.shelf(page), expected_continue(lib))
        self.assertIn("0% on Deck", page.inner_text("#cont .pick"))
        self.assertEqual(self.problems, [])

    def test_themes_and_mobile(self):
        for scheme, size in (("light", {"width": 1280, "height": 900}), ("dark", {"width": 1280, "height": 900}),
                             ("light", {"width": 390, "height": 844}), ("dark", {"width": 390, "height": 844})):
            with self.subTest(scheme=scheme, width=size["width"]):
                self.serve(fresh_library(), color_scheme=scheme, viewport=size)
                page = self.open()
                self.assertTrue(page.is_visible("#contSec"))
                overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                self.assertLessEqual(overflow, 1, "no horizontal scroll")
                page = self.open("stats")
                self.assertFalse(page.is_visible("#contSec"))
                page = self.open("ask")
                self.assertFalse(page.is_visible("#contSec"))
                self.assertEqual(self.problems, [])


if __name__ == "__main__":
    unittest.main()
