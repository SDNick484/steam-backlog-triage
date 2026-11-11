"""GET/PUT /api/prefs and prefs.json (backlog_server.py).

Standard library only:  python3 -m unittest tests.test_prefs -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backlog_query as q
from _support import Server

ALL_ONE = {k: 1 for k in q.WEIGHT_KEYS}


class PrefsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server().start()
        cls.file = cls.s.data / "prefs.json"

    @classmethod
    def tearDownClass(cls):
        cls.s.cleanup()

    def setUp(self):
        self.file.unlink(missing_ok=True)

    def test_get_without_a_file(self):
        r = self.s.get("/api/prefs")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json(), {"weights": ALL_ONE, "default": True})
        self.assertIn("etag", r.headers)
        self.assertFalse(self.file.exists())

    def test_put_stores_and_returns_previous(self):
        r = self.s.put("/api/prefs", {"weights": {"deck": 2}})
        self.assertEqual(r.status, 200)
        body = r.json()
        self.assertEqual(body["weights"]["deck"], 2)
        self.assertEqual({k: v for k, v in body["weights"].items() if k != "deck"}, {k: 1 for k in ALL_ONE if k != "deck"})
        self.assertEqual(body["previous"], ALL_ONE)
        g = self.s.get("/api/prefs")
        self.assertEqual(g.json()["weights"]["deck"], 2)
        self.assertFalse(g.json()["default"])
        self.assertEqual(g.headers["etag"], r.headers["etag"])
        on_disk = json.loads(self.file.read_text())
        self.assertEqual(on_disk["version"], 1)
        self.assertEqual(set(on_disk["weights"]), set(q.WEIGHT_KEYS))

    def test_put_empty_resets(self):
        self.s.put("/api/prefs", {"weights": {"deck": 2}})
        r = self.s.put("/api/prefs", {"weights": {}})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["weights"], ALL_ONE)
        self.assertEqual(r.json()["previous"]["deck"], 2)
        self.assertEqual(self.s.get("/api/prefs").json(), {"weights": ALL_ONE, "default": True})

    def test_if_none_match_304(self):
        etag = self.s.get("/api/prefs").headers["etag"]
        r = self.s.get("/api/prefs", {"If-None-Match": etag})
        self.assertEqual(r.status, 304)

    def test_if_match_412(self):
        self.s.put("/api/prefs", {"weights": {"deck": 2}})
        current = self.s.get("/api/prefs").headers["etag"]
        before = self.file.read_bytes()
        r = self.s.put("/api/prefs", {"weights": {"deck": 3}}, {"If-Match": '"0000"'})
        self.assertEqual(r.status, 412)
        self.assertEqual(r.json()["etag"], current)
        self.assertEqual(self.file.read_bytes(), before)
        r = self.s.put("/api/prefs", {"weights": {"deck": 3}}, {"If-Match": current})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.s.get("/api/prefs").json()["weights"]["deck"], 3)

    def test_put_400s(self):
        bad = [[], {"weights": []}, {"weights": {"nope": 1}}, {"weights": {"deck": True}},
               {"weights": {"next": 0}}, {"weights": {"deck": 0.3}}]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.s.put("/api/prefs", body).status, 400)
        big = json.dumps({"weights": {}, "pad": "x" * 5000}).encode()
        self.assertGreater(len(big), 4096)
        self.assertEqual(self.s.put("/api/prefs", big, {"Content-Type": "application/json"}).status, 400)
        self.assertEqual(self.s.put("/api/prefs", b"not json").status, 400)
        self.assertFalse(self.file.exists())

    def test_cross_site_put_403(self):
        r = self.s.put("/api/prefs", {"weights": {"deck": 2}}, {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 403)
        self.assertFalse(self.file.exists())

    def test_bad_file_falls_back_per_key(self):
        self.file.write_text('{"version":1,"weights":{"deck":2,"next":0,"zzz":1}}')
        w = self.s.get("/api/prefs").json()["weights"]
        self.assertEqual((w["deck"], w["next"]), (2, 1))
        self.file.write_text("not json")
        self.assertEqual(self.s.get("/api/prefs").json(), {"weights": ALL_ONE, "default": True})
        self.file.write_text("[1, 2]")
        self.assertEqual(self.s.get("/api/prefs").json(), {"weights": ALL_ONE, "default": True})
        self.assertEqual(self.s.get("/healthz").status, 200)
        log = self.s.log_text()
        prefs_lines = [ln for ln in log.splitlines() if ln.startswith("prefs:")]
        self.assertTrue(prefs_lines, log)
        for ln in prefs_lines:
            self.assertIn("prefs.json", ln)
            self.assertNotIn("zzz", ln)
            self.assertFalse(any(ch.isdigit() for ch in ln), ln)  # no value from the file, in any form

    def test_api_version_still_2(self):
        for r in (self.s.get("/api/prefs"), self.s.put("/api/prefs", {"weights": {}}), self.s.put("/api/prefs", [])):
            self.assertEqual(r.headers["x-backlog-api"], "2")


if __name__ == "__main__":
    unittest.main()
