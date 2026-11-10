"""Ops hardening: the same-origin guard on mutating routes, headers on every response, the run status file
and the deep health check. Standard library only.

    python3 -m unittest -v tests.test_hardening
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ask  # noqa: E402
from _support import Server, load_fixture_library  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = Server().start()
        self.addCleanup(self.srv.cleanup)
        self.host = f"127.0.0.1:{self.srv.port}"


# ====================================================================== H1: same-origin guard

class CrossSiteFunction(unittest.TestCase):
    """ask.cross_site_request(headers): one check for Ask and for every mutating route."""

    def check(self, **h) -> bool:
        return ask.cross_site_request({k.replace("_", "-"): v for k, v in h.items()})

    def test_matrix(self):
        self.assertFalse(self.check(), "no headers: curl, cron, the MCP client")
        self.assertFalse(self.check(Host="a.example.com", Origin="http://a.example.com"))
        self.assertFalse(self.check(Host="a.example.com", Origin="https://A.example.com"), "case-insensitive")
        self.assertTrue(self.check(Host="a.example.com", Origin="https://b.example.com"))
        self.assertTrue(self.check(Host="a.example.com", Origin="null"))
        self.assertTrue(self.check(Host="a.example.com:8484", Origin="http://a.example.com"), "port differs")
        self.assertTrue(self.check(Sec_Fetch_Site="cross-site"))
        self.assertTrue(self.check(Sec_Fetch_Site="Cross-Site"))
        self.assertFalse(self.check(Sec_Fetch_Site="same-origin"))
        self.assertFalse(self.check(Sec_Fetch_Site="same-site"))
        self.assertFalse(self.check(Sec_Fetch_Site="none"))

    def test_ask_uses_the_same_function(self):
        self.assertTrue(ask.Ask.cross_site({"Sec-Fetch-Site": "cross-site"}))
        self.assertFalse(ask.Ask.cross_site({}))


class SameOriginGuard(Base):
    ROUTES = [
        ("PUT", "/api/triage", {"appid": 620, "lane": "next"}),
        ("PUT", "/api/notes", {"appid": 620, "note": "hello"}),
        ("PUT", "/api/order", {"order": [620, 730]}),
        ("PUT", "/api/library", {"games": [{"appid": 1, "name": "x"}]}),
        ("POST", "/api/export", {}),
        ("POST", "/api/ask", {"thread": [{"q": "hi"}]}),
        ("DELETE", "/api/triage", None),
    ]
    REFUSED = {"error": "cross-site request refused"}

    def untouched(self):
        d = self.srv.data
        for name in ("triage.json", "notes.json", "order.json", ".export.lock"):
            self.assertFalse((d / name).exists(), name)
        self.assertEqual(json.loads((d / "steam_backlog.json").read_text()), load_fixture_library())

    def call(self, method, path, body, headers):
        return self.srv.req(method, path, body, headers)

    def test_cross_origin_refused_on_every_mutating_route(self):
        for method, path, body in self.ROUTES:
            with self.subTest(method=method, path=path):
                r = self.call(method, path, body, {"Origin": "https://evil.example.com"})
                self.assertEqual(r.status, 403, r.text())
                if path != "/api/ask":  # Ask keeps its own message
                    self.assertEqual(r.json(), self.REFUSED)
                    self.assertTrue(r.headers["content-type"].startswith("application/json"))
        self.untouched()

    def test_sec_fetch_site_cross_site_refused(self):
        for method, path, body in self.ROUTES:
            with self.subTest(method=method, path=path):
                r = self.call(method, path, body, {"Sec-Fetch-Site": "cross-site"})
                self.assertEqual(r.status, 403, r.text())
        self.untouched()

    def test_unknown_post_and_put_paths_are_refused_too(self):
        for method in ("POST", "PUT"):
            r = self.call(method, "/api/nope", {}, {"Origin": "https://evil.example.com"})
            self.assertEqual((r.status, r.json()), (403, self.REFUSED))

    def test_oversized_body_gets_the_403_not_a_reset(self):
        r = self.call("PUT", "/api/notes", b"x" * 1000, {"Origin": "https://evil.example.com"})
        self.assertEqual(r.status, 403)

    def test_no_headers_passes(self):
        """curl, cron and the MCP server's loopback client send neither header."""
        self.assertEqual(self.call("PUT", "/api/triage", {"appid": 620, "lane": "next"}, {}).status, 200)
        self.assertEqual(self.call("PUT", "/api/notes", {"appid": 620, "note": "n"}, {}).status, 200)
        self.assertEqual(self.call("PUT", "/api/order", {"order": [620]}, {}).status, 200)
        self.assertEqual(self.call("POST", "/api/export", {}, {}).status, 202)
        self.assertEqual(self.call("DELETE", "/api/triage", None, {}).status, 501, "unchanged: unsupported")

    def test_same_origin_passes(self):
        h = {"Origin": "http://" + self.host, "Sec-Fetch-Site": "same-origin"}
        r = self.call("PUT", "/api/triage", {"appid": 620, "lane": "done"}, h)
        self.assertEqual(r.status, 200, r.text())
        self.assertEqual(self.call("GET", "/api/triage", None, {}).json(), {"620": "done"})

    def test_same_site_and_none_pass(self):
        for site in ("same-site", "none"):
            r = self.call("PUT", "/api/notes", {"appid": 1, "note": site}, {"Sec-Fetch-Site": site})
            self.assertEqual(r.status, 200, site)

    def test_matching_origin_without_sec_fetch_passes(self):
        r = self.call("PUT", "/api/order", {"order": [1]}, {"Origin": "http://" + self.host})
        self.assertEqual(r.status, 200)

    def test_reads_are_never_refused(self):
        r = self.call("GET", "/api/triage", None, {"Origin": "https://evil.example.com", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
