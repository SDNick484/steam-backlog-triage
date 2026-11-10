"""The page's grouping and statistics helpers (the <stats-core> block of backlog.html), run under node.

Standard library only. The block is pure (no DOM, no page state), so it is lifted out of the HTML and run in a
node vm with the few page-level definitions it relies on. Like the score parity test, a missing node is a skip
unless REQUIRE_NODE=1 (CI sets it), when it is a failure.

    python3 -m unittest tests.test_stats
"""
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.test_score_vectors import extract_function, extract_statement

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "backlog.html").read_text(encoding="utf-8")
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "stats_library.json").read_text(encoding="utf-8"))


def core_block():
    m = re.search(r"// <stats-core>\n(.*?)// </stats-core>", PAGE, re.S)
    if not m:
        raise AssertionError("no <stats-core> block in backlog.html")
    return m.group(1)


def page_code():
    """The block plus the page-level definitions it uses."""
    return "\n".join([
        extract_statement(PAGE, r"const TRIED_MIN = "),
        extract_statement(PAGE, r"const playState = "),
        extract_statement(PAGE, r"const TIERS = "),
        extract_statement(PAGE, r"const DECK_ORDER = "),
        extract_function(PAGE, "mainLen"),
        extract_function(PAGE, "normalize"),
        core_block(),
    ])


RUNNER = r"""
const vm = require("node:vm");
const fs = require("node:fs");
const { code, expr, data } = JSON.parse(fs.readFileSync(0, "utf8"));
const sandbox = { DATA: data };
vm.createContext(sandbox);
process.stdout.write(vm.runInContext(code + "\n;JSON.stringify(" + expr + ")", sandbox));
"""


def node(expr, data=None):
    """Evaluate a JS expression after the page code (DATA is `data`) and return its JSON value."""
    if not shutil.which("node"):
        if os.environ.get("REQUIRE_NODE") == "1":
            raise AssertionError("REQUIRE_NODE=1 but node is not installed")
        raise unittest.SkipTest("node is not installed")
    res = subprocess.run(["node", "-e", RUNNER], input=json.dumps({"code": page_code(), "expr": expr, "data": data}),
                         capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


def fixture_games():
    return node("DATA.map(normalizeGame).map(annotateNames)", FIXTURE["games"])


class NameKey(unittest.TestCase):
    def keys(self, names):
        return node("DATA.map(nameKey)", names)

    def test_case_and_corporate_suffixes_are_ignored(self):
        got = self.keys(["Devolver Digital", "Devolver Digital Inc.", "DEVOLVER DIGITAL", "devolver digital, inc",
                         "Devolver Digital Inc", "  Devolver Digital  "])
        self.assertEqual(set(got), {"devolver digital"})

    def test_each_listed_suffix(self):
        names = [f"Acme Games {s}" for s in ("Inc", "Inc.", "LLC", "Ltd", "Ltd.", "Co.", "Corp", "Corporation", "GmbH",
                                              "S.A.", "AB", "Limited")]
        self.assertEqual(set(self.keys(names)), {"acme games"})

    def test_suffixes_are_removed_repeatedly(self):
        self.assertEqual(self.keys(["Square Enix Co., Ltd.", "Square Enix", "Square Enix Holdings Co Ltd"]),
                         ["square enix", "square enix", "square enix holdings"])
        self.assertEqual(self.keys(["Foo Corp. Inc."]), ["foo"])

    def test_trademark_symbols_and_end_punctuation(self):
        self.assertEqual(self.keys(["Valve™", "Valve®", "(Valve)", "Valve.", "-Valve-"]), ["valve"] * 5)

    def test_only_trailing_suffixes_and_never_the_whole_name(self):
        self.assertEqual(self.keys(["Inc Games", "Ltd", "Inc.", "Limited Run Games"]),
                         ["inc games", "ltd", "inc", "limited run games"])

    def test_different_companies_stay_different(self):
        got = self.keys(["Team17 Digital Limited", "Team17", "Paradox Interactive AB", "Paradox Interactive"])
        self.assertEqual(got, ["team17 digital", "team17", "paradox interactive", "paradox interactive"])

    def test_blank_names_have_no_key(self):
        self.assertEqual(self.keys(["", "   ", "™", "..."]), ["", "", "", ""])

    def test_accents_and_non_latin_names_survive(self):
        self.assertEqual(self.keys(["Ubisoft Montréal", "ＦｒｏｍSoftware Inc."]), ["ubisoft montréal", "fromsoftware"])


class Groups(unittest.TestCase):
    def test_annotate_names_dedupes_and_drops_blanks(self):
        g = node("annotateNames(normalizeGame({appid:1,name:'x',publishers:['Devolver Digital','DEVOLVER DIGITAL Inc.',' ','Finji'],"
                 "developers:['Studio A Ltd.','Studio A']}))")
        self.assertEqual(g["pubKeys"], ["devolver digital", "finji"])
        self.assertEqual(g["devKeys"], ["studio a"])

    def test_an_old_export_without_the_fields_is_all_unknown(self):
        g = node("annotateNames(normalizeGame({appid:1,name:'x',playtime:0}))")
        self.assertEqual((g["publishers"], g["developers"], g["pubKeys"], g["devKeys"], g["reviews"]), ([], [], [], [], None))

    def test_malformed_fields_become_no_data(self):
        g = node("annotateNames(normalizeGame({appid:1,name:'x',publishers:'Valve',developers:[1,null,{}],reviews:'lots'}))")
        self.assertEqual((g["publishers"], g["developers"], g["reviews"]), ([], [], None))

    def test_label_is_the_most_common_spelling_and_games_count_once(self):
        games = [{"publishers": ["Devolver Digital"]}, {"publishers": ["Devolver Digital Inc."]},
                 {"publishers": ["Devolver Digital"]}, {"publishers": ["DEVOLVER DIGITAL", "Devolver Digital Inc."]},
                 {"publishers": ["Finji"]}]
        got = node("(() => { const gs = DATA.map(annotateNames); return [...nameGroups(gs, 'pub').values()]; })()", games)
        by = {e["key"]: e for e in got}
        self.assertEqual(by["devolver digital"]["label"], "Devolver Digital")
        self.assertEqual(by["devolver digital"]["n"], 4, "a game listing two spellings still counts once")
        self.assertEqual(by["finji"]["n"], 1)

    def test_label_ties_go_to_the_alphabetically_first_spelling(self):
        got = node("(() => [...nameGroups(DATA.map(annotateNames), 'dev').values()])()",
                   [{"developers": ["Zed Ltd"]}, {"developers": ["Zed"]}])
        self.assertEqual(got[0]["label"], "Zed")


class Buckets(unittest.TestCase):
    def test_review_buckets(self):
        def rb(r):
            return node("reviewBucket(DATA)", {"reviews": r})
        self.assertEqual(rb({"total": 900, "pct": 97, "desc": "Overwhelmingly Positive"}), "overwhelmingly-positive")
        self.assertEqual(rb({"total": 900, "pct": 50, "desc": "Mixed"}), "mixed")
        self.assertEqual(rb({"total": 7, "pct": 71, "desc": "7 user reviews"}), "few")
        self.assertEqual(rb({"total": 0, "pct": None, "desc": "No user reviews"}), "few")
        self.assertEqual(rb({"total": 20, "pct": None, "desc": None}), "few")
        self.assertIsNone(rb(None))

    def test_metacritic_buckets_are_exactly_the_filter_ranges(self):
        got = node("[0, 1, 59, 60, 69, 70, 79, 80, 89, 90, 100].map((m) => mcBucket({metacritic: m}))")
        self.assertEqual(got, [None, "lt60", "lt60", "60s", "60s", "70s", "70s", "80s", "80s", "90p", "90p"])
        # Each bucket is the filter mc = min, mcmax = max.
        ranges = node("MC_BUCKETS")
        self.assertEqual(ranges, [["lt60", "Under 60", None, 59], ["60s", "60s", 60, 69], ["70s", "70s", 70, 79],
                                  ["80s", "80s", 80, 89], ["90p", "90+", 90, None]])
        self.assertEqual(node("MC_MAX_OPTS"), [59, 69, 79, 89])

    def test_unknown_per_dimension(self):
        g = {"genres": [], "pubKeys": [], "devKeys": [], "proton": None, "metacritic": None, "year": None,
             "controller": None, "hasDetails": False, "reviews": None, "tags": [], "tagKeys": []}
        got = node("UNK_KEYS.map((k) => isUnknown(DATA, k))", g)
        self.assertEqual(got, [True] * 9)
        known = {**g, "genres": ["RPG"], "pubKeys": ["a"], "devKeys": ["b"], "proton": {"tier": "gold", "total": 1},
                 "metacritic": 80, "year": 2020, "controller": "full", "reviews": {"total": 1, "pct": 100, "desc": "x"},
                 "tags": ["Metroidvania"], "tagKeys": ["metroidvania"]}
        self.assertEqual(node("UNK_KEYS.map((k) => isUnknown(DATA, k))", known), [False] * 9)
        # "None listed" controller support is a known value once the export had store details.
        self.assertFalse(node("isUnknown(DATA, 'ctrl')", {**g, "hasDetails": True}))


TAG_KEY_CASES = ["Soulslike", "Souls-like", "souls like", "SOULS-LIKE", "Soulsborne", "soulsbourne", "Metroidvania",
                 "Roguelike", "Rogue-like", "Roguelite", "2.5D", "Co-op", "co op", "  Pixel   Graphics ", "Sci-fi",
                 "Ｓｏｕｌｓ‐ｌｉｋｅ", "Café", "", "+", "constructor", "__proto__"]
TAG_KEY_WANT = ["soulslike", "soulslike", "soulslike", "soulslike", "soulslike", "soulslike", "metroidvania",
                "roguelike", "roguelike", "roguelite", "25d", "coop", "coop", "pixelgraphics", "scifi",
                "soulslike", "café", "", "", "constructor", "proto"]


class TagKeys(unittest.TestCase):
    def test_spellings_share_a_key_and_aliases_map_onto_it(self):
        self.assertEqual(node("DATA.map(tagKey)", TAG_KEY_CASES), TAG_KEY_WANT)
        self.assertEqual(node("[null, undefined, 5].map(tagKey)"), ["", "", "5"])

    def test_distinct_tags_stay_distinct(self):
        self.assertNotEqual(node("tagKey('Roguelike')"), node("tagKey('Roguelite')"))
        self.assertNotEqual(node("tagKey('Action RPG')"), node("tagKey('RPG')"))

    def test_annotate_names_adds_deduplicated_tag_keys(self):
        g = node("annotateNames({tags: ['Souls-like', 'Soulslike', '+', 'Metroidvania']})")
        self.assertEqual(g["tagKeys"], ["soulslike", "metroidvania"])
        self.assertEqual(node("annotateNames({}).tagKeys"), [])
        self.assertEqual(node("annotateNames({tags: 'x'}).tags"), [])

    def test_groups_count_a_game_once_and_label_with_the_commonest_spelling(self):
        games = [{"tags": ["Souls-like", "Soulslike"]}, {"tags": ["Souls-like"]}, {"tags": ["Soulslike"]},
                 {"tags": ["Souls-like", "Metroidvania"]}, {"tags": []}]
        groups = node("[...nameGroups(DATA.map(annotateNames), 'tag').values()]", games)
        by = {e["key"]: e for e in groups}
        self.assertEqual(by["soulslike"], {"key": "soulslike", "label": "Souls-like", "n": 4})
        self.assertEqual(by["metroidvania"]["n"], 1)


# ---------------------------------------------------------------- statsFor, checked against the fixture
SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "corporation", "gmbh", "sa", "ab", "limited"}


def pkey(name):
    """A plain Python version of nameKey for the fixture's ASCII names."""
    toks = re.sub(r"^\W+|\W+$", "", name.lower().strip()).split()
    while len(toks) > 1 and re.sub(r"\W", "", toks[-1]) in SUFFIXES:
        toks.pop()
        toks[-1] = re.sub(r"^\W+|\W+$", "", toks[-1])
    return " ".join(toks)


def tkey(name):
    """A plain Python version of tagKey for the fixture's ASCII tag names."""
    k = re.sub(r"[^a-z0-9]+", "", name.lower())
    return {"soulsborne": "soulslike", "soulsbourne": "soulslike"}.get(k, k)


def pstate(g):
    return "never" if g["playtime"] == 0 else "tried" if g["playtime"] < 120 else "played"


STATS_JS = "statsFor(DATA.map(normalizeGame).map(annotateNames), {dsrc: 'main', laneOf: () => ''})"


class StatsFor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.games = FIXTURE["games"]
        cls.stats = node(STATS_JS, cls.games)

    def rows(self, d):
        return {r["key"]: r for r in self.stats["dims"][d]["rows"]}

    def test_tiles_match_a_plain_count(self):
        t, g = self.stats["tiles"], self.games
        self.assertEqual(t["games"], 40)
        self.assertEqual(t["minutes"], sum(x["playtime"] for x in g))
        self.assertEqual(t["deckMinutes"], sum(x.get("deckPlaytime", 0) for x in g))
        self.assertEqual(t["deckGames"], sum(1 for x in g if x.get("deckPlaytime", 0) > 0))
        for st in ("never", "tried", "played"):
            self.assertEqual(t[st], sum(1 for x in g if pstate(x) == st), st)
        self.assertEqual(t["never"] + t["tried"] + t["played"], 40)
        mcs = sorted(x["metacritic"] for x in g if x.get("metacritic"))
        mid = len(mcs) // 2
        self.assertEqual(t["mcCount"], len(mcs))
        self.assertAlmostEqual(t["mcMedian"], mcs[mid] if len(mcs) % 2 else (mcs[mid - 1] + mcs[mid]) / 2)

    def test_genre_publisher_developer_and_tag_rows(self):
        for d, field, key in (("gen", "genres", lambda x: x), ("pub", "publishers", pkey), ("dev", "developers", pkey),
                              ("tag", "tags", tkey)):
            want, unknown = {}, 0
            for g in self.games:
                ks = {key(v) for v in g.get(field, []) if key(v)}
                if not ks:
                    unknown += 1
                for k in ks:
                    e = want.setdefault(k, {"n": 0, "minutes": 0, "never": 0})
                    e["n"] += 1
                    e["minutes"] += g["playtime"]
                    e["never"] += pstate(g) == "never"
            got = self.rows(d)
            self.assertEqual(set(got), set(want), d)
            for k, e in want.items():
                self.assertEqual({x: got[k][x] for x in e}, e, f"{d} {k}")
            dim = self.stats["dims"][d]
            self.assertEqual(dim["unknown"]["n"] if dim["unknown"] else 0, unknown, d)
            self.assertEqual(dim["known"] + unknown, 40, d)

    def test_tags_are_keyed_ranked_by_games_with_a_muted_unknown_row(self):
        dim = self.stats["dims"]["tag"]
        none = sum(1 for g in self.games if not g.get("tags"))
        self.assertGreater(none, 0)
        self.assertEqual(dim["unknown"]["n"], none)
        self.assertEqual(dim["known"], 40 - none)
        by = {r["key"]: r for r in dim["rows"]}
        self.assertIn("soulslike", by, "rows are keyed by the normalized tag, labelled with Steam's spelling")
        self.assertEqual(by["soulslike"]["label"], "Souls-like")
        self.assertEqual(by["metroidvania"]["label"], "Metroidvania")
        counts = [r["n"] for r in dim["rows"]]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertGreater(by["metroidvania"]["n"], 1, "some games share a tag")

    def test_every_dimension_rows_plus_unknown_cover_the_games(self):
        for d, dim in self.stats["dims"].items():
            self.assertEqual(dim["total"], 40)
            self.assertEqual(dim["known"] + (dim["unknown"]["n"] if dim["unknown"] else 0), 40, d)
            if d not in ("gen", "pub", "dev", "tag"):   # the multi-value dimensions count a game under each value
                self.assertEqual(sum(r["n"] for r in dim["rows"]), dim["known"], d)
            for r in dim["rows"]:
                self.assertEqual(r["never"] + r["tried"] + r["played"], r["n"], f"{d} {r['key']}")

    def test_single_value_dimensions(self):
        g = self.games
        for k, r in self.rows("deck").items():
            self.assertEqual(r["n"], sum(1 for x in g if x.get("deck") == int(k)), k)
        for k, r in self.rows("pt").items():
            self.assertEqual(r["n"], sum(1 for x in g if (x.get("proton") or {}).get("tier") == k), k)
        for k, r in self.rows("year").items():
            self.assertEqual(r["n"], sum(1 for x in g if x.get("year") == int(k)), k)

    def test_pinned_literals(self):
        # Worked out once from tests/fixtures/stats_library.json; a change here is a change in meaning.
        t, dims = self.stats["tiles"], self.stats["dims"]
        self.assertEqual(t["deckGames"], 6)
        self.assertEqual([(r["key"], r["n"]) for r in dims["where"]["rows"]],
                         [("mostdeck", 2), ("mostelse", 4), ("nodeck", 24), ("unstarted", 10)])
        self.assertIsNone(dims["where"]["unknown"])
        self.assertEqual({k: t[k] for k in ("minutes", "deckMinutes", "never", "tried", "played", "backlogGames",
                                            "backlogWithTime", "backlogHours", "mcMedian", "mcCount", "rvMedian", "rvCount")},
                         {"minutes": 9460, "deckMinutes": 1979, "never": 10, "tried": 17, "played": 13,
                          "backlogGames": 27, "backlogWithTime": 12, "backlogHours": 391, "mcMedian": 69, "mcCount": 32,
                          "rvMedian": 67, "rvCount": 26})
        rows = lambda d: [(r["key"], r["n"]) for r in dims[d]["rows"]]
        unk = lambda d: dims[d]["unknown"]["n"] if dims[d]["unknown"] else None
        self.assertEqual((rows("deck"), unk("deck")), ([(3, 12), (2, 9), (1, 8)], 11))
        self.assertEqual((rows("mc"), unk("mc")), ([("lt60", 7), ("60s", 11), ("70s", 7), ("80s", 3), ("90p", 4)], 8))
        self.assertEqual((sum(n for _, n in rows("rv")), unk("rv")), (26, 14))
        self.assertEqual((rows("len"), unk("len")), ([("short", 5), ("mid", 2), ("long", 4), ("epic", 5)], 24))
        self.assertEqual(unk("pub"), 10)
        self.assertEqual([(r["label"], r["n"]) for r in dims["pub"]["rows"][:2]], [("Devolver Digital", 12), ("Finji", 4)])

    def test_lane_dimension_uses_the_lane_function(self):
        js = "statsFor(DATA.map(normalizeGame).map(annotateNames), {laneOf: (g) => g.appid % 2 ? 'next' : ''})"
        dims = node(js, self.games)["dims"]["lane"]
        odd = sum(1 for g in self.games if g["appid"] % 2)
        self.assertEqual(dims["rows"][0]["n"], odd)
        self.assertEqual(dims["unknown"]["n"], 40 - odd)

    def test_empty_library(self):
        st = node("statsFor([], {})", None)
        self.assertEqual(st["tiles"]["games"], 0)
        self.assertIsNone(st["tiles"]["mcMedian"])
        self.assertEqual(st["dims"]["gen"]["rows"], [])


class RowHelpers(unittest.TestCase):
    ROWS = [{"key": str(y), "label": str(y), "n": n, "minutes": 60 * n, "never": 1, "tried": 0, "played": n - 1}
            for y, n in ((2009, 2), (2011, 3), (2019, 4), (2021, 5))]

    def test_year_buckets_by_decade(self):
        got = node("yearBuckets(DATA, 10)", [{**r, "key": int(r["key"])} for r in self.ROWS])
        self.assertEqual([(r["label"], r["n"]) for r in got], [("2000s", 2), ("2010s", 7), ("2020s", 5)])
        self.assertEqual(sum(r["never"] for r in got), 4)
        same = node("yearBuckets(DATA, 1)", self.ROWS)
        self.assertEqual(same, self.ROWS)

    def test_rank_rows_by_metric(self):
        rows = [{"key": "a", "label": "A", "n": 3, "minutes": 600, "never": 3, "tried": 0, "played": 0},
                {"key": "b", "label": "B", "n": 5, "minutes": 60, "never": 1, "tried": 4, "played": 0},
                {"key": "c", "label": "C", "n": 5, "minutes": 60, "never": 1, "tried": 4, "played": 0}]
        order = lambda m: [r["key"] for r in node(f"rankRows(DATA, '{m}')", rows)]
        self.assertEqual(order("games"), ["b", "c", "a"])     # ties go A to Z
        self.assertEqual(order("hours"), ["a", "b", "c"])
        self.assertEqual(order("never"), ["a", "b", "c"])     # 100% then 20%, 20%

    def test_fold_rows(self):
        rows = [{"key": str(i), "label": str(i), "n": 10 - i, "minutes": 1, "never": 1, "tried": 1, "played": 8 - i}
                for i in range(6)]
        out = node("foldRows(DATA, 4)", rows)
        self.assertEqual(len(out["rows"]), 4)
        self.assertEqual((out["other"]["values"], out["other"]["n"]), (2, 6 + 5))
        self.assertIsNone(node("foldRows(DATA, 6)", rows)["other"])

    def test_csv(self):
        dim = {"unknown": {"n": 2, "minutes": 30, "never": 1, "tried": 1, "played": 0}}
        rows = [{"key": "x", "label": 'Say "hi", ok', "n": 4, "minutes": 90, "never": 1, "tried": 1, "played": 2}]
        csv = node("statsCsv(DATA.dim, DATA.rows)", {"dim": dim, "rows": rows})
        self.assertEqual(csv.splitlines(), [
            "value,games,hours_played,never_launched,tried,played_2h_plus,percent_never_launched",
            '"Say ""hi"", ok",4,1.5,1,1,2,25',
            "Unknown,2,0.5,1,1,0,50"])


if __name__ == "__main__":
    unittest.main()
