"""backlog_query.py against the page: the MCP domain cases, score vectors, filter and statistics parity.

Standard library only. The parity tests lift the page's own JavaScript (clean, passes, selected, the
<stats-core> block) out of backlog.html and run it under node, then compare with the Python port on
tests/fixtures/stats_library.json. A missing node is a skip unless REQUIRE_NODE=1 (CI sets it), when it is a
failure.

    REQUIRE_NODE=1 python3 -m unittest tests.test_backlog_query -v
"""
import json
import os
import shutil
import subprocess
import unittest
from datetime import UTC, datetime
from pathlib import Path

import backlog_query as q
from tests.test_score_vectors import PAGE, VECTORS, extract_function, extract_statement
from tests.test_stats import core_block

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "stats_library.json").read_text(encoding="utf-8"))
RAW = FIXTURE["games"]
GAMES = [q.normalize(g, i) for i, g in enumerate(RAW)]
# Eight fixture games across the four lanes (three Up next, all unplayed), and three notes.
TRIAGE = {910001: "next", 910004: "next", 910008: "next", 910002: "someday", 910009: "someday",
          910005: "skip", 910013: "done", 910003: "done"}
NOTES = {910001: "check the co-op", 910006: "on sale?", 910020: "gift from a friend"}
SNAP = q.Snapshot(GAMES, dict(TRIAGE), dict(NOTES), [910008], "2026-11-01T12:00:00Z")


# ---------------------------------------------------------------- node
RUNNER = r"""
const vm = require("node:vm");
const fs = require("node:fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const sandbox = { INPUT: input };
vm.createContext(sandbox);
process.stdout.write(vm.runInContext(input.code + "\n;JSON.stringify(" + input.expr + ")", sandbox));
"""


def run_node(code, expr, **data):
    if not shutil.which("node"):
        if os.environ.get("REQUIRE_NODE") == "1":
            raise AssertionError("REQUIRE_NODE=1 but node is not installed")
        raise unittest.SkipTest("node is not installed")
    res = subprocess.run(["node", "-e", RUNNER], input=json.dumps({"code": code, "expr": expr, **data}),
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


def extract_block(src, pattern):
    """A multi-line statement ending in a line that is exactly '};'."""
    import re
    m = re.search(pattern, src)
    if not m:
        raise AssertionError(f"not found in backlog.html: {pattern}")
    return src[m.start():src.index("\n};\n", m.start()) + 3]


def page_code():
    return "\n".join([
        extract_statement(PAGE, r"const TRIED_MIN = "),
        extract_statement(PAGE, r"const playState = "),
        extract_statement(PAGE, r"const mainH = "),
        extract_statement(PAGE, r"const TIERS = "),
        extract_statement(PAGE, r"const CTRLS = "),
        extract_statement(PAGE, r"const PLAYS = "),
        extract_statement(PAGE, r"const LANE_F = "),
        extract_statement(PAGE, r"const PROGS = "),
        extract_statement(PAGE, r"const SRCS = "),
        extract_statement(PAGE, r"const VIEWS = "),
        extract_statement(PAGE, r"const SORTS = "),
        extract_statement(PAGE, r"const MC_OPTS = "),
        extract_statement(PAGE, r"const DECK_ORDER = "),
        extract_statement(PAGE, r"const DEFAULT_F = "),
        extract_block(PAGE, r"const numIn = "),
        extract_statement(PAGE, r"const pickIn = "),
        extract_function(PAGE, "mainLen"),
        extract_function(PAGE, "normalize"),
        core_block(),
        extract_function(PAGE, "clean"),
        extract_function(PAGE, "currentTriage"),
        extract_function(PAGE, "lane"),
        extract_statement(PAGE, r"const isClosed = "),
        extract_statement(PAGE, r"const inBacklog = "),
        extract_statement(PAGE, r"const PROG_RULE = "),
        extract_statement(PAGE, r"const inProgress = "),
        extract_statement(PAGE, r"const progMatch = "),
        extract_function(PAGE, "passes"),
        extract_function(PAGE, "selected"),
    ])


def raw(**kw):
    base = {"appid": 1, "name": "X", "playtime": 0, "deckPlaytime": 0, "lastPlayed": 0, "deck": 0, "hltb": None,
            "genres": [], "metacritic": None, "year": None, "controller": None, "proton": None}
    return {**base, **kw}


# ---------------------------------------------------------------- ported from the MCP's test_domain.py
def lib():
    """A small hand-made library standing in for the MCP's 49-game fixture."""
    h = lambda m, e=None, c=None: {"main": m, "extra": e, "complete": c}  # noqa: E731
    rows = [
        raw(appid=620, name="Portal 2", playtime=640, deck=3, hltb=h(8.5, 13.5, 22), genres=["Puzzle", "Action"],
            metacritic=95, year=2011, controller="full", proton={"tier": "platinum", "total": 900}),
        raw(appid=400, name="Portal", playtime=0, deck=3, hltb=h(3, 4, 6), genres=["Puzzle"], metacritic=90,
            year=2007, controller="partial", proton={"tier": "gold", "total": 500}, lastPlayed=1500000000),
        raw(appid=1145360, name="Hades", playtime=1420, deckPlaytime=610, deck=3, hltb=h(22, 48, 95),
            genres=["Action", "Roguelike"], metacritic=93, year=2020, controller="full",
            proton={"tier": "native", "total": 300}, lastPlayed=1700000100),
        raw(appid=413150, name="Stardew Valley", playtime=5200, deck=3, hltb=h(53, 95, 160),
            genres=["Simulation", "RPG"], metacritic=89, year=2016, lastPlayed=1700000200,
            proton={"tier": "gold", "total": 10}),
        raw(appid=1085660, name="Destiny 2", playtime=60, deck=1, hltb=None, genres=["Action"], metacritic=82,
            year=2017, controller="full", proton={"tier": "borked", "total": 80}),
        raw(appid=2280, name="DOOM", playtime=0, deck=2, hltb=h(11, 14, 20), genres=["Action"], metacritic=85,
            year=1993),
        raw(appid=379720, name="DOOM", playtime=0, deck=3, hltb=h(12, 15, 22), genres=["Action"], metacritic=87,
            year=2016),
        raw(appid=753640, name="Unpacking", playtime=30, deck=3, hltb=h(4.5, 5, 6), genres=["Puzzle", "Cozy"],
            metacritic=80, year=2021, controller="full", proton={"tier": "silver", "total": 20}),
        raw(appid=367520, name="Hollow Knight", playtime=0, deck=3, hltb=h(26, 41, 63), genres=["Platformer"],
            metacritic=87, year=2017, controller="partial", proton={"tier": "gold", "total": 400}),
        raw(appid=555, name="Mystery Prototype", playtime=0, deck=0),
        raw(appid=444, name="The Witcher 3: Wild Hunt", playtime=0, deck=2, hltb=h(52, 106, 173), genres=["RPG"],
            metacritic=92, year=2015),
    ]
    return [q.normalize(r, i) for i, r in enumerate(rows)]


def names(res):
    return {g.name for g in res.games}


def by_name(gs, name):
    (g,) = [g for g in gs if g.name == name]
    return g


class PortedDomain(unittest.TestCase):
    def find(self, f, triage=None, notes=None, **kw):
        return q.find(lib(), q.Filters(view="all", **f) if isinstance(f, dict) else f, triage or {}, notes or {}, **{"weights": q.DEFAULT_WEIGHTS, **kw})

    def test_explain_reads_like_the_design_doc(self):
        g = q.normalize(raw(deck=3, hltb={"main": 8, "extra": 13, "complete": 22}, metacritic=86))
        self.assertEqual(q.explain(q.score_breakdown(g, "", weights=q.DEFAULT_WEIGHTS)), "never played +30, Verified +30, 8 h +18, MC 86 +5.5")

    def test_vectors_cover_every_term(self):
        seen = {t.term for v in VECTORS["vectors"] for t in q.score_breakdown(q.normalize(v["game"]), v["lane"], weights=q.DEFAULT_WEIGHTS)}
        self.assertEqual(seen, {"play", "deck", "length", "metacritic", "deck_played", "controller", "next"})

    def test_normalize_matches_the_page(self):
        g = q.normalize(raw(deck=True, playtime=-5, hltb={"main": 0, "extra": 0}, controller="none",
                            genres=[1, "RPG"]))
        self.assertEqual((g.deck, g.playtime, g.hltb, g.controller, g.genres), (0, 0, None, None, ("RPG",)))
        self.assertTrue(g.has_details and g.has_proton)
        g = q.normalize({"appid": 7})
        self.assertEqual((g.name, g.has_proton, g.has_details, g.proton), ("App 7", False, False, None))
        g = q.normalize(raw(proton={"tier": "gold"}, metacritic=float("nan"), year=True))
        self.assertEqual((g.proton, g.metacritic, g.year), (q.Proton("gold", 0), None, None))

    def test_normalize_extras(self):
        g = q.normalize({"appid": 1, "name": "x", "publishers": ["  Devolver Digital ", "", 5, "Finji"] + ["P"] * 12,
                         "developers": "Valve", "reviews": {"total": -3, "pct": "x", "desc": 4}})
        self.assertEqual(len(g.publishers), 10)
        self.assertEqual(g.publishers[:2], ("Devolver Digital", "Finji"))
        self.assertEqual(g.pub_keys, ("devolver digital", "finji", "p"))
        self.assertEqual((g.developers, g.dev_keys), ((), ()))
        self.assertEqual(g.reviews, q.Reviews(0, None, None))
        self.assertIsNone(q.normalize({"appid": 1, "reviews": "lots"}).reviews)

    def test_len_bucket(self):
        for hours, bucket in [(None, "none"), (4.9, "short"), (5, "mid"), (14.9, "mid"), (15, "long"), (40, "epic")]:
            h = {"main": hours, "extra": None, "complete": None} if hours else None
            self.assertEqual(q.len_bucket(q.normalize(raw(hltb=h))), bucket)

    def test_query_matches_name_or_genre(self):
        self.assertEqual(names(self.find({"query": "portal"})), {"Portal", "Portal 2"})
        self.assertIn("Hades", names(self.find({"query": "roguelike"})))

    def test_genres_any_all_and_unknown(self):
        any_ = names(self.find({"genres": ("Puzzle", "Cozy")}))
        all_ = names(self.find({"genres": ("Puzzle", "Cozy"), "genre_match": "all"}))
        self.assertEqual(all_, {"Unpacking"})
        self.assertLess(all_, any_)
        self.assertNotIn("Mystery Prototype", any_)
        self.assertIn("Mystery Prototype", names(self.find({"genres": ("Puzzle",), "genres_unknown": True})))

    def test_length_uses_one_measure_without_fallback(self):
        self.assertEqual(names(self.find({"hours_max": 4.9})), {"Portal", "Unpacking"})
        self.assertIn("Portal", names(self.find({"hours_max": 4.9, "length_measure": "extra"})))
        self.assertEqual(names(self.find({"length_none": True})), {"Destiny 2", "Mystery Prototype"})
        self.assertEqual(names(self.find({"hours_min": 50, "hours_max": 60})), {"Stardew Valley", "The Witcher 3: Wild Hunt"})
        self.assertIn("Mystery Prototype", names(self.find({"hours_max": 4.9, "length_unknown": True})))

    def test_protondb_set_and_minimum(self):
        gold_up = names(self.find({"protondb_min": "gold"}))
        self.assertNotIn("Destiny 2", gold_up)
        self.assertIn("Hades", gold_up)
        self.assertEqual(names(self.find({"protondb": ("borked",)})), {"Destiny 2"})
        self.assertGreater(len(names(self.find({"protondb_min": "gold", "protondb_unknown": True}))), len(gold_up))

    def test_metacritic_year_controller_deck(self):
        self.assertEqual(self.find({"metacritic_min": 90, "metacritic_max": 89}, relax=False).games, [])
        self.assertEqual(names(self.find({"metacritic_min": 90})), {"Portal 2", "Hades", "Portal", "The Witcher 3: Wild Hunt"})
        self.assertEqual(names(self.find({"year_from": 2020})), {"Hades", "Unpacking"})
        self.assertTrue({"Portal 2", "Hades"} <= names(self.find({"controller": ("full",)})))
        none = names(self.find({"controller": ("none",)}))
        self.assertNotIn("Mystery Prototype", none)  # no details = unknown, not "none"
        self.assertIn("DOOM", none)
        self.assertEqual(names(self.find({"deck": (1,)})), {"Destiny 2"})

    def test_play_state_lane_and_notes(self):
        tri = {620: "next", 1145360: "done"}
        self.assertEqual(names(self.find({"lane": ("next",)}, tri)), {"Portal 2"})
        untriaged = names(self.find({"lane": ("none",)}, tri))
        self.assertTrue("Portal 2" not in untriaged and "Hades" not in untriaged and "Portal" in untriaged)
        self.assertEqual(names(self.find({"has_note": True}, notes={400: "x"})), {"Portal"})
        self.assertNotIn("Portal", names(self.find({"has_note": False}, notes={400: "x"})))
        tried = names(self.find({"play_state": ("tried",)}))
        self.assertIn("Destiny 2", tried)
        self.assertNotIn("Hades", tried)

    def test_views_and_sorts(self):
        r = self.find(q.Filters(view="backlog"), {620: "skip"})
        self.assertTrue("Hades" not in names(r) and "Portal 2" not in names(r))
        self.assertEqual({g.name for g in self.find(q.Filters(view="closed"), {620: "skip"}).games}, {"Portal 2"})
        self.assertEqual([g.name for g in self.find({}, sort="name").games][:2], ["Destiny 2", "DOOM"])
        self.assertEqual(q.length_hours(self.find({}, sort="short").games[0]), 3)
        self.assertEqual(self.find({}, sort="most").games[0].name, "Stardew Valley")
        self.assertEqual(self.find({}, sort="recent").games[0].name, "Stardew Valley")
        self.assertEqual(self.find({}, sort="mc").games[0].name, "Portal 2")

    def test_nothing_found_relaxes_weakest_first(self):
        r = self.find({"deck": (1,), "metacritic_min": 90})
        self.assertEqual((r.exact, r.relaxed), (False, ["metacritic"]))
        self.assertEqual(names(r), {"Destiny 2"})
        r = self.find({"query": "hollw knight"})
        self.assertEqual((r.exact, r.fuzzy_query, names(r)), (False, True, {"Hollow Knight"}))
        r = self.find({"query": "zzzzzz"})
        self.assertEqual((r.exact, r.games), (False, []))
        self.assertTrue(self.find({"query": "zzzzzz"}, relax=False).exact)

    def test_relaxation_order(self):
        every = q.Filters(metacritic_min=60, reviews=("mixed",), year_from=2000, protondb=("gold",),
                          controller=("full",), publishers=("a",), developers=("b",), tags=("Metroidvania",),
                          genres=("RPG",),
                          has_note=True, unknown_only=("gen",), hours_min=1, deck=(3,), play_state=("never",),
                          lane=("next",))
        self.assertEqual(every.active(), ["metacritic", "reviews", "year", "protondb", "controller", "publishers",
                                          "developers", "tags", "genres", "has_note", "unknown_only", "length", "deck",
                                          "play_state", "lane"])
        for name in [*every.active(), "query"]:
            self.assertNotIn(name, every.without(name).active())

    def test_picks_rank_like_the_page(self):
        gs = lib()
        top = q.picks(gs, q.Filters(), {}, {}, weights=q.DEFAULT_WEIGHTS)
        scores = [q.score(g, "", weights=q.DEFAULT_WEIGHTS) for g in top]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(q.in_backlog(g, {}) for g in top))
        for a, b in zip(top, top[1:]):
            if q.score(a, "", weights=q.DEFAULT_WEIGHTS) == q.score(b, "", weights=q.DEFAULT_WEIGHTS):
                self.assertLess(a.export_index, b.export_index)

    def test_next_bonus_does_not_force_first(self):
        gs = lib()
        tri = {1085660: "next"}  # Destiny 2: tried, Unsupported on the Deck: 22 - 45 + 4 + 100 - 6.5 (MC 82)
        top = q.picks(gs, q.Filters(), tri, {}, weights=q.DEFAULT_WEIGHTS)
        self.assertEqual(q.score(by_name(gs, "Destiny 2"), "next", weights=q.DEFAULT_WEIGHTS), 22 - 45 + 3.5 + 4 + 100)
        self.assertEqual(top[0].name, "Portal")
        self.assertGreater([g.name for g in top].index("Destiny 2"), 0)
        self.assertNotIn("Destiny 2", {g.name for g in q.picks(gs, q.Filters(), tri, {}, exclude_next=True, weights=q.DEFAULT_WEIGHTS)})

    def test_next_queue(self):
        gs = lib()
        tri = {620: "next", 400: "next", 753640: "next"}
        ids = [g.appid for g in q.next_queue(gs, tri, [753640], weights=q.DEFAULT_WEIGHTS)]
        self.assertEqual(ids[0], 753640)
        self.assertEqual(set(ids), {620, 400, 753640})

    def test_overview(self):
        o = q.overview(lib(), {620: "next"})
        self.assertEqual(o.total, 11)
        self.assertEqual(sum(o.play_states.values()), 11)
        self.assertEqual(sum(sum(row.values()) for row in o.grid.values()), o.backlog)
        self.assertEqual((o.lanes["next"], o.lanes["none"]), (1, 10))
        self.assertTrue(0 < o.coverage["hltb"] < 1)
        self.assertEqual(q.overview([], {}).coverage["hltb"], 0.0)

    def test_resolve(self):
        gs = lib()
        self.assertEqual(q.resolve(620, gs).game, by_name(gs, "Portal 2"))
        self.assertEqual(q.resolve("620", gs).game, by_name(gs, "Portal 2"))
        self.assertEqual(q.resolve("portal", gs).game, by_name(gs, "Portal"))  # exact beats prefix
        self.assertEqual(q.resolve("the witcher 3 wild hunt", gs).game, by_name(gs, "The Witcher 3: Wild Hunt"))
        doom = q.resolve("doom", gs)
        self.assertEqual((doom.game, doom.reason, {g.appid for g in doom.candidates}), (None, "ambiguous", {2280, 379720}))
        near = q.resolve("hollow", gs)
        self.assertTrue(near.game is None and near.reason == "no_match" and near.candidates[0].name == "Hollow Knight")
        self.assertEqual(q.resolve(12345, gs).reason, "unknown_appid")
        self.assertEqual(q.resolve("12345", gs).reason, "unknown_appid")
        self.assertEqual(q.resolve("", gs).reason, "no_match")


class ScoreVectors(unittest.TestCase):
    def test_vectors(self):
        self.assertGreaterEqual(len(VECTORS["vectors"]), 30)
        for v in VECTORS["vectors"]:
            with self.subTest(vector=v["id"], why=v["why"]):
                g = q.normalize(v["game"])
                exp = v["expected"]
                self.assertAlmostEqual(q.score(g, v["lane"], weights=q.DEFAULT_WEIGHTS), exp["score"], delta=1e-9)
                self.assertEqual(q.play_state(g), exp["playState"])
                self.assertEqual(q.length_hours(g), exp["lengthHours"])
                terms = q.score_breakdown(g, v["lane"], weights=q.DEFAULT_WEIGHTS)
                self.assertAlmostEqual(sum(t.points for t in terms), exp["score"], delta=1e-9)
                self.assertTrue(all(t.points != 0 for t in terms if t.term != "play"))


# ---------------------------------------------------------------- filter parity with the page
STATES = [
    {},
    {"view": "next"}, {"view": "someday"}, {"view": "played"}, {"view": "closed"}, {"view": "all"},
    {"view": "all", "q": "rpg"}, {"view": "all", "q": "  Fixture Game 1 "},
    {"view": "all", "deck": [3]}, {"view": "all", "deck": [1, 2]},
    {"view": "all", "gen": ["RPG"]}, {"view": "all", "gen": ["RPG", "Puzzle"], "gm": "all"},
    {"view": "all", "gen": ["Strategy"], "genUnk": True},
    {"view": "all", "dsrc": "main", "dmin": 10, "dmax": 30}, {"view": "all", "dsrc": "extra", "dmin": 5, "dmax": 40},
    {"view": "all", "dsrc": "extra", "dmax": 30, "durUnk": True}, {"view": "all", "dnone": True},
    {"view": "all", "dsrc": "complete", "dnone": True},
    {"view": "all", "pt": ["gold", "silver"]}, {"view": "all", "pt": ["borked"], "ptUnk": True},
    {"view": "all", "mc": 70}, {"view": "all", "mc": 80, "mcmax": 89}, {"view": "all", "mcmax": 59, "mcUnk": True},
    {"view": "all", "ymin": 2010, "ymax": 2019}, {"view": "all", "ymin": 2015, "yrUnk": True},
    {"view": "all", "ctrl": ["partial"]}, {"view": "all", "ctrl": ["none"], "ctrlUnk": True},
    {"view": "all", "pub": ["devolver digital", "finji"]}, {"view": "all", "pub": ["valve"], "pubUnk": True},
    {"view": "all", "dev": ["studio alpha"]}, {"view": "all", "dev": ["studio beta"], "devUnk": True},
    {"view": "all", "tag": ["Metroidvania"]}, {"view": "all", "tag": ["Souls-like", "Roguelike"]},
    {"view": "all", "tag": ["Cozy"], "tagUnk": True}, {"view": "all", "unk": ["tag"]},
    {"view": "all", "tag": ["metroidvania"]}, {"view": "all", "tag": ["Souls like"]}, {"view": "all", "tag": ["Soulsborne"]}, {"view": "all", "tag": ["Metroidvania", "Difficult"], "tagUnk": True, "deck": [3]},
    {"view": "backlog", "tag": ["Souls-like"], "dsrc": "main", "dmax": 30},
    {"view": "all", "tag": [""] + ["Pixel Graphics"] * 2 + ["x" * 81], "gen": ["RPG"]},
    {"view": "all", "rv": ["mixed", "few"]}, {"view": "all", "rv": ["very-positive"], "rvUnk": True},
    {"view": "all", "unk": ["gen"]}, {"view": "all", "unk": ["pub", "rv"]}, {"view": "all", "unk": ["mc", "yr", "ctrl"]},
    {"view": "all", "play": ["never"]}, {"view": "all", "play": ["tried", "played"]},
    {"view": "all", "lane": ["none"]}, {"view": "all", "lane": ["next", "done"]},
    {"view": "all", "notes": "yes"}, {"view": "all", "notes": "no"},
    {"view": "all", "prog": ["deck"]}, {"view": "all", "prog": ["unstarted"]}, {"view": "all", "prog": ["deck", "unstarted"]},
    {"view": "backlog", "prog": ["deck"], "play": ["tried"]}, {"view": "all", "prog": ["deck", "bogus", "deck"]},
    {"view": "all", "prog": ["mostdeck"]}, {"view": "all", "prog": ["mostelse", "deckonly"]}, {"view": "all", "prog": ["nodeck"]},
    {"view": "backlog", "deck": [3], "mc": 60, "play": ["never"], "ymin": 2000},
    # values clean() must drop
    {"view": "all", "mc": 82, "mcmax": 100, "deck": ["7"], "rv": ["great"]},
    {"view": "bogus", "dsrc": "nope", "gm": "some", "dmin": -1, "dmax": 5000, "ymin": 1900, "notes": "maybe",
     "pt": ["gold", "gold", "diamond"], "lane": ["next", "weird"], "unk": ["gen", "len"]},
    {"view": "all", "deck": ["3", 2, "2.0"], "gen": [], "pub": [""] + ["x"] * 3, "q": 5},
]


# ---------------------------------------------------------------- "in progress" (clock pinned on both sides)
NOW = 1_731_000_000  # seconds; the page's Date.now() and backlog_query.clock are both set to it
DAY = 86400


def progress_library():
    """The fixture with lastPlayed spread over ~660 days (so the 365-day edge falls inside it), plus edge cases."""
    rows = json.loads(json.dumps(RAW))
    for i, r in enumerate(rows):
        if r.get("lastPlayed"):
            r["lastPlayed"] = NOW - (i * 17 + 3) * DAY
    h = lambda m, e=None: {"main": m, "extra": e, "complete": None}  # noqa: E731
    edge = [
        raw(appid=1, name="At 80 percent", playtime=480, lastPlayed=NOW - DAY, hltb=h(10)),        # not under 80%: out
        raw(appid=2, name="Just under 80", playtime=479, lastPlayed=NOW - DAY, hltb=h(10)),        # in
        raw(appid=3, name="Exactly a year", playtime=60, lastPlayed=NOW - 365 * DAY, hltb=h(10)),  # in (<= 365 d)
        raw(appid=4, name="A year and a second", playtime=60, lastPlayed=NOW - 365 * DAY - 1, hltb=h(10)),  # out
        raw(appid=5, name="Extra only", playtime=60, lastPlayed=NOW - DAY, hltb=h(None, 7)),        # in (mainH falls back)
        raw(appid=6, name="No HLTB", playtime=600, lastPlayed=NOW - DAY),                           # out: unknown length
        raw(appid=7, name="Never launched", playtime=0, lastPlayed=NOW - DAY, hltb=h(10)),          # out
        raw(appid=8, name="No last played", playtime=60, lastPlayed=0, hltb=h(10)),                 # out
        raw(appid=9, name="Skipped", playtime=60, lastPlayed=NOW - DAY, hltb=h(10)),                # out (lane skip)
        raw(appid=10, name="Finished", playtime=60, lastPlayed=NOW - DAY, hltb=h(10)),              # out (lane done)
        raw(appid=11, name="Deck only", playtime=300, deckPlaytime=300, lastPlayed=NOW - DAY, hltb=h(20)),  # in
    ]
    return rows + edge


PROG_TRIAGE = {**TRIAGE, 9: "skip", 10: "done", 11: "next"}
PROG_STATES = [
    {"view": "all", "prog": ["inprog"]}, {"view": "backlog", "prog": ["inprog"]},
    {"view": "all", "prog": ["inprog", "unstarted"]}, {"view": "all", "prog": ["inprog"], "play": ["played"]},
    {"view": "all", "prog": ["inprog"], "lane": ["none"]}, {"view": "all", "prog": ["deck"], "q": "game"},
]


class InProgress(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, q, "clock", q.clock)
        q.clock = lambda: NOW
        self.raw = progress_library()
        self.games = [q.normalize(r, i) for i, r in enumerate(self.raw)]

    def test_the_edges(self):
        got = {g.name for g in self.games if q.in_progress(g, PROG_TRIAGE)}
        for name in ("Just under 80", "Exactly a year", "Extra only", "Deck only"):
            self.assertIn(name, got)
        for name in ("At 80 percent", "A year and a second", "No HLTB", "Never launched", "No last played",
                     "Skipped", "Finished"):
            self.assertNotIn(name, got)

    def test_page_parity(self):
        run = run_node(
            "Date.now = () => INPUT.now * 1000;\nconst S = {};\n" + page_code(),
            "(() => { Object.assign(S, { f: null, games: INPUT.games.map(normalizeGame).map(annotateNames),"
            " notes: new Set(INPUT.notes), source: 'server', triage: new Map(INPUT.triage) });"
            " return { flags: S.games.map((g) => inProgress(g)),"
            " sel: INPUT.states.map((st) => { S.f = clean(st); return selected().map((g) => g.appid); }) }; })()",
            games=self.raw, states=PROG_STATES + STATES, notes=list(NOTES), now=NOW,
            triage=[[a, v] for a, v in PROG_TRIAGE.items()])
        self.assertEqual(run["flags"], [q.in_progress(g, PROG_TRIAGE) for g in self.games])
        self.assertGreater(sum(run["flags"]), 5)
        self.assertLess(sum(run["flags"]), len(run["flags"]) - 5)
        for state, want in zip(PROG_STATES + STATES, run["sel"]):
            with self.subTest(state=state):
                got = [g.appid for g in q.selected(self.games, q.clean(state), PROG_TRIAGE, NOTES)]
                self.assertEqual(got, want)

    def test_filter_values_sort_and_view_semantics(self):
        f = q.clean({"view": "all", "prog": ["inprog", "bogus", "inprog"]})
        self.assertEqual(f.progress, ("inprog",))
        self.assertIn("progress", f.active())
        self.assertEqual(f.without("progress").progress, ())
        sel = q.selected(self.games, f, PROG_TRIAGE, NOTES)
        self.assertTrue(sel and all(q.in_progress(g, PROG_TRIAGE) for g in sel))
        by_deck = [g.deck_playtime for g in q.sort_games(list(self.games), "deckplay", PROG_TRIAGE, weights=q.DEFAULT_WEIGHTS)]
        self.assertEqual(by_deck, sorted(by_deck, reverse=True))
        # D6: "backlog" still means not played and not closed. A game can be in progress and not in the backlog.
        d = by_name(self.games, "Deck only")
        self.assertTrue(q.in_progress(d, PROG_TRIAGE))
        self.assertEqual(q.play_state(d), "played")
        self.assertFalse(q.in_backlog(d, PROG_TRIAGE))


class FilterVectors(unittest.TestCase):
    def test_states_cover_enough(self):
        self.assertGreaterEqual(len(STATES), 30)
        self.assertGreaterEqual(sum(1 for s in STATES if "tag" in s or "tagUnk" in s or "tag" in s.get("unk", ())), 6)

    def test_page_parity(self):
        node_ids = run_node(
            "const S = {};\n" + page_code(),
            "(() => { Object.assign(S, { f: null, games: INPUT.games.map(normalizeGame).map(annotateNames),"
            " notes: new Set(INPUT.notes), source: 'server', triage: new Map(INPUT.triage) });"
            " return INPUT.states.map((st) => { S.f = clean(st); return selected().map((g) => g.appid); }); })()",
            games=RAW, states=STATES, notes=list(NOTES), triage=[[a, v] for a, v in TRIAGE.items()])
        for state, want in zip(STATES, node_ids):
            with self.subTest(state=state):
                got = [g.appid for g in q.selected(GAMES, q.clean(state), TRIAGE, NOTES)]
                self.assertEqual(got, want)

    def test_states_are_not_all_empty_or_full(self):
        sizes = {len(q.selected(GAMES, q.clean(s), TRIAGE, NOTES)) for s in STATES}
        self.assertGreater(len(sizes), 8)
        self.assertIn(0, sizes | {0})

    def test_tag_key_matches_the_page(self):
        from tests.test_stats import TAG_KEY_CASES, TAG_KEY_WANT
        self.assertEqual([q.tag_key(x) for x in TAG_KEY_CASES], TAG_KEY_WANT)
        self.assertEqual([q.tag_key(x) for x in (None, 5)], ["", "5"])

    def test_tags_are_keyed_loosely_and_blank_is_unknown(self):
        self.assertEqual(q.normalize(raw(tags=[" Souls-like ", "", 5, "Metroidvania"] + ["T"] * 12)).tags[:2],
                         ("Souls-like", "Metroidvania"))
        self.assertEqual(len(q.normalize(raw(tags=[f"T{i}" for i in range(30)])).tags), 10)
        self.assertEqual(q.normalize(raw()).tags, ())
        self.assertEqual(q.normalize(raw(tags="Souls-like")).tags, ())
        f = q.clean({"view": "all", "tag": ["Metroidvania", "", "Metroidvania", "x" * 81], "tagUnk": True})
        self.assertEqual((f.tags, f.tags_unknown), (("metroidvania",), True))
        by = {g.name: g for g in GAMES}
        self.assertTrue(q.passes(by["Fixture Game 03"], q.clean({"unk": ["tag"]}), TRIAGE, NOTES))
        self.assertTrue(q.passes(by["Fixture Game 02"], q.clean({"tag": ["metroidvania"]}), TRIAGE, NOTES))
        self.assertTrue(q.passes(by["Fixture Game 01"], q.clean({"tag": ["Souls like"]}), TRIAGE, NOTES))
        self.assertTrue(q.passes(by["Fixture Game 01"], q.clean({"tag": ["soulsborne"]}), TRIAGE, NOTES))
        self.assertFalse(q.passes(by["Fixture Game 02"], q.clean({"tag": ["Roguelite"]}), TRIAGE, NOTES))
        self.assertTrue(q.passes(by["Fixture Game 02"], q.clean({"tag": ["Metroidvania"]}), TRIAGE, NOTES))
        self.assertTrue(q.passes(by["Fixture Game 03"], q.clean({"tag": ["Metroidvania"], "tagUnk": True}), TRIAGE, NOTES))
        self.assertFalse(q.passes(by["Fixture Game 03"], q.clean({"tag": ["Metroidvania"]}), TRIAGE, NOTES))

    def test_clean_drops_invalid_values(self):
        f = q.clean({"mc": 82, "mcmax": 100, "deck": ["7"], "rv": ["great"], "view": "bogus", "q": "x" * 300})
        self.assertEqual((f.metacritic_min, f.metacritic_max, f.deck, f.reviews, f.view), (None, None, (), (), "backlog"))
        self.assertEqual(len(f.query), 200)
        f = q.clean({"mc": 80, "mcmax": 89, "notes": "no", "unk": ["gen", "pub", "len"], "gen": ["a"] * 3 + ["b" * 61]})
        self.assertEqual((f.metacritic_min, f.metacritic_max, f.has_note, f.unknown_only, f.genres),
                         (80, 89, False, ("gen", "pub"), ("a",)))
        self.assertEqual(q.clean({}), q.Filters())


class WherePlayed(unittest.TestCase):
    """Deck share buckets: the edges, page against Python, and every stats row against the filter that lists it."""
    EDGE = [  # (playtime, deckPlaytime) -> bucket
        (0, 0, "unstarted"), (60, 0, "nodeck"), (100, 100, "deckonly"), (100, 120, "deckonly"), (100, 50, "mostdeck"),
        (101, 50, "mostelse"), (100, 49, "mostelse"), (100, 99, "mostdeck"), (1, 1, "deckonly"),
    ]

    def test_edges_match_the_page(self):
        rows = [raw(appid=i + 1, name=f"E{i}", playtime=p, deckPlaytime=d) for i, (p, d, _) in enumerate(self.EDGE)]
        want = run_node(page_code(), "INPUT.games.map(normalizeGame).map(whereKey)", games=rows)
        self.assertEqual(want, [b for _, _, b in self.EDGE])
        self.assertEqual([q.where_key(q.normalize(r)) for r in rows], want)

    def test_every_row_equals_its_filter(self):
        st = q.stats_for(GAMES)
        self.assertGreaterEqual(len(st["dims"]["where"]["rows"]), 3)
        for r in st["dims"]["where"]["rows"]:
            with self.subTest(row=r["key"]):
                f = q.clean({"view": "all", "prog": [r["key"]]})
                self.assertEqual(len(q.selected(GAMES, f, TRIAGE, NOTES)), r["n"])
        # the tile and the "started on Deck" filter agree, and the three Deck rows add up to it
        started = len(q.selected(GAMES, q.clean({"view": "all", "prog": ["deck"]}), TRIAGE, NOTES))
        self.assertEqual(started, st["tiles"]["deckGames"])
        self.assertEqual(started, sum(r["n"] for r in st["dims"]["where"]["rows"] if r["key"] in ("deckonly", "mostdeck", "mostelse")))


class YearBounds(unittest.TestCase):
    """A release year outside 1950-2100 (the year filter's own range) is bad data: unknown, like no year.

    Without the bound, one corrupt year makes the stats year histogram walk every year up to it."""

    YEARS = [1949, 1950, 2100, 2101, 3_000_000, 1e300, -2024, 2024.5, 0]

    def test_out_of_range_years_are_unknown_like_the_page(self):
        raws = [raw(appid=i + 1, name=f"G{i}", year=y) for i, y in enumerate(self.YEARS)]
        page = run_node(page_code(), "INPUT.games.map(normalize).map((g) => [g.year, g.hasDetails])", games=raws)
        py = [[g.year, g.has_details] for g in (q.normalize(r, i) for i, r in enumerate(raws))]
        self.assertEqual(py, page)
        self.assertEqual([y for y, _ in py], [None, 1950, 2100, None, None, None, None, 2024.5, None])
        # Store details were still fetched: a bad year doesn't make the controller filter call it unknown.
        self.assertTrue(all(h for _, h in py))

    def test_stats_years_stay_bounded(self):
        games = [q.normalize(raw(appid=1, name="A", year=1990), 0),
                 q.normalize(raw(appid=2, name="B", year=3_000_000), 1)]
        dim = q.stats_for(games)["dims"]["year"]
        self.assertEqual([r["key"] for r in dim["rows"]], [1990])
        self.assertEqual(dim["unknown"]["n"], 1)


class StatsParity(unittest.TestCase):
    def test_stats_for_matches_page(self):
        for dsrc in ("main", "extra", "complete"):
            with self.subTest(dsrc=dsrc):
                want = run_node(
                    "\n".join([extract_statement(PAGE, r"const TRIED_MIN = "),
                               extract_statement(PAGE, r"const playState = "),
                               extract_statement(PAGE, r"const TIERS = "),
                               extract_statement(PAGE, r"const DECK_ORDER = "),
                               extract_function(PAGE, "mainLen"), extract_function(PAGE, "normalize"),
                               core_block()]),
                    "(() => { const t = new Map(INPUT.triage);"
                    " return statsFor(INPUT.games.map(normalizeGame).map(annotateNames),"
                    " { dsrc: INPUT.dsrc, laneOf: (g) => t.get(g.appid) || '' }); })()",
                    games=RAW, dsrc=dsrc, triage=[[a, v] for a, v in TRIAGE.items()])
                got = q.stats_for(GAMES, dsrc=dsrc, lane_of=lambda g: TRIAGE.get(g.appid, ""))
                self.assertEqual(json.loads(json.dumps(got)), want)

    def test_name_key_unicode(self):
        sample = ["Devolver Digital Inc.", "SQUARE ENIX CO., LTD.", "Ｓｑｕａｒｅ Ｅｎｉｘ", "Ubisoft™", "Bandai Namco®",
                  "Ünïcödé Games GmbH", " - ", "", "Café Studios, Inc", "Co"]
        want = run_node(core_block(), "INPUT.names.map(nameKey)", names=sample)
        self.assertEqual([q.name_key(n) for n in sample], want)

    def test_row_helpers_match_page(self):
        rows = [{"key": y, "label": str(y), "n": n, "minutes": 60 * n + y % 7, "never": 1, "tried": 0, "played": n - 1}
                for y, n in ((1999, 2), (2009, 2), (2011, 3), (2019, 4), (2021, 5))]
        js = ("({ yb: yearBuckets(INPUT.rows, 10), same: yearBuckets(INPUT.rows, 1),"
              " rk: ['games','hours','never'].map((m) => rankRows(INPUT.rows, m)),"
              " fold: foldRows(INPUT.rows, 3), whole: foldRows(INPUT.rows, 5) })")
        want = run_node(core_block(), js, rows=rows)
        self.assertEqual(q.year_buckets(rows, 10), want["yb"])
        self.assertEqual(q.year_buckets(rows, 1), want["same"])
        self.assertEqual([q.rank_rows(rows, m) for m in ("games", "hours", "never")], want["rk"])
        self.assertEqual(q.fold_rows(rows, 3), (want["fold"]["rows"], want["fold"]["other"]))
        self.assertEqual(q.fold_rows(rows, 5), (want["whole"]["rows"], None))


# ---------------------------------------------------------------- tool-facing functions
def all_keys(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from all_keys(v)
    elif isinstance(x, list):
        for v in x:
            yield from all_keys(v)


class ToolOutputs(unittest.TestCase):
    def test_bad_metacritic_names_the_allowed_values(self):
        with self.assertRaises(q.QueryError) as cm:
            q.tool_filters(metacritic_min=82)
        msg = str(cm.exception)
        self.assertIn("metacritic_min", msg)
        self.assertIn("60, 70, 75, 80, 85, 90", msg)

    def test_tool_filters_maps_names_and_enums(self):
        f = q.tool_filters(deck=["verified", "playable"], publishers=["Devolver Digital Inc."], length="short",
                           include_unknown=["length", "genres"], unknown_only=["year", "controller"],
                           has_note=False, metacritic_max=89)
        self.assertEqual((f.deck, f.publishers, f.hours_max, f.length_unknown, f.genres_unknown, f.unknown_only),
                         ((3, 2), ("devolver digital",), 4.9, True, True, ("yr", "ctrl")))
        self.assertEqual((f.has_note, f.metacritic_max), (False, 89))
        self.assertTrue(q.tool_filters(length="none").length_none)
        # An explicit range beats the preset.
        f = q.tool_filters(length="short", hours_min=20)
        self.assertEqual((f.hours_min, f.hours_max), (20, None))
        for bad in ({"unknown_only": ["length"]}, {"deck": ["great"]}, {"year_from": 1900}, {"lane": ["later"]},
                    {"protondb_min": "pending"}, {"query": "x" * 201}, {"genre_match": "some"}, {"has_note": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(q.QueryError):
                q.tool_filters(**bad)

    def test_tool_filters_tags_are_exact_names(self):
        f = q.tool_filters(tags=["Souls-like", "soulslike", "", "Metroidvania"], include_unknown=["tags"])
        self.assertEqual((f.tags, f.tags_unknown), (("soulslike", "metroidvania"), True))
        self.assertEqual(q.tool_filters(tags="Roguelike").tags, ("roguelike",), "one name is a list of one")
        self.assertEqual(q.tool_filters(unknown_only=["tags"]).unknown_only, ("tag",))
        for spelling in ("Soulslike", "souls-like", "souls like", "SOULSLIKE", "Soulsborne"):
            self.assertEqual(q.tool_filters(tags=[spelling]).tags, ("soulslike",), spelling)
        for bad in ({"tags": ["x"] * 11}, {"tags": ["x" * 81]}, {"tags": [5]}, {"tags": {"a": 1}}):
            with self.subTest(bad=bad), self.assertRaises(q.QueryError):
                q.tool_filters(**bad)

    def test_find_games_by_tag_matches_the_page_filter(self):
        for state, kw in (({"view": "all", "tag": ["Metroidvania"]}, {"tags": ["Metroidvania"]}),
                          ({"view": "all", "tag": ["Souls-like", "Roguelike"]}, {"tags": ["Souls-like", "Roguelike"]}),
                          ({"view": "all", "tag": ["Souls like"]}, {"tags": ["Soulsborne"]}),
                          ({"view": "all", "unk": ["tag"]}, {"unknown_only": ["tags"]}),
                          ({"view": "all", "tag": ["Cozy"], "tagUnk": True}, {"tags": ["Cozy"], "include_unknown": ["tags"]})):
            with self.subTest(state=state):
                want = [g.appid for g in q.selected(GAMES, q.clean(state), TRIAGE, NOTES)]
                out = q.find_out(SNAP, q.tool_filters(**kw), view="all", limit=25, sort="name")
                self.assertTrue(out["exact"])
                self.assertEqual(out["matches"], len(want))
                self.assertEqual(sorted(g["appid"] for g in out["games"]), sorted(want))
        self.assertEqual({g["appid"] for g in q.find_out(SNAP, q.tool_filters(tags=["Metroidvania"]), view="all",
                                                         limit=25)["games"]},
                         {g.appid for g in GAMES if "Metroidvania" in g.tags})

    def test_game_out_lists_tags_in_steams_order(self):
        by = {g.name: g for g in GAMES}
        self.assertEqual(q.game_out(SNAP, game="910001")["game"]["tags"], list(by["Fixture Game 02"].tags))
        self.assertEqual(q.game_out(SNAP, game="910002")["game"]["tags"], [])

    def test_find_out_truncates(self):
        out = q.find_out(SNAP, q.tool_filters(), limit=2)
        self.assertEqual(out["matches"], len(GAMES))
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["games"]), 2)
        out = q.find_out(SNAP, q.tool_filters(), limit=25, offset=30)
        self.assertEqual((len(out["games"]), out["truncated"]), (10, False))
        self.assertEqual(set(out["games"][0]), {"appid", "name", "year", "deck", "play_state", "playtime_hours",
                                                 "length_hours", "metacritic", "protondb", "lane", "has_note", "score"})

    def test_zero_match_relaxes_metacritic_before_genres(self):
        def run(**kw):
            f = q.tool_filters(**kw)
            self.assertEqual(q.selected(GAMES, q.replace(f, view="all"), TRIAGE, NOTES), [])  # strict: empty
            return q.find_out(SNAP, f)

        out = run(metacritic_min=90, genres=["Strategy"], deck=["unsupported"])
        self.assertEqual((out["exact"], out["relaxed"]), (False, ["metacritic"]))  # genres kept
        self.assertTrue(out["games"])
        self.assertIn("metacritic", out["detail"])
        out = run(metacritic_min=90, genres=["Simulation"], lane=["next"])
        self.assertEqual((out["exact"], out["relaxed"]), (False, ["metacritic", "genres"]))  # lane kept
        self.assertTrue(all(g["lane"] == "next" for g in out["games"]))

    def test_game_out_found_and_note(self):
        out = q.game_out(SNAP, game="910001")
        self.assertTrue(out["found"])
        self.assertEqual(out["game"]["note"], NOTES[910001])
        queue = [e["appid"] for e in q.next_queue_out(SNAP)["queue"]]
        self.assertEqual(queue[0], 910008)  # saved first
        self.assertEqual(out["game"]["next_position"], queue.index(910001) + 1)
        self.assertTrue(out["game"]["in_backlog"])
        self.assertIn("in Up next +100", out["game"]["score_explained"])
        self.assertEqual(out["game"]["store_url"], "https://store.steampowered.com/app/910001/")
        self.assertIsNone(q.game_out(SNAP, game="910007")["game"]["note"])

    def test_game_out_ambiguous_and_unknown(self):
        games = GAMES + [q.normalize(raw(appid=1, name="Twin")), q.normalize(raw(appid=2, name="TWIN"))]
        out = q.game_out(q.Snapshot(games), game="twin")
        self.assertFalse(out["found"])
        self.assertEqual(out["unresolved"]["reason"], "ambiguous")
        self.assertEqual({c["appid"] for c in out["unresolved"]["candidates"]}, {1, 2})
        self.assertEqual(q.game_out(SNAP, game="777")["unresolved"]["reason"], "unknown_appid")

    def test_picks_out_lists_up_next_with_bonus(self):
        out = q.picks_out(SNAP, q.tool_filters(), count=12)
        self.assertEqual(len(out["picks"]), 12)
        self.assertEqual([p["rank"] for p in out["picks"]], list(range(1, 13)))
        nexts = [p for p in out["picks"] if p["lane"] == "next"]
        self.assertTrue(nexts)
        for p in nexts:
            self.assertIn("+100", p["score_explained"])
            self.assertIn("next", [t["term"] for t in p["score_breakdown"]])
        self.assertEqual(next(p for p in out["picks"] if p["appid"] == 910001)["note"], NOTES[910001])
        self.assertNotIn(910001, [p["appid"] for p in q.picks_out(SNAP, q.tool_filters(), exclude_next=True, count=12)["picks"]])

    def test_next_queue_out(self):
        out = q.next_queue_out(SNAP)
        self.assertEqual([e["appid"] for e in out["queue"]][0], 910008)
        self.assertEqual([e["position"] for e in out["queue"]], [1, 2, 3])

    def test_overview_out(self):
        out = q.overview_out(SNAP, now=datetime(2026, 11, 3, 12, tzinfo=UTC))
        self.assertEqual(out["games"], 40)
        self.assertEqual(out["next_queue"], 3)
        self.assertEqual(out["export"], {"exported_at": "2026-11-01T12:00:00Z", "age_days": 2.0})

    def test_stats_out_publisher_folds_to_25(self):
        many = [q.normalize(raw(appid=i, name=f"G{i}", publishers=[f"Publisher {chr(97 + i % 26)}{i}"]), i)
                for i in range(60)]
        out = q.stats_out(q.Snapshot(many), q.tool_filters(), dimension="publisher")
        self.assertEqual(len(out["rows"]), 25)
        self.assertEqual(out["other"]["games"], 35)
        self.assertEqual(out["other"]["values"], 35)
        out = q.stats_out(SNAP, q.tool_filters(), dimension="publisher")
        self.assertLessEqual(len(out["rows"]), 25)
        self.assertEqual(out["rows"][0]["label"], "Devolver Digital")
        self.assertEqual(set(out["rows"][0]), {"label", "games", "hours_played", "never", "tried", "played"})
        self.assertIsNotNone(out["unknown"])

    def test_stats_out_tags_rank_fold_and_unknown(self):
        out = q.stats_out(SNAP, q.tool_filters(), dimension="tags")
        none = sum(1 for g in GAMES if not g.tags)
        self.assertEqual(out["dimension"], "tags")
        self.assertEqual((out["known"], out["unknown"]["games"]), (40 - none, none))
        self.assertEqual(set(out["rows"][0]), {"label", "games", "hours_played", "never", "tried", "played"})
        self.assertEqual(out["rows"][0]["games"], max(r["games"] for r in out["rows"]))
        self.assertIn("Metroidvania", {r["label"] for r in out["rows"]})
        many = [q.normalize(raw(appid=i, name=f"G{i}", tags=[f"Tag {i}"]), i) for i in range(60)]
        out = q.stats_out(q.Snapshot(many), q.tool_filters(), dimension="tags")
        self.assertEqual((len(out["rows"]), out["other"]["games"], out["other"]["values"]), (25, 35, 35))
        filtered = q.stats_out(SNAP, q.tool_filters(tags=["Metroidvania"]), dimension="tags")
        self.assertEqual(filtered["total"], sum(1 for g in GAMES if "Metroidvania" in g.tags))

    def test_stats_out_decade_and_others(self):
        out = q.stats_out(SNAP, q.tool_filters(), dimension="decade")
        self.assertTrue(out["rows"] and all(r["label"].endswith("s") for r in out["rows"]))
        self.assertEqual(sum(r["games"] for r in out["rows"]) + out["unknown"]["games"], 40)
        for dim in ("genre", "developer", "deck", "protondb", "controller", "length", "lane", "metacritic",
                    "reviews", "year"):
            with self.subTest(dim=dim):
                o = q.stats_out(SNAP, q.tool_filters(), dimension=dim, metric="hours", view="backlog")
                self.assertEqual(o["dimension"], dim)
                self.assertLessEqual(len(o["rows"]), 25)
        with self.assertRaises(q.QueryError):
            q.stats_out(SNAP, q.tool_filters(), dimension="owner")
        hours = q.stats_out(SNAP, q.tool_filters(), dimension="genre", metric="hours")["rows"]
        self.assertEqual([r["hours_played"] for r in hours], sorted((r["hours_played"] for r in hours), reverse=True))

    def test_no_identity_keys_in_any_output(self):
        outs = [q.overview_out(SNAP), q.find_out(SNAP, q.tool_filters()), q.game_out(SNAP, game="910001"),
                q.picks_out(SNAP, q.tool_filters()), q.next_queue_out(SNAP),
                q.stats_out(SNAP, q.tool_filters(), dimension="genre")]
        for out in outs:
            text = json.dumps(out).lower()
            self.assertNotIn("steamid", text)
            self.assertNotIn("persona", text)
            self.assertFalse({k for k in all_keys(out) if "steamid" in k.lower() or "persona" in k.lower()})
            json.dumps(out, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
