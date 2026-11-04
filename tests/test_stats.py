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
             "controller": None, "hasDetails": False, "reviews": None}
        got = node("UNK_KEYS.map((k) => isUnknown(DATA, k))", g)
        self.assertEqual(got, [True] * 8)
        known = {**g, "genres": ["RPG"], "pubKeys": ["a"], "devKeys": ["b"], "proton": {"tier": "gold", "total": 1},
                 "metacritic": 80, "year": 2020, "controller": "full", "reviews": {"total": 1, "pct": 100, "desc": "x"}}
        self.assertEqual(node("UNK_KEYS.map((k) => isUnknown(DATA, k))", known), [False] * 8)
        # "None listed" controller support is a known value once the export had store details.
        self.assertFalse(node("isUnknown(DATA, 'ctrl')", {**g, "hasDetails": True}))


if __name__ == "__main__":
    unittest.main()
