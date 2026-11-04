"""Checks backlog.html's score() against tests/score_vectors.json (see SCORING.md).

Standard library only. The page's normalize(), score(), playState() and mainH() are pulled out of
the HTML the same way scripts/gen_score_vectors.mjs (in mcp-server-steam-backlog) does, run under
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
        extract_statement(PAGE, r"const playState = "),
        extract_statement(PAGE, r"const mainH = "),
        extract_function(PAGE, "normalize"),
        extract_function(PAGE, "score"),
    ])


RUNNER = r"""
const vm = require("node:vm");
const fs = require("node:fs");
const { code, vectors } = JSON.parse(fs.readFileSync(0, "utf8"));
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(code + '\nconst lane = (g) => g.__lane || "";\n' +
  "this.normalize = normalize; this.score = score; this.playState = playState; this.mainH = mainH;", sandbox);
const out = vectors.map((v) => {
  const g = sandbox.normalize(v.game);
  g.__lane = v.lane;
  return { id: v.id, score: sandbox.score(g), playState: sandbox.playState(g), lengthHours: sandbox.mainH(g) };
});
process.stdout.write(JSON.stringify(out));
"""


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


if __name__ == "__main__":
    unittest.main()
