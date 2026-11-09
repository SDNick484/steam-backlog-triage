"""steam_backlog_export.py against a local mock of the Steam Web API, the Steam store and ProtonDB.

The three base URLs are overridable with STEAM_API_BASE, STEAM_STORE_BASE and PROTONDB_BASE (the script's own
test seam), so no test touches the network. time.sleep is replaced (tests/exporter_shim.py) so back-off doesn't
slow the suite, and every requested sleep is recorded so the tests can still check it. howlongtobeatpy is hidden
via PYTHONPATH, so no test reaches the real, unofficial HowLongToBeat service.

Expected library shape: schema 2 as described in section 3 of docs/backlog-api-contract.md (in SDNick484/mcp-server-steam-backlog) (the file the server
serves at GET /api/library).
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _support import DEFAULT_TAG_NAMES, MockSteam, fake_steamid, run_exporter  # noqa: E402
import steam_backlog_export as exporter  # noqa: E402

GAME_KEYS = {"appid", "name", "playtime", "deckPlaytime", "lastPlayed", "deck", "hltb", "genres", "metacritic",
             "year", "controller", "publishers", "developers", "reviews", "tags", "proton"}
FAKE_KEY = "FAKEKEY0123456789"


class ExporterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.mock = MockSteam()
        self.mock.__enter__()
        self.addCleanup(self.mock.__exit__, None, None, None)
        self.n = 0

    def run_ex(self, *args, mock=None, key=FAKE_KEY, steamid=True, env=None):
        """Run the exporter in self.dir. Returns (proc, sleeps)."""
        self.n += 1
        log = self.dir / f"sleeps{self.n}.json"
        argv = list(args)
        if steamid:
            argv += ["--steamid", fake_steamid()]
        if key:
            argv += ["--key", key]
        p = run_exporter(self.dir, argv, self.mock if mock is None else mock, sleep_log=log, extra_env=env)
        sleeps = json.loads(log.read_text()) if log.exists() else []
        return p, sleeps

    def library(self, name="steam_backlog.json"):
        return json.loads((self.dir / name).read_text())

    def ok(self, *args, **kw):
        p, sleeps = self.run_ex(*args, **kw)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p, sleeps


class OutputShape(ExporterCase):
    def test_top_level_fields(self):
        """Contract section 3: schema 2; source; exportedAt ISO 8601 UTC; steamid; persona; games."""
        p, _ = self.ok()
        lib = self.library()
        self.assertEqual(set(lib), {"schema", "source", "exportedAt", "steamid", "persona", "games"})
        self.assertEqual(lib["schema"], 2)
        self.assertEqual(lib["source"], "steam_backlog_export.py")
        self.assertRegex(lib["exportedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
        self.assertEqual(lib["steamid"], fake_steamid())
        self.assertEqual(lib["persona"], "Test Persona")

    def test_game_records_have_exactly_the_documented_fields_and_types(self):
        """Contract section 3, per-game fields."""
        self.ok()
        games = self.library()["games"]
        self.assertEqual(len(games), 4)
        for g in games:
            with self.subTest(g["name"]):
                self.assertEqual(set(g), GAME_KEYS)
                self.assertIsInstance(g["appid"], int)
                for k in ("playtime", "deckPlaytime", "lastPlayed", "deck"):
                    self.assertIsInstance(g[k], int, k)
                self.assertIn(g["deck"], (0, 1, 2, 3))
                self.assertIsInstance(g["genres"], list)
                self.assertIsNone(g["hltb"])
                self.assertEqual(g["proton"], {"tier": "gold", "total": 120})

    def test_games_are_sorted_by_name_ignoring_case(self):
        """backlog export sorts owned games by lower-cased name ('test game alpha' sorts before 'Test Game Bravo')."""
        self.ok()
        self.assertEqual([g["name"] for g in self.library()["games"]],
                         ["test game alpha", "Test Game Bravo", "Test Game Charlie", "Test Game Delta"])

    def test_playtime_fields_are_mapped_and_defaulted(self):
        """playtime_forever -> playtime, playtime_deck_forever -> deckPlaytime, rtime_last_played -> lastPlayed;
        missing values become 0."""
        self.ok()
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual((by[2003]["playtime"], by[2003]["deckPlaytime"], by[2003]["lastPlayed"]), (600, 120, 1710000000))
        self.assertEqual((by[2001]["playtime"], by[2001]["deckPlaytime"], by[2001]["lastPlayed"]), (0, 0, 0))

    def test_logs_go_to_stderr_and_stdout_stays_empty(self):
        """The server merges both streams, but the script's contract is: progress on stderr."""
        p, _ = self.ok()
        self.assertEqual(p.stdout, "")
        self.assertIn("Test Persona: 4 games", p.stderr)
        self.assertRegex(p.stderr, r"Wrote steam_backlog\.json: 4 games, 1 never launched, 0 with HLTB times\.")

    def test_the_key_is_never_printed(self):
        """Secrets must not reach the log (the log is shown in the dashboard after redaction, but is not trusted)."""
        p, _ = self.ok()
        self.assertNotIn(FAKE_KEY, p.stderr + p.stdout)

    def test_the_library_is_written_through_a_temp_file(self):
        """The server hashes the library for its ETag, so it must never see a half-written file: write beside it
        and rename over it. No .tmp file may be left behind."""
        self.ok()
        self.assertEqual(sorted(x.name for x in self.dir.glob("*.tmp")), [])
        self.assertTrue((self.dir / "steam_backlog_cache.json").exists())


class Limit(ExporterCase):
    def test_limit_processes_only_the_first_n_games_by_name(self):
        """--limit N: only the first N games (after sorting) are looked up and written."""
        self.ok("--limit", "2")
        self.assertEqual([g["name"] for g in self.library()["games"]], ["test game alpha", "Test Game Bravo"])
        self.assertEqual(self.mock.count("deck:"), 2)
        self.assertEqual(self.mock.count("proton:"), 2)
        self.assertEqual(set(self.mock.hits) - {"owned", "persona"}, {"deck:2001", "deck:2002", "proton:2001", "proton:2002"})


class NoHowLongToBeat(ExporterCase):
    def test_runs_without_the_package_and_leaves_hltb_empty(self):
        """howlongtobeatpy is the one optional dependency: without it the export still succeeds, says so, and
        every game's hltb is null."""
        p, _ = self.ok()
        self.assertIn("HowLongToBeat: skipped (pip install howlongtobeatpy to enable).", p.stderr)
        self.assertTrue(all(g["hltb"] is None for g in self.library()["games"]))

    def test_skip_hltb_flag_does_not_even_try_the_import(self):
        """--skip-hltb: no HowLongToBeat message at all."""
        p, _ = self.ok("--skip-hltb")
        self.assertNotIn("HowLongToBeat", p.stderr)


class Cache(ExporterCase):
    def test_second_run_reuses_the_cache(self):
        """Everything looked up is cached (steam_backlog_cache.json), so a second run asks Steam and ProtonDB
        only for the library and the persona, and writes the same games."""
        self.ok()
        first = self.library()
        self.assertEqual(self.mock.count("deck:"), 4)
        self.assertEqual(self.mock.count("proton:"), 4)
        p, _ = self.ok()
        self.assertEqual(self.mock.count("deck:"), 4, "no new deck requests")
        self.assertEqual(self.mock.count("proton:"), 4, "no new ProtonDB requests")
        self.assertEqual(self.mock.hits["owned"], 2)
        self.assertIn("Steam Deck compatibility: everything cached.", p.stderr)
        self.assertIn("ProtonDB: everything cached.", p.stderr)
        second = self.library()
        first.pop("exportedAt"), second.pop("exportedAt")
        self.assertEqual(first, second)

    def test_refresh_ignores_the_cache(self):
        """--refresh: fetch everything again."""
        self.ok()
        self.ok("--refresh")
        self.assertEqual(self.mock.count("deck:"), 8)
        self.assertEqual(self.mock.count("proton:"), 8)

    def test_new_games_are_the_only_ones_fetched_later(self):
        """A game added after the first run is fetched; the rest come from the cache."""
        self.ok()
        self.mock.games = self.mock.games + [{"appid": 2005, "name": "Test Game Echo", "playtime_forever": 1}]
        self.ok()
        self.assertEqual(self.mock.hits["deck:2005"], 1)
        self.assertEqual(self.mock.hits["deck:2001"], 1)
        self.assertEqual(len(self.library()["games"]), 5)

    def test_cache_file_has_all_kinds(self):
        """The cache holds deck, hltb, details, proton, reviews, tags and taglist sections (CACHE_KINDS)."""
        self.ok()
        cache = json.loads((self.dir / "steam_backlog_cache.json").read_text())
        self.assertEqual(set(cache), {"deck", "hltb", "details", "proton", "reviews", "tags", "taglist"})
        self.assertEqual(set(cache["deck"]), {"2001", "2002", "2003", "2004"})
        self.assertEqual(set(cache["deck"]["2001"]), {"t", "v"})

    def test_corrupt_cache_is_ignored(self):
        """An unreadable cache file is reported and replaced, not fatal."""
        (self.dir / "steam_backlog_cache.json").write_text("{not json")
        p, _ = self.ok()
        self.assertIn("unreadable; starting fresh", p.stderr)
        self.assertEqual(self.mock.count("deck:"), 4)


class StoreRateLimiting(ExporterCase):
    def test_a_429_with_retry_after_is_waited_out_then_succeeds(self):
        """The store answers 429 once: the exporter waits Retry-After seconds, retries, and uses the good answer."""
        self.mock.deck[2002] = [(429, b"", {"Retry-After": "7"}), (200, {"success": 1, "results": {"resolved_category": 2}})]
        p, sleeps = self.ok()
        self.assertIn("HTTP 429 from", p.stderr)
        self.assertIn([7.0, "http_json"], sleeps)
        self.assertEqual(self.mock.hits["deck:2002"], 2)
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2002]["deck"], 2)

    def test_a_429_without_retry_after_backs_off_exponentially(self):
        """No Retry-After: wait 5 s, then 10 s, ... (delay doubles, capped at 120)."""
        self.mock.deck[2002] = [(429, b"", {}), (429, b"", {}), (200, {"success": 1, "results": {"resolved_category": 1}})]
        _, sleeps = self.ok()
        waits = [s for s, who in sleeps if who == "http_json"]
        self.assertEqual(waits, [5.0, 10.0])
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2002]["deck"], 1)

    def test_a_server_error_is_retried_too(self):
        """5xx is treated like 429."""
        self.mock.deck[2002] = [(503, b"", {}), (200, {"success": 1, "results": {"resolved_category": 3}})]
        _, sleeps = self.ok()
        self.assertEqual([s for s, who in sleeps if who == "http_json"], [5.0])

    def test_a_404_is_not_retried_and_not_cached(self):
        """Other 4xx: the answer is None ('network trouble'): the game exports as deck 0 and is retried next run."""
        self.mock.deck[2002] = [(404, b"{}", {})]
        _, sleeps = self.ok()
        self.assertEqual(self.mock.hits["deck:2002"], 1)
        self.assertEqual([s for s, who in sleeps if who == "http_json"], [])
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2002]["deck"], 0)
        self.ok()
        self.assertEqual(self.mock.hits["deck:2002"], 2, "not cached, so fetched again")
        self.assertEqual(self.mock.hits["deck:2001"], 1, "the others were cached")

    def test_an_unknown_deck_category_is_stored_as_unknown(self):
        """A resolved_category outside 0-3 becomes 0 (Unknown)."""
        self.mock.deck[2002] = [(200, {"success": 1, "results": {"resolved_category": 99}})]
        self.ok()
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2002]["deck"], 0)


class ProtonDbGarbage(ExporterCase):
    def test_non_json_is_retried_then_skipped_without_failing_the_export(self):
        """ProtonDB is unofficial: an HTML error page behind a 200 is retried (5, 10, 20, 40 s), then given up
        on. The export still succeeds, that game's proton is null, and it is not cached (retried next run)."""
        self.mock.proton[2002] = [(200, b"<html>maintenance</html>", {})]
        p, sleeps = self.ok()
        self.assertIn("giving up on", p.stderr)
        self.assertEqual([s for s, who in sleeps if who == "http_json"], [5.0, 10.0, 20.0, 40.0])
        self.assertEqual(self.mock.hits["proton:2002"], 5)
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertIsNone(by[2002]["proton"])
        self.assertEqual(by[2001]["proton"], {"tier": "gold", "total": 120})
        self.ok()
        self.assertEqual(self.mock.hits["proton:2002"], 10)
        self.assertEqual(self.mock.hits["proton:2001"], 1)

    def test_valid_json_of_the_wrong_shape_is_tolerated(self):
        """'Missing or unexpected fields come back as tier None rather than failing the export.'"""
        self.mock.proton[2001] = [(200, [1, 2, 3], {})]
        self.mock.proton[2002] = [(200, {"tier": "ultra-mega", "total": -5}, {})]
        self.mock.proton[2003] = [(200, {"tier": 7, "total": "many"}, {})]
        self.mock.proton[2004] = [(200, {}, {})]
        self.ok()
        got = {g["appid"]: g["proton"] for g in self.library()["games"]}
        for appid in (2001, 2002, 2003, 2004):
            self.assertEqual(got[appid], {"tier": None, "total": 0}, appid)

    def test_a_known_tier_passes_through(self):
        """Tiers on the allow-list are kept with their report count."""
        self.mock.proton[2001] = [(200, {"tier": "platinum", "total": 4321}, {})]
        self.ok()
        got = {g["appid"]: g["proton"] for g in self.library()["games"]}
        self.assertEqual(got[2001], {"tier": "platinum", "total": 4321})


class StoreDetails(ExporterCase):
    def test_details_are_off_by_default(self):
        """Store details take ~1.6 s a game, so they are opt-in (--details)."""
        self.ok()
        self.assertEqual(self.mock.count("details:"), 0)
        self.assertTrue(all(g["genres"] == [] and g["year"] is None for g in self.library()["games"]))

    def test_details_fill_genres_metacritic_year_and_controller(self):
        """--details: genres, metacritic score, release year and controller support from appdetails."""
        self.mock.details[2002] = [(200, {"2002": {"success": True, "data": {
            "genres": [{"description": "RPG"}], "metacritic": {"score": 91},
            "release_date": {"date": "Mar 3, 2019"}, "categories": [{"id": 18}]}}}, {})]
        self.mock.details[2003] = [(200, {"2003": {"success": False}}, {})]
        p, _ = self.ok("--details")
        self.assertRegex(p.stderr, r"Store details: fetching 4 games")
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual((by[2002]["genres"], by[2002]["metacritic"], by[2002]["year"], by[2002]["controller"]),
                         (["RPG"], 91, 2019, "partial"))
        self.assertEqual((by[2003]["genres"], by[2003]["metacritic"], by[2003]["year"], by[2003]["controller"]),
                         ([], None, None, None))
        self.assertEqual((by[2001]["genres"], by[2001]["year"], by[2001]["controller"]),
                         (["Action", "Indie"], 2020, "full"))

    def test_details_fill_publishers_and_developers(self):
        """--details: publishers and developers from appdetails; [] for an unsuccessful lookup and without --details."""
        self.mock.details[2002] = [(200, appdetails(2002, publishers=["Devolver Digital", "Other Pub"],
                                                    developers=["Some Studio"]), {})]
        self.mock.details[2003] = [(200, {"2003": {"success": False}}, {})]
        self.ok("--details")
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual((by[2002]["publishers"], by[2002]["developers"]), (["Devolver Digital", "Other Pub"], ["Some Studio"]))
        self.assertEqual((by[2003]["publishers"], by[2003]["developers"]), ([], []))
        self.assertEqual((by[2001]["publishers"], by[2001]["developers"]), (["Test Publisher Inc."], ["Test Studio"]))

    def test_without_details_the_name_lists_are_empty(self):
        self.ok()
        for g in self.library()["games"]:
            self.assertEqual((g["publishers"], g["developers"]), ([], []))

    def test_an_old_cache_entry_is_fetched_once_more_to_backfill_names(self):
        """A details entry cached before publishers/developers existed counts as a miss for this run; entries that
        already have both keys, and entries that are not cached at all, behave as before."""
        now = int(time.time())
        old = {"genres": ["RPG"], "metacritic": 80, "year": 2019, "controller": None}
        new = {"genres": ["Action"], "metacritic": None, "year": 2020, "controller": "full",
               "publishers": ["Kept Pub"], "developers": ["Kept Dev"]}
        (self.dir / "steam_backlog_cache.json").write_text(json.dumps({
            "details": {"2001": {"t": now, "v": new}, "2002": {"t": now, "v": old}}}))
        p, _ = self.ok("--details")
        self.assertIn("Store details: 1 cached games lack publishers and developers; fetching them again.", p.stderr)
        self.assertRegex(p.stderr, r"Store details: fetching 3 games")
        self.assertEqual(self.mock.hits.get("details:2001", 0), 0, "a current entry is not fetched")
        self.assertEqual(self.mock.hits["details:2002"], 1)
        self.assertEqual(self.mock.hits["details:2003"], 1)
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual((by[2001]["publishers"], by[2001]["developers"]), (["Kept Pub"], ["Kept Dev"]))
        self.assertEqual(by[2002]["publishers"], ["Test Publisher Inc."])
        # The backfill is a one-off: the next run finds everything current.
        p, _ = self.ok("--details")
        self.assertNotIn("lack publishers", p.stderr)
        self.assertIn("Store details: everything cached.", p.stderr)
        self.assertEqual(self.mock.hits["details:2002"], 1)

    def test_reviews_are_fetched_with_details_and_land_in_the_record(self):
        self.mock.reviews[2002] = [(200, review_payload(2000, 1500, "Very Positive"), {})]
        self.mock.reviews[2003] = [(200, review_payload(0, 0, "No user reviews"), {})]
        p, _ = self.ok("--details")
        self.assertRegex(p.stderr, r"Store reviews: fetching 4 games")
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2002]["reviews"], {"total": 2000, "pct": 75, "desc": "Very Positive"})
        self.assertEqual(by[2003]["reviews"], {"total": 0, "pct": None, "desc": "No user reviews"})
        self.assertEqual(by[2001]["reviews"], {"total": 100, "pct": 90, "desc": "Very Positive"})

    def test_reviews_are_off_without_details(self):
        self.ok()
        self.assertEqual(self.mock.count("reviews:"), 0)
        self.assertTrue(all(g["reviews"] is None for g in self.library()["games"]))

    def test_a_reviews_network_failure_is_not_cached_and_the_record_is_null(self):
        """Like ProtonDB: trouble gives None, nothing is cached, the next run asks again."""
        self.mock.reviews[2002] = [(404, b"", {})]
        self.ok("--details")
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertIsNone(by[2002]["reviews"])
        self.assertNotIn("2002", json.loads((self.dir / "steam_backlog_cache.json").read_text())["reviews"])
        self.mock.reviews[2002] = [(200, review_payload(40, 30, "Mostly Positive"), {})]
        self.mock.hits.pop("reviews:2002")
        self.ok("--details")
        self.assertEqual(self.mock.hits["reviews:2002"], 1)
        self.assertEqual(self.mock.count("reviews:"), 4, "three from the first run, then only the failed game")
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2002]["reviews"], {"total": 40, "pct": 75, "desc": "Mostly Positive"})

    def test_reviews_stop_after_a_run_of_failures_and_the_next_run_finishes(self):
        """Steam throttles the store with pages that aren't JSON. Don't wait out every game: stop, keep the rest
        uncached, say why, and still write the library."""
        games = [{"appid": 3000 + i, "name": f"Throttle Game {i:02d}", "playtime_forever": 0} for i in range(30)]
        mock = MockSteam(games=games)
        mock.__enter__()
        self.addCleanup(mock.__exit__, None, None, None)
        for g in games:
            mock.reviews[g["appid"]] = [(200, b"<html>slow down</html>", {})]
        p, _ = self.run_ex("--details", mock=mock)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Stopping the reviews step", p.stderr)
        self.assertIn("next run", p.stderr)
        asked = mock.count("reviews:")
        limit = exporter.REVIEWS_MAX_FAILURES
        # A failing game is asked once, with no retries (retrying while throttled makes it worse); two workers can overshoot by one.
        self.assertLessEqual(asked, limit + 1, "it kept asking after the limit")
        self.assertEqual(len(self.library()["games"]), 30, "the library is still written")
        self.assertTrue(all(g["reviews"] is None for g in self.library()["games"]))
        # Steam recovers: the next run fetches every game it has not got.
        for g in games:
            mock.reviews.pop(g["appid"])
        p, _ = self.run_ex("--details", mock=mock)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(all(g["reviews"] is not None for g in self.library()["games"]))

    def test_reviews_are_cached_for_thirty_days(self):
        self.assertEqual(exporter.TTL_REVIEWS, 30)
        self.ok("--details")
        old = json.loads((self.dir / "steam_backlog_cache.json").read_text())
        old["reviews"]["2001"]["t"] -= 31 * 86400
        old["reviews"]["2002"]["t"] -= 29 * 86400
        (self.dir / "steam_backlog_cache.json").write_text(json.dumps(old))
        self.ok("--details")
        self.assertEqual(self.mock.hits["reviews:2001"], 2)
        self.assertEqual(self.mock.hits["reviews:2002"], 1)

    def test_an_old_cache_entry_is_left_alone_without_details(self):
        """No --details: no store requests at all, and the old entry still supplies its genres (names are [])."""
        now = int(time.time())
        (self.dir / "steam_backlog_cache.json").write_text(json.dumps({
            "details": {"2002": {"t": now, "v": {"genres": ["RPG"], "metacritic": 80, "year": 2019, "controller": None}}}}))
        self.ok()
        self.assertEqual(self.mock.count("details:"), 0)
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual((by[2002]["genres"], by[2002]["publishers"], by[2002]["developers"]), (["RPG"], [], []))


def appdetails(appid, **data):
    return {str(appid): {"success": True, "data": data}}


class FetchDetailsNames(unittest.TestCase):
    """fetch_details against canned appdetails JSON (no network): publishers and developers."""

    def fetch(self, payload, appid=7):
        with mock.patch.object(exporter, "http_json", return_value=payload):
            return exporter.fetch_details(appid, exporter.RateLimiter(0))

    def test_names_are_kept_in_order(self):
        d = self.fetch(appdetails(7, publishers=["Devolver Digital", "Second Pub"], developers=["Studio A", "Studio B"]))
        self.assertEqual(d["publishers"], ["Devolver Digital", "Second Pub"])
        self.assertEqual(d["developers"], ["Studio A", "Studio B"])

    def test_names_are_trimmed_and_empty_strings_dropped(self):
        d = self.fetch(appdetails(7, publishers=[""], developers=["  Studio A  ", "", "   ", "Studio B"]))
        self.assertEqual(d["publishers"], [])
        self.assertEqual(d["developers"], ["Studio A", "Studio B"])

    def test_missing_or_malformed_keys_give_empty_lists(self):
        for extra in ({}, {"publishers": None, "developers": "Studio"}, {"publishers": [1, None, {"x": 1}], "developers": {}}):
            with self.subTest(extra):
                d = self.fetch(appdetails(7, genres=[{"description": "Action"}], **extra))
                self.assertEqual((d["publishers"], d["developers"]), ([], []))
                self.assertEqual(d["genres"], ["Action"])

    def test_an_unsuccessful_lookup_returns_empty_lists_not_none(self):
        d = self.fetch({"7": {"success": False}})
        self.assertEqual(d, {"genres": [], "metacritic": None, "year": None, "controller": None,
                             "publishers": [], "developers": []})

    def test_network_trouble_is_still_none(self):
        self.assertIsNone(self.fetch(None))

    def test_details_current(self):
        self.assertTrue(exporter.details_current({"t": 1, "v": {"genres": [], "publishers": [], "developers": []}}))
        self.assertFalse(exporter.details_current({"t": 1, "v": {"genres": [], "publishers": []}}))
        self.assertFalse(exporter.details_current({"t": 1, "v": {"genres": [], "developers": []}}))
        self.assertFalse(exporter.details_current({"t": 1, "v": {"genres": []}}))
        self.assertFalse(exporter.details_current(None))


def review_payload(total, positive, desc="Very Positive"):
    return {"success": 1, "query_summary": {"num_reviews": 0, "review_score": 8, "review_score_desc": desc,
                                            "total_positive": positive, "total_negative": total - positive,
                                            "total_reviews": total}, "reviews": []}


class FailureStreakTests(unittest.TestCase):
    def test_trips_once_after_the_limit_in_a_row(self):
        s = exporter.FailureStreak(3)
        self.assertEqual([s.record(False) for _ in range(5)], [False, False, True, False, False])
        self.assertTrue(s.stopped)

    def test_a_success_resets_the_count(self):
        s = exporter.FailureStreak(3)
        for ok in (False, False, True, False, False):
            self.assertFalse(s.record(ok))
        self.assertFalse(s.stopped)
        self.assertTrue(s.record(False))

    def test_the_reviews_limit_is_small_but_not_one(self):
        self.assertGreaterEqual(exporter.REVIEWS_MAX_FAILURES, 3)
        self.assertLessEqual(exporter.REVIEWS_MAX_FAILURES, 10)


class FetchReviews(unittest.TestCase):
    """fetch_reviews and review_record against canned appreviews JSON (no network)."""

    def fetch(self, payload):
        with mock.patch.object(exporter, "http_json", return_value=payload) as m:
            r = exporter.fetch_reviews(7, exporter.RateLimiter(0))
        return r, m

    def test_the_summary_is_reduced_to_total_positive_desc(self):
        r, m = self.fetch(review_payload(1234, 1100, "Very Positive"))
        self.assertEqual(r, {"total": 1234, "positive": 1100, "desc": "Very Positive"})
        url = m.call_args.args[0]
        self.assertRegex(url, r"/appreviews/7\?")
        for part in ("json=1", "num_per_page=0", "language=all", "purchase_type=all"):
            self.assertIn(part, url)

    def test_no_reviews(self):
        r, _ = self.fetch(review_payload(0, 0, "No user reviews"))
        self.assertEqual(r, {"total": 0, "positive": 0, "desc": "No user reviews"})

    def test_missing_or_malformed_summary_is_zero_not_an_error(self):
        for payload in ({}, {"success": 0}, {"query_summary": []}, [1, 2],
                        {"query_summary": {"total_reviews": "many", "total_positive": -4, "review_score_desc": 5}}):
            with self.subTest(payload):
                r, _ = self.fetch(payload)
                self.assertEqual(r, {"total": 0, "positive": 0, "desc": None})

    def test_positive_never_exceeds_total(self):
        r, _ = self.fetch(review_payload(10, 50))
        self.assertEqual((r["total"], r["positive"]), (10, 10))

    def test_network_trouble_is_none_so_it_is_not_cached(self):
        r, _ = self.fetch(None)
        self.assertIsNone(r)

    def test_a_failed_request_is_not_retried_and_requests_are_spaced(self):
        _, m = self.fetch(review_payload(1, 1, "x"))
        self.assertEqual(m.call_args.kwargs.get("retries"), 0)
        self.assertGreaterEqual(exporter.REVIEWS_INTERVAL, 3.0)

    def test_review_record(self):
        rec = exporter.review_record
        self.assertEqual(rec({"t": 1, "v": {"total": 3, "positive": 2, "desc": "3 user reviews"}}),
                         {"total": 3, "pct": 67, "desc": "3 user reviews"})
        self.assertEqual(rec({"t": 1, "v": {"total": 200, "positive": 199, "desc": "x"}})["pct"], 100)
        self.assertEqual(rec({"t": 1, "v": {"total": 0, "positive": 0, "desc": None}}),
                         {"total": 0, "pct": None, "desc": None})
        self.assertIsNone(rec(None))


class BadInput(ExporterCase):
    def test_no_key_is_a_clear_error_before_any_request(self):
        """STEAM_API_KEY or --key is required."""
        p, _ = self.run_ex(key=None)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Set STEAM_API_KEY", p.stderr)
        self.assertEqual(self.mock.hits, {})
        self.assertFalse((self.dir / "steam_backlog.json").exists())

    def test_key_from_the_environment(self):
        """$STEAM_API_KEY is the default for --key (how run-export.sh passes it)."""
        p, _ = self.run_ex(key=None, env={"STEAM_API_KEY": FAKE_KEY})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_bad_steamid_is_rejected(self):
        """--steamid must be 17 digits."""
        p, _ = self.run_ex(steamid=False, key=FAKE_KEY)
        self.assertNotEqual(p.returncode, 0)
        p, _ = self.run_ex("--steamid", "123", steamid=False)
        self.assertIn("17-digit", p.stderr)

    def test_a_profile_url_with_a_numeric_id_needs_no_lookup(self):
        """--profile .../profiles/<17 digits> is read directly."""
        p, _ = self.run_ex("--profile", f"https://steamcommunity.com/profiles/{fake_steamid()}/", steamid=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.library()["steamid"], fake_steamid())

    def test_persona_missing_falls_back_to_the_steamid_in_the_log_and_null_in_the_file(self):
        """No persona name: the log line uses the SteamID, the library says persona null."""
        mock = MockSteam(persona=None)
        mock.__enter__()
        self.addCleanup(mock.__exit__, None, None, None)
        p, _ = self.ok(mock=mock)
        self.assertIsNone(self.library()["persona"])
        self.assertIn(f"{fake_steamid()}: 4 games", p.stderr)


if __name__ == "__main__":
    unittest.main()


def items_payload(*items):
    return {"response": {"store_items": list(items)}}


def item(appid, tagids=None, success=1):
    d = {"item_type": 0, "id": appid, "success": success, "appid": appid if success == 1 else 0}
    if tagids is not None:
        d["tagids"] = tagids
        d["tags"] = [{"tagid": t, "weight": 10} for t in tagids]
    return d


class FetchTags(unittest.TestCase):
    """fetch_tags and fetch_taglist against canned JSON (no network)."""

    def tags(self, payload, appids=(7, 8)):
        with mock.patch.object(exporter, "http_json", return_value=payload) as m:
            return exporter.fetch_tags(list(appids), exporter.RateLimiter(0)), m

    def test_ids_are_kept_in_steams_order(self):
        got, _ = self.tags(items_payload(item(7, [29482, 4026, 122]), item(8, [19])))
        self.assertEqual(got, {7: [29482, 4026, 122], 8: [19]})

    def test_the_request_asks_for_tag_counts_for_every_app(self):
        _, m = self.tags(items_payload())
        url = m.call_args[0][0]
        self.assertIn("/IStoreBrowseService/GetItems/v1?input_json=", url)
        import urllib.parse
        req = json.loads(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["input_json"][0])
        self.assertEqual([i["appid"] for i in req["ids"]], [7, 8])
        self.assertTrue(req["data_request"]["include_tag_count"])

    def test_more_than_tags_max_ids_are_cut_in_order(self):
        ids = list(range(100, 100 + exporter.TAGS_MAX + 7))
        got, _ = self.tags(items_payload(item(7, ids)), appids=(7,))
        self.assertEqual(got[7], ids[:exporter.TAGS_MAX])
        self.assertEqual(len(got[7]), 10)

    def test_an_app_the_store_does_not_know_is_an_empty_answer(self):
        got, _ = self.tags(items_payload(item(7, success=15), item(8, [19])))
        self.assertEqual(got, {7: [], 8: [19]})

    def test_an_app_absent_from_the_response_is_left_out_so_it_is_not_cached(self):
        got, _ = self.tags(items_payload(item(8, [19])))
        self.assertEqual(got, {8: [19]})

    def test_an_item_nobody_asked_for_is_ignored(self):
        got, _ = self.tags(items_payload(item(99, [19]), item(8, [19])))
        self.assertEqual(got, {8: [19]})

    def test_a_failed_call_or_wrong_shape_is_none(self):
        for payload in (None, [], {}, {"response": {}}, {"response": {"store_items": "x"}}):
            self.assertIsNone(self.tags(payload)[0], payload)

    def test_malformed_tag_ids_are_dropped(self):
        got, _ = self.tags(items_payload(item(7, [19, "x", None, True, 122]), item(8)))
        self.assertEqual(got, {7: [19, 122], 8: []})

    def test_taglist_maps_ids_to_names(self):
        with mock.patch.object(exporter, "http_json", return_value={"response": {"tags": [
                {"tagid": 1628, "name": "Metroidvania"}, {"tagid": 29482, "name": " Souls-like "},
                {"tagid": "x", "name": "Bad"}, {"tagid": 5, "name": ""}]}}):
            self.assertEqual(exporter.fetch_taglist(exporter.RateLimiter(0)), {"1628": "Metroidvania", "29482": "Souls-like"})

    def test_an_empty_or_failed_taglist_is_none(self):
        for payload in (None, {"response": {"tags": []}}, {"response": {}}):
            with mock.patch.object(exporter, "http_json", return_value=payload):
                self.assertIsNone(exporter.fetch_taglist(exporter.RateLimiter(0)))

    def test_a_tag_id_without_a_name_is_dropped_not_written_as_a_number(self):
        self.assertEqual(exporter.tag_names({"v": [29482, 99999, 1628]}, {"29482": "Souls-like", "1628": "Metroidvania"}),
                         ["Souls-like", "Metroidvania"])
        self.assertEqual(exporter.tag_names(None, {"1": "x"}), [])


class StoreTags(ExporterCase):
    def cache(self):
        return json.loads((self.dir / "steam_backlog_cache.json").read_text())

    def test_tags_are_off_without_details(self):
        self.ok()
        self.assertEqual((self.mock.count("tags"), self.mock.count("taglist")), (0, 0))
        self.assertTrue(all(g["tags"] == [] for g in self.library()["games"]))

    def test_tags_land_in_the_record_by_name_in_steams_order(self):
        self.mock.tags[2002] = [1628, 3959, 3871]
        self.mock.tags[2003] = []
        p, _ = self.ok("--details")
        self.assertRegex(p.stderr, r"Store tags: fetching 4 games in 1 batches")
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2002]["tags"], ["Metroidvania", "Roguelite", "2D"])
        self.assertEqual(by[2003]["tags"], [])
        self.assertEqual(by[2001]["tags"], ["Souls-like", "Difficult", "RPG"])

    def test_the_batch_fetch_is_one_call_and_the_taglist_is_fetched_once(self):
        self.ok("--details")
        self.assertEqual(self.mock.tag_batches, [[2001, 2002, 2003, 2004]])
        self.assertEqual((self.mock.count("tags"), self.mock.count("taglist")), (1, 1))
        self.assertEqual(self.cache()["taglist"]["all"]["v"], {str(k): v for k, v in DEFAULT_TAG_NAMES.items()})
        self.assertEqual(set(self.cache()["tags"]), {"2001", "2002", "2003", "2004"})
        self.assertEqual(self.cache()["tags"]["2001"]["v"], [29482, 4026, 122], "ids are cached, names resolved at export")

    def test_batches_are_tags_batch_apps_each(self):
        with mock.patch.object(exporter, "TAGS_BATCH", 3):
            todo = [1, 2, 3, 4, 5, 6, 7]
            cache = exporter.Cache(str(self.dir / "c.json"), False)
            calls = []
            with mock.patch.object(exporter, "fetch_taglist", return_value={"19": "Action"}), \
                    mock.patch.object(exporter, "fetch_tags", side_effect=lambda ids, lim: calls.append(ids) or {a: [19] for a in ids}):
                exporter.update_tags(todo, cache, exporter.RateLimiter(0))
        self.assertEqual(calls, [[1, 2, 3], [4, 5, 6], [7]])

    def test_a_second_run_is_all_cache_hits(self):
        self.ok("--details")
        p, _ = self.ok("--details")
        self.assertRegex(p.stderr, r"Store tags: everything cached")
        self.assertEqual((self.mock.count("tags"), self.mock.count("taglist")), (1, 1))

    def test_a_game_added_later_is_the_only_one_fetched(self):
        self.ok("--details", "--limit", "2")
        self.ok("--details")
        self.assertEqual(self.mock.tag_batches, [[2001, 2002], [2003, 2004]])
        self.assertEqual(self.mock.count("taglist"), 1)

    def test_expired_tags_are_fetched_again_but_a_fresh_taglist_is_not(self):
        self.ok("--details")
        cache = self.cache()
        for e in cache["tags"].values():
            e["t"] -= (exporter.TTL_TAGS + 1) * 86400
        (self.dir / "steam_backlog_cache.json").write_text(json.dumps(cache))
        self.ok("--details")
        self.assertEqual(self.mock.count("tags"), 2)
        self.assertEqual(self.mock.count("taglist"), 1)

    def test_a_stale_taglist_is_fetched_again(self):
        self.ok("--details")
        cache = self.cache()
        cache["taglist"]["all"]["t"] -= (exporter.TTL_TAGLIST + 1) * 86400
        (self.dir / "steam_backlog_cache.json").write_text(json.dumps(cache))
        self.mock.taglist = [(200, {"response": {"tags": [{"tagid": 29482, "name": "Soulslike"}]}}, {})]
        self.ok("--details")
        self.assertEqual(self.mock.count("taglist"), 2)
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2001]["tags"], ["Soulslike"], "ids the new list lacks are dropped")

    def test_a_stale_taglist_is_used_when_the_refresh_fails(self):
        self.ok("--details")
        cache = self.cache()
        cache["taglist"]["all"]["t"] -= (exporter.TTL_TAGLIST + 1) * 86400
        (self.dir / "steam_backlog_cache.json").write_text(json.dumps(cache))
        self.mock.taglist = [(404, b"", {})]
        p, _ = self.ok("--details")
        self.assertRegex(p.stderr, r"couldn't fetch the tag list; using the older copy")
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2001]["tags"], ["Souls-like", "Difficult", "RPG"])

    def test_no_taglist_at_all_leaves_tags_empty_but_the_export_finishes(self):
        self.mock.taglist = [(404, b"", {})]
        p, _ = self.ok("--details")
        self.assertRegex(p.stderr, r"tags stay empty this run")
        self.assertTrue(all(g["tags"] == [] for g in self.library()["games"]))
        self.assertEqual(set(self.cache()["tags"]), {"2001", "2002", "2003", "2004"}, "ids are still cached")

    def test_an_id_missing_from_the_taglist_is_dropped(self):
        self.mock.tags[2002] = [1628, 99999, 3871]
        self.ok("--details")
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2002]["tags"], ["Metroidvania", "2D"])

    def test_more_than_tags_max_ids_keep_the_first_ten(self):
        ids = list(DEFAULT_TAG_NAMES)  # 12 real ids
        self.mock.tags[2002] = ids
        self.ok("--details")
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual(by[2002]["tags"], [DEFAULT_TAG_NAMES[i] for i in ids[:10]])

    def test_an_app_absent_from_the_response_is_not_cached_and_is_asked_again(self):
        self.mock.tags_omit = {2003}
        self.ok("--details")
        self.assertNotIn("2003", self.cache()["tags"])
        self.assertEqual({g["appid"]: g for g in self.library()["games"]}[2003]["tags"], [])
        self.mock.tags_omit = set()
        self.ok("--details")
        self.assertEqual(self.mock.tag_batches[-1], [2003])
        self.assertEqual({g["appid"]: g for g in self.library()["games"]}[2003]["tags"], ["Souls-like", "Difficult", "RPG"])

    def test_a_failed_batch_warns_is_not_cached_and_does_not_abort(self):
        """60 games are two batches (50 + 10): the first fails, the second lands, the export finishes."""
        ids = list(range(3000, 3060))
        self.mock.games = [{"appid": a, "name": f"Game {a}", "playtime_forever": 0} for a in ids]
        self.mock.tags_batch = [(404, b"", {}), (200, items_payload(*[item(a, [19]) for a in ids[50:]]), {})]
        p, _ = self.ok("--details")
        self.assertRegex(p.stderr, r"fetching 60 games in 2 batches")
        self.assertRegex(p.stderr, r"warning: tag batch 1/2 failed")
        self.assertEqual(set(self.cache()["tags"]), {str(a) for a in ids[50:]})
        by = {g["appid"]: g for g in self.library()["games"]}
        self.assertEqual((by[3000]["tags"], by[3059]["tags"]), ([], ["Action"]))
        self.assertEqual(self.mock.count("reviews:"), 60, "later steps still run")
        self.mock.tags_batch = []
        self.ok("--details")
        self.assertEqual(self.mock.tag_batches[-1], ids[:50], "only the failed batch is asked again")
        self.assertEqual({g["appid"]: g for g in self.library()["games"]}[3000]["tags"], ["Souls-like", "Difficult", "RPG"])
