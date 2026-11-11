"""Checks backlog.html's score() against tests/score_vectors.json (see SCORING.md).

Standard library only. The page's normalize(), score(), playState() and mainH() are pulled out of
the HTML the same way tests/gen_score_vectors.mjs does, run under
node, and compared with the expected values. Skipped when node isn't installed, unless REQUIRE_NODE=1
(CI sets it), when a missing node is a failure so parity can never pass by being skipped.

    python3 -m unittest discover -s tests
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "backlog.html").read_text(encoding="utf-8")
VECTORS = json.loads((Path(__file__).resolve().parent / "score_vectors.json").read_text(encoding="utf-8"))


def extract_statement(src, pattern):
    m = re.search(pattern, src)
    if not m:
        raise AssertionError(f"not found in backlog.html: {pattern}")
    end = src.index(";\n", m.start())
    return src[m.start():end + 1]


def extract_function(src, name):
    start = src.index(f"function {name}(")
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def extracted():
    return "\n".join([
        extract_statement(PAGE, r"const TRIED_MIN = "),
        extract_statement(PAGE, r"const WEIGHT_KEYS = "),
        extract_statement(PAGE, r"const WEIGHT_RANGE = "),
        extract_statement(PAGE, r"const WEIGHT_STEP = "),
        extract_statement(PAGE, r"const DEFAULT_W = "),
        extract_statement(PAGE, r"const playState = "),
        extract_statement(PAGE, r"const mainH = "),
        extract_function(PAGE, "normalize"),
        extract_function(PAGE, "liveWeights"),
        extract_function(PAGE, "scoreTerms"),
        extract_function(PAGE, "score"),
    ])


RUNNER = r"""
const vm = require("node:vm");
const fs = require("node:fs");
const { code, vectors, weightSets, mode } = JSON.parse(fs.readFileSync(0, "utf8"));
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext("const S = {weights: null, wDraft: null};\n" + code + '\nconst lane = (g) => g.__lane || "";\n' +
  "this.S = S; this.normalize = normalize; this.score = score; this.scoreTerms = scoreTerms; " +
  "this.playState = playState; this.mainH = mainH; " +
  "this.consts = {WEIGHT_KEYS, WEIGHT_RANGE, WEIGHT_STEP, DEFAULT_W};", sandbox);
if (mode === "consts") {
  process.stdout.write(JSON.stringify(sandbox.consts));
} else if (mode === "sets") {
  const res = {};
  for (const set of weightSets) {
    sandbox.S.weights = set.weights;
    const rows = vectors.map((v) => {
      const g = sandbox.normalize(v.game);
      g.__lane = v.lane;
      return { id: v.id, score: sandbox.score(g), terms: sandbox.scoreTerms(g).map((t) => [t[0], t[2]]),
        labeled: sandbox.scoreTerms(g).map((t) => [t[0], t[1], t[2]]) };
    });
    res[set.name] = rows;
  }
  process.stdout.write(JSON.stringify(res));
} else {
  const out = vectors.map((v) => {
    const g = sandbox.normalize(v.game);
    g.__lane = v.lane;
    return { id: v.id, score: sandbox.score(g), playState: sandbox.playState(g), lengthHours: sandbox.mainH(g) };
  });
  process.stdout.write(JSON.stringify(out));
}
"""


def run_node(mode):
    if not shutil.which("node"):
        if os.environ.get("REQUIRE_NODE") == "1":
            raise AssertionError("REQUIRE_NODE=1 but node is not installed, so the score parity check cannot run")
        raise unittest.SkipTest("node is not installed")
    res = subprocess.run(
        ["node", "-e", RUNNER],
        input=json.dumps({"code": extracted(), "vectors": VECTORS["vectors"],
                          "weightSets": VECTORS.get("weightSets", []), "mode": mode}),
        capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise AssertionError(res.stderr)
    return json.loads(res.stdout)


class ScoreVectors(unittest.TestCase):
    def test_vectors_file_is_sane(self):
        self.assertEqual(len(VECTORS["vectors"]), 33)
        self.assertEqual(VECTORS["tolerance"], 1e-9)

    def test_scoring_functions_match_the_vectors_source(self):
        # Fails when score()/normalize() change: regenerate the vectors in both repos (SCORING.md).
        digest = hashlib.sha256(extracted().encode("utf-8")).hexdigest()
        self.assertEqual(digest, VECTORS["source"]["functionsSha256"])

    def test_page_scores_match_the_vectors(self):
        if not shutil.which("node"):
            if os.environ.get("REQUIRE_NODE") == "1":
                self.fail("REQUIRE_NODE=1 but node is not installed, so the score parity check cannot run")
            self.skipTest("node is not installed")
        res = subprocess.run(
            ["node", "-e", RUNNER], input=json.dumps({"code": extracted(), "vectors": VECTORS["vectors"]}),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = {r["id"]: r for r in json.loads(res.stdout)}
        tol = VECTORS["tolerance"]
        for v in VECTORS["vectors"]:
            with self.subTest(vector=v["id"], why=v["why"]):
                want, have = v["expected"], got[v["id"]]
                self.assertLessEqual(abs(have["score"] - want["score"]), tol)
                self.assertEqual(have["playState"], want["playState"])
                if want["lengthHours"] is None:
                    self.assertIsNone(have["lengthHours"])
                else:
                    self.assertEqual(have["lengthHours"], want["lengthHours"])

    def test_weight_sets_present(self):
        sets = VECTORS["weightSets"]
        self.assertEqual([w["name"] for w in sets], ["default", "mixed", "floor", "ceiling"])
        for w in sets:
            self.assertEqual(len(w["expected"]), 33)
            self.assertEqual(len(w["rank"]), 33)

    def test_page_matches_every_weight_set(self):
        got = run_node("sets")
        tol = VECTORS["tolerance"]
        for ws in VECTORS["weightSets"]:
            rows = {r["id"]: r for r in got[ws["name"]]}
            for want in ws["expected"]:
                with self.subTest(set=ws["name"], vector=want["id"]):
                    have = rows[want["id"]]
                    self.assertLessEqual(abs(have["score"] - want["score"]), tol)
                    self.assertEqual(have["terms"], want["terms"])
            with self.subTest(set=ws["name"], what="rank"):
                ranked = sorted(rows.values(), key=lambda r: (-r["score"], r["id"]))
                self.assertEqual([r["id"] for r in ranked], ws["rank"])

    def test_page_labels_match_backlog_query_for_every_set(self):
        # The vectors pin [key, points] only; this pins the label text too, so deck, length, tried and
        # "in Up next" wording can't drift between the page and backlog_query.score_breakdown.
        import sys
        sys.path.insert(0, str(ROOT))
        import backlog_query as q
        got = run_node("sets")
        by_id = {v["id"]: v for v in VECTORS["vectors"]}
        for ws in VECTORS["weightSets"]:
            for row in got[ws["name"]]:
                with self.subTest(set=ws["name"], vector=row["id"]):
                    v = by_id[row["id"]]
                    terms = q.score_breakdown(q.normalize(v["game"]), v["lane"], weights=ws["weights"])
                    self.assertEqual(row["labeled"], [[t.term, t.label, t.points] for t in terms])

    def test_page_weight_constants_match_backlog_query(self):
        import sys
        sys.path.insert(0, str(ROOT))
        import backlog_query as q
        c = run_node("consts")
        self.assertEqual(c["WEIGHT_KEYS"], list(q.WEIGHT_KEYS))
        self.assertEqual({k: tuple(v) for k, v in c["WEIGHT_RANGE"].items()}, dict(q.WEIGHT_RANGE))
        self.assertEqual(c["WEIGHT_STEP"], q.WEIGHT_STEP)
        self.assertEqual(c["DEFAULT_W"], dict(q.DEFAULT_WEIGHTS))

    def test_default_set_equals_the_unweighted_vectors(self):
        default = next(w for w in VECTORS["weightSets"] if w["name"] == "default")
        for v, row in zip(VECTORS["vectors"], default["expected"]):
            self.assertEqual(row["id"], v["id"])
            self.assertEqual(row["score"], v["expected"]["score"])


if __name__ == "__main__":
    unittest.main()
