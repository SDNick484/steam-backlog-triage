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


# ====================================================================== H2: headers on every response

class SecurityHeaders(Base):
    WANT = {"x-content-type-options": "nosniff", "referrer-policy": "same-origin",
            "content-security-policy": "frame-ancestors 'none'"}

    def check(self, r, label):
        for k, v in self.WANT.items():
            self.assertEqual(r.headers.get(k), v, f"{label}: {k}")
        self.assertNotIn("script-src", r.headers["content-security-policy"])

    def test_every_kind_of_response(self):
        s = self.srv
        etag = s.get("/api/library").headers["etag"]
        cases = {
            "200 json": s.get("/api/triage"),
            "200 page": s.get("/"),
            "200 stats page": s.get("/stats"),
            "200 healthz": s.get("/healthz"),
            "304": s.get("/api/library", {"If-None-Match": etag}),
            "400": s.put("/api/triage", {"appid": "x"}),
            "403": s.put("/api/triage", {"appid": 1, "lane": "next"}, {"Origin": "https://evil.example.com"}),
            "404": s.get("/nope"),
            "412": s.put("/api/order", {"order": [1]}, {"If-Match": '"' + "0" * 32 + '"'}),
            "415": s.post("/api/export", b"{}", {"Content-Type": "text/plain"}),
            "501 stdlib error page": s.req("PATCH", "/api/triage"),
        }
        want_status = {"304": 304, "403": 403, "404": 404, "412": 412, "415": 415, "400": 400}
        for label, r in cases.items():
            with self.subTest(label):
                if label in want_status:
                    self.assertEqual(r.status, want_status[label])
                self.check(r, label)

    def test_caching_headers_and_etags_are_as_before(self):
        r = self.srv.get("/api/triage")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertRegex(r.headers["etag"], r'^"[0-9a-f]{32}"$')
        self.assertEqual(self.srv.get("/").headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
