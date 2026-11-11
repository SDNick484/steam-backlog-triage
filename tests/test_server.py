"""backlog_server.py: every route in the README's API table, and the API 2 behavior.

The expected behavior is docs/backlog-api-contract.md in SDNick484/mcp-server-steam-backlog; each test
quotes the section it checks. Where the contract is silent (health check, page, size limits) the
docstring quotes the README or the server's own module docstring instead.

Standard library only:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `python -m unittest tests.test_server` works too

from _support import ROOT, SERVER, Server, raw_request, request, seed_library

ETAG_RE = re.compile(r'^"[0-9a-f]{32}"$')


def etag_of(body: bytes) -> str:
    """Contract §2.2: first 32 hex characters of SHA-256 over the exact body bytes, in quotes."""
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


class Base(unittest.TestCase):
    seed = True

    def setUp(self):
        self.srv = Server(seed=self.seed).start()
        self.addCleanup(self.srv.cleanup)

    def assert_api_headers(self, r):
        self.assertEqual(r.headers.get("x-backlog-server"), "1", "X-Backlog-Server")
        self.assertEqual(r.headers.get("x-backlog-api"), "2", "X-Backlog-Api")


# ====================================================================== versioning, pages, 404s

class Versioning(Base):
    def test_every_status_carries_both_version_headers(self):
        """Contract §1: "Every response ... (all paths, all status codes, including 304 and errors) MUST
        carry: X-Backlog-Server: 1 ... X-Backlog-Api: 2"."""
        s = self.srv
        etag = s.get("/api/library").headers["etag"]
        cases = {
            200: s.get("/api/triage"),
            304: s.get("/api/library", {"If-None-Match": etag}),
            400: s.put("/api/triage", {"appid": "x"}),
            404: s.get("/nope"),
            412: s.put("/api/order", {"order": [1]}, {"If-Match": '"' + "0" * 32 + '"'}),
            415: s.post("/api/export", b"{}", {"Content-Type": "text/plain"}),
        }
        for status, r in cases.items():
            with self.subTest(status=status):
                self.assertEqual(r.status, status)
                self.assert_api_headers(r)

    def test_standard_library_error_pages_carry_them_too(self):
        """backlog_server.py, send_response(): the headers go on "the standard library's own error pages
        (bad request line, unsupported method, ...)" as well."""
        r = self.srv.req("DELETE", "/api/triage")
        self.assertEqual(r.status, 501)
        self.assert_api_headers(r)
        raw = raw_request(self.srv.port, b"GET / HTTP/9.9\r\n\r\n")
        if raw.startswith(b"HTTP/"):
            low = raw.lower()
            self.assertIn(b"x-backlog-api: 2", low)
            self.assertIn(b"x-backlog-server: 1", low)
        else:
            # Python 3.12 and older answer an unsupported HTTP version in the HTTP/0.9 style: an error page with
            # no status line and so no headers at all. That is the standard library's doing, and no header can be
            # added to it; what matters is that it is still the error page and the connection is closed cleanly.
            self.assertIn(b"error code: 505", raw.lower())

    def test_cache_control_no_store_stays_on_every_response(self):
        """Contract §2.2: "Cache-Control: no-store stays on every response"."""
        s = self.srv
        etag = s.get("/api/library").headers["etag"]
        for r in (s.get("/api/library"), s.get("/api/triage"), s.get("/nope"),
                  s.get("/api/library", {"If-None-Match": etag})):
            self.assertEqual(r.headers.get("cache-control"), "no-store")


class PageAndHealth(Base):
    def test_healthz(self):
        """README, API table: GET /healthz -> "ok"."""
        r = self.srv.get("/healthz")
        self.assertEqual((r.status, r.text()), (200, "ok"))
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assert_api_headers(r)

    def test_dashboard_page_and_alias(self):
        """README, API table: GET / -> Dashboard. The page is wrapped in a document shell."""
        for path in ("/", "/index.html"):
            r = self.srv.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertTrue(r.headers["content-type"].startswith("text/html"))
            self.assertIn("<!doctype html>", r.text().lower())
            self.assertIn("window.APP_CONFIG=", r.text())
            self.assertIn("Refresh library", r.text())

    def test_stats_page_is_the_same_page_at_stats(self):
        """README, API table: GET /stats and /stats/ -> the dashboard, which shows its Statistics view from the path."""
        home = self.srv.get("/").text()
        for path in ("/stats", "/stats/", "/stats?x=1"):
            r = self.srv.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertTrue(r.headers["content-type"].startswith("text/html"), path)
            self.assertEqual(r.text(), home, path)
        h = self.srv.req("HEAD", "/stats")
        self.assertEqual(h.status, 200)
        self.assertEqual(self.srv.get("/stats/x").status, 404)
        self.assertEqual(self.srv.get("/statsx").status, 404)

    def test_nav_links_reach_the_page_config(self):
        """backlog_server.py docstring: --nav "Deals=https://deals.example.com/" adds a header link."""
        with Server(nav=["Deals=https://deals.example.com/"]) as s:
            html = s.get("/").text()
        self.assertIn('"label": "Deals"', html)
        self.assertIn("https://deals.example.com/", html)

    def test_missing_page_is_a_json_500_that_names_no_path(self):
        """Contract §7: error messages MUST NOT include filesystem paths."""
        with Server(page=Path("/nonexistent/backlog.html")) as s:
            r = s.get("/")
        self.assertEqual(r.status, 500)
        self.assertEqual(r.json(), {"error": "dashboard page missing"})

    def test_query_strings_are_ignored_when_routing(self):
        """backlog_server.py splits the query off before routing."""
        self.assertEqual(self.srv.get("/api/triage?x=1").status, 200)
        self.assertEqual(self.srv.get("/healthz?probe=1").text(), "ok")


class NotFound(Base):
    def test_unknown_paths_are_404_json(self):
        """Contract §7: 404 is used for "unknown path"; errors stay {"error": "<message>"} as JSON."""
        s = self.srv
        for r in (s.get("/nope"), s.get("/api"), s.get("/api/nope"), s.get("/api/export/nope"),
                  s.put("/nope", {"x": 1}), s.put("/api/nope", {"x": 1}), s.post("/nope", {}),
                  s.post("/api/library", {})):
            with self.subTest(status=r.status):
                self.assertEqual(r.status, 404)
                self.assertEqual(r.json(), {"error": "not found"})
                self.assertTrue(r.headers["content-type"].startswith("application/json"))

    def test_error_bodies_leak_no_paths_or_tracebacks(self):
        """Contract §7: "Messages MUST NOT include filesystem paths, exception text or command output"."""
        s = self.srv
        bodies = [s.put("/api/triage", b"{not json").text(), s.put("/api/library", {"games": "x"}).text(),
                  s.get("/nope").text(), s.put("/api/order", {"order": "x"}).text()]
        for text in bodies:
            self.assertNotIn(str(s.data), text)
            self.assertNotIn("Traceback", text)
            self.assertNotIn("/home/", text)

    @unittest.expectedFailure
    def test_invalid_json_error_does_not_carry_exception_text(self):
        """Contract §7: "Messages MUST NOT include filesystem paths, exception text or command output; details
        go to the server's stderr." KNOWN GAP: do_PUT answers 400 {"error": str(e)} for a body that isn't JSON,
        so Python's JSONDecodeError text ("Expecting value: line 1 column 1 (char 0)") reaches the client. It
        holds no path and no secret, so this is expected to fail until the server says something generic;
        when it starts passing, unittest reports an unexpected success: remove the decorator then."""
        for path in ("/api/triage", "/api/notes", "/api/order", "/api/library"):
            r = self.srv.put(path, b"{not json", {"Content-Type": "application/json"})
            self.assertEqual(r.status, 400)
            self.assertNotRegex(r.json()["error"], r"Expecting|line \d+ column \d+|char \d+|Traceback")

    def test_oversized_request_body_is_refused_without_reading_it(self):
        """backlog_server.py: MAX_BODY is 32 MiB; a larger Content-Length gets 400 "missing or oversized body"."""
        head = (b"PUT /api/triage HTTP/1.0\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(32 * 1024 * 1024 + 1).encode() + b"\r\n\r\n")
        raw = raw_request(self.srv.port, head)
        self.assertTrue(raw.startswith(b"HTTP/1.0 400"), raw[:60])
        self.assertIn(b"missing or oversized body", raw)

    def test_put_with_no_body_is_400(self):
        """backlog_server.py body(): an empty body is "missing or oversized body"."""
        for path in ("/api/triage", "/api/notes", "/api/order", "/api/library"):
            with self.subTest(path=path):
                r = self.srv.req("PUT", path)
                self.assertEqual(r.status, 400)
                self.assertIn("missing or oversized body", r.json()["error"])


class StartupOptions(unittest.TestCase):
    def run_server(self, *args):
        return subprocess.run([sys.executable, str(SERVER), "--port", "0", *args], capture_output=True,
                              text=True, timeout=30, stdin=subprocess.DEVNULL)

    def test_export_cmd_must_be_an_executable_file(self):
        """Contract §4.1: --export-cmd PATH is "the command to run"; the server refuses a bad one at startup."""
        r = self.run_server("--export-cmd", "/nonexistent/run-export.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("isn't an executable file", r.stderr)

    def test_export_timeout_must_be_positive(self):
        """Contract §4.1: --export-timeout SECONDS, "wall-clock limit for one run"."""
        r = self.run_server("--export-timeout", "0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--export-timeout must be a positive number", r.stderr)

    def test_nav_needs_label_and_url(self):
        """backlog_server.py docstring: --nav expects LABEL=URL."""
        r = self.run_server("--nav", "no-equals-sign")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--nav expects LABEL=URL", r.stderr)


# ====================================================================== /api/library

class Library(Base):
    seed = False

    def test_404_before_the_first_export_and_no_etag(self):
        """Contract §2.4: with no export file "GET /api/library still answers 404 {"error": "no export yet"},
        with no ETag"."""
        r = self.srv.get("/api/library")
        self.assertEqual(r.status, 404)
        self.assertEqual(r.json(), {"error": "no export yet"})
        self.assertNotIn("etag", r.headers)

    def test_if_none_match_star_on_a_missing_library_is_still_404(self):
        """Contract §2.4: "If-None-Match: * on a missing library also gets that 404"."""
        r = self.srv.get("/api/library", {"If-None-Match": "*"})
        self.assertEqual(r.status, 404)
        self.assertEqual(r.json(), {"error": "no export yet"})

    def test_put_rejects_anything_that_is_not_an_export(self):
        """Contract §3.3: "PUT /api/library with a non-object body: 400 already, unchanged"; README: a library
        without games is not an export ("not a steam_backlog_export.py file")."""
        bad = [[], [1], "text", 5, {}, {"games": "x"}, {"games": [1]}, {"games": [{"name": "no appid"}]},
               {"games": [{"appid": 1}, "x"]}]
        for body in bad:
            with self.subTest(body=body):
                r = self.srv.put("/api/library", json.dumps(body).encode(), {"Content-Type": "application/json"})
                self.assertEqual(r.status, 400)
                self.assertEqual(r.json(), {"error": "not a steam_backlog_export.py file"})
        self.assertEqual(self.srv.get("/api/library").status, 404, "nothing was written")

    def test_put_then_get_round_trips_and_counts_games(self):
        """README, API table: PUT /api/library replaces the current export (the page's "Load export")."""
        lib = {"schema": 2, "games": [{"appid": 1, "name": "A"}, {"appid": 2, "name": "B"}], "persona": "ü"}
        r = self.srv.put("/api/library", lib)
        self.assertEqual((r.status, r.json()), (200, {"ok": True, "games": 2}))
        got = self.srv.get("/api/library")
        self.assertEqual(got.status, 200)
        self.assertEqual(got.json(), lib)
        self.assertTrue(got.headers["content-type"].startswith("application/json"))

    def test_an_empty_games_list_is_a_valid_export(self):
        """README: the check is "games" is a list of objects with an appid; an empty list passes."""
        self.assertEqual(self.srv.put("/api/library", {"games": []}).json(), {"ok": True, "games": 0})


class LibraryEtags(Base):
    def test_etag_is_a_quoted_strong_sha256_prefix_of_the_body(self):
        """Contract §2.2: 'ETag: "<32 lowercase hex chars>"' ... "the first 32 hex characters of SHA-256 over
        the exact response body bytes. It is a strong validator (no W/ prefix)"."""
        r = self.srv.get("/api/library")
        self.assertEqual(r.status, 200)
        self.assertRegex(r.headers["etag"], ETAG_RE)
        self.assertEqual(r.headers["etag"], etag_of(r.body))
        self.assertEqual(self.srv.get("/api/library").headers["etag"], r.headers["etag"], "same bytes, same tag")

    def test_304_on_a_matching_if_none_match_has_headers_and_no_body(self):
        """Contract §2.3: "the server MUST answer 304 Not Modified with ETag, Cache-Control, X-Backlog-Server
        and X-Backlog-Api headers and no body"."""
        etag = self.srv.get("/api/library").headers["etag"]
        r = self.srv.get("/api/library", {"If-None-Match": etag})
        self.assertEqual(r.status, 304)
        self.assertEqual(r.body, b"")
        self.assertEqual(r.headers["etag"], etag)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assert_api_headers(r)

    def test_if_none_match_forms(self):
        """Contract §2.3: "a comma-separated list of entity tags, or *. Comparison is weak: W/"abc" matches
        "abc". If any listed tag matches the current ETag, or the value is * and the resource exists ... 304.
        Otherwise it answers as a normal GET"."""
        etag = self.srv.get("/api/library").headers["etag"]
        other = '"' + "f" * 32 + '"'
        forms = {
            "exact": (etag, 304),
            "weak": ("W/" + etag, 304),
            "list, match last": (f'{other}, "{"e" * 32}", {etag}', 304),
            "list, match first, odd spacing": (f"{etag}  ,{other}", 304),
            "star": ("*", 304),
            "no match": (other, 200),
            "list, no match": (f'{other}, W/"{"d" * 32}"', 200),
        }
        for name, (header, want) in forms.items():
            with self.subTest(name):
                self.assertEqual(self.srv.get("/api/library", {"If-None-Match": header}).status, want)

    def test_unparseable_if_none_match_is_a_normal_200(self):
        """Contract §2.3: "An unparseable value is treated as no match (normal 200), never as an error"."""
        for header in ("garbage", etag_of(b"x").strip('"'), '"unterminated', ",,,", "W/", "<>"):
            with self.subTest(header=header):
                r = self.srv.get("/api/library", {"If-None-Match": header})
                self.assertEqual(r.status, 200)
                self.assertRegex(r.headers["etag"], ETAG_RE)

    def test_head_returns_the_get_headers_without_a_body(self):
        """Contract §2.1: "HEAD on each of these MUST return the same headers as GET, ETag included, with no body"."""
        for path in ("/api/library", "/api/triage", "/api/notes", "/api/order"):
            with self.subTest(path=path):
                g, h = self.srv.get(path), self.srv.req("HEAD", path)
                self.assertEqual(h.status, 200)
                self.assertEqual(h.body, b"")
                self.assertEqual(h.headers["etag"], g.headers["etag"])
                self.assertEqual(h.headers["content-length"], g.headers["content-length"])
                self.assertEqual(h.headers["content-type"], g.headers["content-type"])

    def test_a_replaced_library_gets_a_new_etag_and_the_old_one_stops_matching(self):
        """Contract §2.2: "a body that changes MUST get a different one"; §2.3 revalidation after an export:
        "200 with the new body and new ETag"."""
        old = self.srv.get("/api/library").headers["etag"]
        lib = json.loads(self.srv.get("/api/library").body)
        lib["exportedAt"] = "2030-01-01T00:00:00+00:00"
        seed_library(self.srv.data, lib)  # what the exporter's atomic write does
        r = self.srv.get("/api/library", {"If-None-Match": old})
        self.assertEqual(r.status, 200)
        self.assertNotEqual(r.headers["etag"], old)
        self.assertEqual(r.headers["etag"], etag_of(r.body))
        self.assertEqual(self.srv.get("/api/library", {"If-None-Match": r.headers["etag"]}).status, 304)

    def test_a_put_library_changes_the_etag_too(self):
        """Contract §2.1 table: the library's ETag source is "the bytes of steam_backlog_export as served"."""
        old = self.srv.get("/api/library").headers["etag"]
        lib = json.loads(self.srv.get("/api/library").body)
        lib["games"] = lib["games"][:1]
        self.assertEqual(self.srv.put("/api/library", lib).status, 200)
        new = self.srv.get("/api/library").headers["etag"]
        self.assertNotEqual(new, old)

    def test_etags_survive_a_server_restart(self):
        """Contract §2.2: same bytes get the same ETag "including across server restarts. That rules out
        mtime- or counter-based tags"."""
        before = {p: self.srv.get(p).headers["etag"] for p in ("/api/library", "/api/triage")}
        self.srv.stop()
        with Server(self.srv.data) as again:
            after = {p: again.get(p).headers["etag"] for p in before}
        self.assertEqual(before, after)


# ====================================================================== small resources

class SmallResource:
    """Shared behavior of triage, notes and order. Subclasses say how to PUT and what an empty one is."""

    path = ""
    empty = None
    other = None  # a second valid PUT body that changes the resource
    first = None  # a valid PUT body

    def put_ok(self, body, headers=None):
        return self.srv.put(self.path, body, headers)

    def test_empty_resource_is_served_with_an_etag(self):
        """Contract §2.1/§3.1: "the three resources always exist; an absent file reads as {} or []"."""
        r = self.srv.get(self.path)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json(), self.empty)
        self.assertEqual(r.headers["etag"], etag_of(r.body))
        self.assertRegex(r.headers["etag"], ETAG_RE)

    def test_if_none_match_gives_304_after_a_change_gives_200(self):
        """Contract §2.3 applies to "the four resources above"."""
        etag = self.srv.get(self.path).headers["etag"]
        self.assertEqual(self.srv.get(self.path, {"If-None-Match": etag}).status, 304)
        self.assertEqual(self.srv.get(self.path, {"If-None-Match": "W/" + etag}).status, 304)
        self.assertEqual(self.put_ok(self.first).status, 200)
        r = self.srv.get(self.path, {"If-None-Match": etag})
        self.assertEqual(r.status, 200)
        self.assertNotEqual(r.headers["etag"], etag)

    def test_put_response_etag_is_the_new_resource_etag(self):
        """Contract §3.2: "Every successful PUT to these three resources MUST return the new ETag in the
        ETag header"."""
        r = self.put_ok(self.first)
        self.assertEqual(r.status, 200)
        self.assertRegex(r.headers["etag"], ETAG_RE)
        self.assertEqual(r.headers["etag"], self.srv.get(self.path).headers["etag"])

    def test_matching_if_match_writes(self):
        """Contract §3.1: "Match: perform the write". `*` "matches always"; a list matches if any tag does."""
        stale = '"' + "0" * 32 + '"'
        cur = self.srv.get(self.path).headers["etag"]
        self.assertEqual(self.put_ok(self.first, {"If-Match": cur}).status, 200, "the exact current tag")
        self.assertEqual(self.put_ok(self.other, {"If-Match": "*"}).status, 200, "*")
        cur = self.srv.get(self.path).headers["etag"]
        self.assertEqual(self.put_ok(self.first, {"If-Match": f"{stale}, {cur}"}).status, 200, "a list with a match")

    def test_stale_if_match_is_412_and_writes_nothing(self):
        """Contract §3.1: "No match: write nothing and answer 412 Precondition Failed with the current ETag in
        the header and the body: {"error": "precondition failed", "etag": "\\"<current etag>\\""}"."""
        self.assertEqual(self.put_ok(self.first).status, 200)
        before = self.srv.get(self.path)
        r = self.put_ok(self.other, {"If-Match": '"' + "0" * 32 + '"'})
        self.assertEqual(r.status, 412)
        self.assertEqual(r.json(), {"error": "precondition failed", "etag": before.headers["etag"]})
        self.assertEqual(r.headers["etag"], before.headers["etag"])
        self.assert_api_headers(r)
        after = self.srv.get(self.path)
        self.assertEqual(after.body, before.body, "a refused write must not change anything")
        self.assertEqual(after.headers["etag"], before.headers["etag"])

    def test_if_match_made_stale_by_an_intervening_write(self):
        """Contract §3.4: the MCP reads, the page changes the resource, then the MCP PUTs with the old tag and
        gets 412; "the MCP then re-reads"; re-sending the fresh tag works."""
        tag = self.srv.get(self.path).headers["etag"]
        self.assertEqual(self.put_ok(self.first).status, 200)  # "the page" changes it
        refused = self.put_ok(self.other, {"If-Match": tag})
        self.assertEqual(refused.status, 412)
        fresh = refused.json()["etag"]
        self.assertEqual(self.put_ok(self.other, {"If-Match": fresh}).status, 200)

    def test_weak_tags_never_satisfy_if_match(self):
        """Contract §3.1: "compares with strong comparison" (RFC 9110: a W/ tag never matches)."""
        cur = self.srv.get(self.path).headers["etag"]
        r = self.put_ok(self.first, {"If-Match": "W/" + cur})
        self.assertEqual(r.status, 412)

    def test_validation_comes_before_the_precondition(self):
        """Contract §3.1: "Validation (400) is checked before the precondition, so a malformed request is a 400
        even with a stale tag."""
        r = self.srv.put(self.path, [1, 2, 3, "bad"] if self.path != "/api/order" else {"order": ["x"]},
                         {"If-Match": '"' + "0" * 32 + '"'})
        self.assertEqual(r.status, 400)

    def test_no_if_match_keeps_todays_behavior(self):
        """Contract §3.1: "No If-Match: exactly today's behavior (plus the new response fields in §3.2)"; the
        page sends none."""
        self.assertEqual(self.put_ok(self.first).status, 200)
        self.assertEqual(self.put_ok(self.other).status, 200)

    def test_survives_a_restart(self):
        """README: marks, notes and order "are saved on the server" (triage.json, notes.json, order.json)."""
        self.put_ok(self.first)
        want = self.srv.get(self.path)
        self.srv.stop()
        with Server(self.srv.data) as again:
            got = again.get(self.path)
        self.assertEqual(got.json(), want.json())
        self.assertEqual(got.headers["etag"], want.headers["etag"])



class Triage(SmallResource, Base):
    path = "/api/triage"
    empty = {}
    first = {"appid": 620, "lane": "next"}
    other = {"appid": 620, "lane": "done"}

    def test_put_returns_the_stored_lane_and_the_previous_one(self):
        """Contract §3.2: 'PUT /api/triage -> {"ok": true, "appid": 620, "lane": "done", "previous": "next"}';
        "previous" is "next | someday | skip | done | "" ("" means no lane)"."""
        r1 = self.srv.put(self.path, {"appid": 620, "lane": "next"})
        self.assertEqual(r1.json(), {"ok": True, "appid": 620, "lane": "next", "previous": ""})
        r2 = self.srv.put(self.path, {"appid": 620, "lane": "done"})
        self.assertEqual(r2.json(), {"ok": True, "appid": 620, "lane": "done", "previous": "next"})
        self.assertEqual(self.srv.get(self.path).json(), {"620": "done"})

    def test_every_lane_is_accepted_and_empty_clears(self):
        """README, API table: PUT {"appid": 620, "lane": "next"} ("" clears). Lanes: next, someday, skip, done."""
        for lane in ("next", "someday", "skip", "done"):
            with self.subTest(lane=lane):
                self.srv.put(self.path, {"appid": 7, "lane": lane})
                self.assertEqual(self.srv.get(self.path).json(), {"7": lane})
        r = self.srv.put(self.path, {"appid": 7, "lane": ""})
        self.assertEqual(r.json(), {"ok": True, "appid": 7, "lane": "", "previous": "done"})
        self.assertEqual(self.srv.get(self.path).json(), {})

    def test_a_missing_lane_clears_like_an_empty_one(self):
        """backlog_server.py: data.get("lane", "") - omitting the lane is the same as ""."""
        self.srv.put(self.path, {"appid": 5, "lane": "skip"})
        self.assertEqual(self.srv.put(self.path, {"appid": 5}).json()["previous"], "skip")
        self.assertEqual(self.srv.get(self.path).json(), {})

    def test_clearing_a_game_that_has_no_lane_is_fine(self):
        """Contract §3.2: `previous` "" "means no lane"."""
        self.assertEqual(self.srv.put(self.path, {"appid": 99, "lane": ""}).json()["previous"], "")

    def test_bad_requests_are_400(self):
        """Contract §3.3: a non-object body -> 400 {"error": "expected a JSON object"}; "appid": true ->
        400 {"error": "expected {appid: int, lane: next|someday|skip|done|''}"} (not key "True" written).
        README: PUT {"appid": 620, "lane": "next"}."""
        shape = "expected {appid: int, lane: next|someday|skip|done|''}"
        cases = [
            ([1], "expected a JSON object"), ("x", "expected a JSON object"), (None, "expected a JSON object"),
            ({"appid": True, "lane": "next"}, shape), ({"appid": "620", "lane": "next"}, shape),
            ({"appid": 6.5, "lane": "next"}, shape), ({"appid": None, "lane": "next"}, shape),
            ({"lane": "next"}, shape), ({"appid": 620, "lane": "bogus"}, shape),
            ({"appid": 620, "lane": 5}, shape), ({"appid": 620, "lane": None}, shape),
            ({"appid": 620, "lane": "NEXT"}, shape),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                r = self.srv.put(self.path, json.dumps(body).encode(), {"Content-Type": "application/json"})
                self.assertEqual(r.status, 400)
                self.assertEqual(r.json(), {"error": message})
        self.assertEqual(self.srv.get(self.path).json(), {}, "no bad request may write anything")

    def test_invalid_json_is_400(self):
        """backlog_server.py do_PUT: a body that isn't JSON is a 400 with a message, not a dropped connection."""
        r = self.srv.put(self.path, b"{nope", {"Content-Type": "application/json"})
        self.assertEqual(r.status, 400)
        self.assertIn("error", r.json())

    def test_a_boolean_appid_does_not_write_the_key_True(self):
        """Contract §3.3 table: today 'PUT /api/triage with "appid": true writes key "True"'; API 2 makes it a 400."""
        self.srv.put(self.path, {"appid": True, "lane": "next"})
        self.assertNotIn("True", self.srv.get(self.path).json())

    def test_file_on_disk_is_compact_json_keyed_by_string_appid(self):
        """README: marks "are saved on the server" in triage.json."""
        self.srv.put(self.path, {"appid": 620, "lane": "next"})
        self.assertEqual((self.srv.data / "triage.json").read_text(), '{"620":"next"}')
        leftovers = [p.name for p in self.srv.data.iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [], "atomic writes leave no temp files")

    def test_if_match_covers_the_whole_map(self):
        """Contract §3.1, note on granularity: "the ETag covers the whole map. An If-Match there fails if any
        game changed"."""
        tag = self.srv.get(self.path).headers["etag"]
        self.srv.put(self.path, {"appid": 1, "lane": "next"})
        r = self.srv.put(self.path, {"appid": 2, "lane": "skip"}, {"If-Match": tag})
        self.assertEqual(r.status, 412)
        self.assertEqual(self.srv.get(self.path).json(), {"1": "next"})

    def test_concurrent_writes_to_different_games_all_land(self):
        """Contract §3.1: "The check and the write MUST happen under one hold of the lock"; the server
        serializes writers, so parallel PUTs lose nothing."""
        errors = []

        def put(i):
            try:
                r = request(self.srv.port, "PUT", self.path, {"appid": 1000 + i, "lane": "next"})
                if r.status != 200:
                    errors.append((i, r.status))
            except Exception as e:  # pragma: no cover - reported below
                errors.append((i, repr(e)))

        threads = [threading.Thread(target=put, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(self.srv.get(self.path).json()), sorted(str(1000 + i) for i in range(24)))


class Notes(SmallResource, Base):
    path = "/api/notes"
    empty = {}
    first = {"appid": 620, "note": "first"}
    other = {"appid": 620, "note": "second"}

    def test_put_returns_the_stored_text_and_the_previous_text(self):
        """Contract §3.2: 'PUT /api/notes -> {"ok": true, "appid": 620, "note": "<stored text>", "previous":
        "<old text>"}'; "note is the text actually stored: today's server strips it"."""
        r1 = self.srv.put(self.path, {"appid": 620, "note": "  hello there \n"})
        self.assertEqual(r1.json(), {"ok": True, "appid": 620, "note": "hello there", "previous": ""})
        r2 = self.srv.put(self.path, {"appid": 620, "note": "new"})
        self.assertEqual(r2.json(), {"ok": True, "appid": 620, "note": "new", "previous": "hello there"})
        self.assertEqual(self.srv.get(self.path).json(), {"620": "new"})

    def test_empty_or_blank_clears_the_note(self):
        """README, API table: PUT {"appid": 620, "note": "..."} ("" clears). Contract §3.2: `""` means the note
        was cleared; a blank note strips to ""."""
        self.srv.put(self.path, {"appid": 620, "note": "keep?"})
        r = self.srv.put(self.path, {"appid": 620, "note": "   "})
        self.assertEqual(r.json(), {"ok": True, "appid": 620, "note": "", "previous": "keep?"})
        self.assertEqual(self.srv.get(self.path).json(), {})
        self.srv.put(self.path, {"appid": 620, "note": "again"})
        self.assertEqual(self.srv.put(self.path, {"appid": 620}).json()["previous"], "again")

    def test_two_thousand_characters_fit_and_two_thousand_and_one_do_not(self):
        """README, API table: notes are "up to 2,000 characters". Contract §3.4: a 2,001-character note is
        400 {"error": "notes are limited to 2000 characters"}."""
        ok = self.srv.put(self.path, {"appid": 1, "note": "x" * 2000})
        self.assertEqual(ok.status, 200)
        r = self.srv.put(self.path, {"appid": 2, "note": "x" * 2001})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.json(), {"error": "notes are limited to 2000 characters"})
        self.assertNotIn("2", self.srv.get(self.path).json())

    def test_the_limit_counts_characters_not_bytes(self):
        """backlog_server.py: NOTE_MAX is checked with len() on the text, so 2,000 two-byte characters fit."""
        r = self.srv.put(self.path, {"appid": 1, "note": "é" * 2000})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.srv.get(self.path).json()["1"], "é" * 2000)

    def test_the_length_check_looks_at_the_stripped_text(self):
        """backlog_server.py: the note is stripped before the limit applies."""
        r = self.srv.put(self.path, {"appid": 1, "note": "  " + "x" * 2000 + "  "})
        self.assertEqual(r.status, 200)

    def test_unicode_round_trips(self):
        """backlog_server.py writes notes with ensure_ascii=False."""
        text = "naïve – café ☕ 日本語"
        self.srv.put(self.path, {"appid": 9, "note": text})
        self.assertEqual(self.srv.get(self.path).json(), {"9": text})
        self.assertEqual(json.loads((self.srv.data / "notes.json").read_text("utf-8")), {"9": text})

    def test_bad_requests_are_400(self):
        """backlog_server.py: {appid: int, note: string}; a non-object body is "expected a JSON object"."""
        for body, message in [([1], "expected a JSON object"), ({"appid": "1", "note": "x"}, None),
                              ({"appid": True, "note": "x"}, None), ({"appid": 1, "note": 5}, None),
                              ({"appid": 1, "note": None}, None), ({"note": "x"}, None)]:
            with self.subTest(body=body):
                r = self.srv.put(self.path, json.dumps(body).encode(), {"Content-Type": "application/json"})
                self.assertEqual(r.status, 400)
                self.assertEqual(r.json()["error"], message or "expected {appid: int, note: string}")

    def test_validation_beats_a_stale_if_match_even_for_the_length_limit(self):
        """Contract §3.4: "PUT /api/notes If-Match: "deadbeef..." with a 2,001-character note -> 400 Bad Request
        {"error": "notes are limited to 2000 characters"}"."""
        r = self.srv.put(self.path, {"appid": 620, "note": "x" * 2001}, {"If-Match": '"' + "deadbeef" * 4 + '"'})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.json(), {"error": "notes are limited to 2000 characters"})

    def test_if_match_covers_the_whole_map(self):
        """Contract §3.1 note on granularity applies to notes as well."""
        tag = self.srv.get(self.path).headers["etag"]
        self.srv.put(self.path, {"appid": 1, "note": "a"})
        self.assertEqual(self.srv.put(self.path, {"appid": 2, "note": "b"}, {"If-Match": tag}).status, 412)


class Order(SmallResource, Base):
    path = "/api/order"
    empty = []
    first = {"order": [620, 1145360, 753640]}
    other = {"order": [753640, 620, 1145360]}

    def test_put_returns_count_the_stored_order_and_the_previous_one(self):
        """Contract §3.2: 'PUT /api/order -> {"ok": true, "count": 3, "order": [..], "previous": [..]}'
        and §3.4: the move from [620, 1145360, 753640] to [753640, 620, 1145360]."""
        self.assertEqual(self.srv.put(self.path, self.first).json(),
                         {"ok": True, "count": 3, "order": [620, 1145360, 753640], "previous": []})
        r = self.srv.put(self.path, self.other)
        self.assertEqual(r.json(), {"ok": True, "count": 3, "order": [753640, 620, 1145360],
                                    "previous": [620, 1145360, 753640]})
        self.assertEqual(self.srv.get(self.path).json(), [753640, 620, 1145360])

    def test_duplicates_are_dropped_not_rejected(self):
        """Contract §3.2: "order is the de-duplicated list actually stored; previous is the list before the
        write". README: "a list of distinct app ids". The first occurrence of each id wins."""
        r = self.srv.put(self.path, {"order": [3, 1, 3, 2, 1]})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json()["order"], [3, 1, 2])
        self.assertEqual(r.json()["count"], 3)
        self.assertEqual(self.srv.get(self.path).json(), [3, 1, 2])

    def test_an_empty_list_clears_the_order(self):
        """backlog_server.py: any list of app ids is valid, including []."""
        self.srv.put(self.path, self.first)
        r = self.srv.put(self.path, {"order": []})
        self.assertEqual(r.json(), {"ok": True, "count": 0, "order": [], "previous": [620, 1145360, 753640]})

    def test_non_integer_entries_and_wrong_shapes_are_400(self):
        """README, API table: PUT {"order": [620, 1145360]} (distinct app ids). backlog_server.py: entries must
        be ints (a boolean is not an app id)."""
        expect = "expected {order: [app ids, up to 5000]}"
        for body in ({"order": ["620"]}, {"order": [1.5]}, {"order": [True]}, {"order": [None]},
                     {"order": [[1]]}, {"order": [620, "x"]}, {"order": "620"}, {"order": 620},
                     {"order": {"a": 1}}, {"orders": [1]}, {}, [620, 1145360], "x"):
            with self.subTest(body=body):
                r = self.srv.put(self.path, json.dumps(body).encode(), {"Content-Type": "application/json"})
                self.assertEqual(r.status, 400)
                self.assertEqual(r.json(), {"error": expect})
        self.assertEqual(self.srv.get(self.path).json(), [], "no bad request may write anything")

    def test_at_most_five_thousand_ids(self):
        """backlog_server.py: ORDER_MAX is 5000, named in the 400 message."""
        self.assertEqual(self.srv.put(self.path, {"order": list(range(5000))}).status, 200)
        r = self.srv.put(self.path, {"order": list(range(5001))})
        self.assertEqual(r.status, 400)
        self.assertIn("5000", r.json()["error"])

    def test_file_on_disk_is_the_deduplicated_compact_list(self):
        """README: "Both are saved on the server in notes.json and order.json"."""
        self.srv.put(self.path, {"order": [5, 5, 4]})
        self.assertEqual((self.srv.data / "order.json").read_text(), "[5,4]")

    def test_the_documented_read_modify_write_cycle(self):
        """Contract §3.4, "Order, read-modify-write with a precondition": GET, then PUT with If-Match; if the page
        reordered in between the PUT is 412 and carries the fresh ETag."""
        self.srv.put(self.path, self.first)
        got = self.srv.get(self.path)
        tag = got.headers["etag"]
        self.assertEqual(got.json(), [620, 1145360, 753640])
        self.srv.put(self.path, {"order": [1145360, 620, 753640]})  # the page, in between
        stale = self.srv.put(self.path, self.other, {"If-Match": tag})
        self.assertEqual(stale.status, 412)
        fresh = self.srv.get(self.path).headers["etag"]
        self.assertEqual(stale.json()["etag"], fresh)
        ok = self.srv.put(self.path, self.other, {"If-Match": fresh})
        self.assertEqual(ok.status, 200)
        self.assertEqual(ok.json()["previous"], [1145360, 620, 753640])


# ====================================================================== the README's route table

class ReadmeRouteTable(unittest.TestCase):
    def routes(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        section = text.split("\n## API\n", 1)[1].split("\n## ", 1)[0]
        out = []
        for m in re.finditer(r"^\|\s*([A-Z /]+?)\s*\|\s*`([^`]+)`", section, re.M):
            for method in (x.strip() for x in m.group(1).split("/")):
                out.append((method, m.group(2)))
        return out

    def test_every_documented_route_exists_and_answers(self):
        """README, API table: each (method, path) row is a route the server really has. Fails if the README
        documents a route the server doesn't serve (404/405/501), and also if the table can't be parsed."""
        routes = self.routes()
        self.assertGreaterEqual(len(routes), 11, f"README API table not parsed: {routes}")
        self.assertIn(("POST", "/api/export"), routes)
        self.assertIn(("GET", "/api/export/status"), routes)
        lib = json.dumps({"games": [{"appid": 1, "name": "A"}]}).encode()
        bodies = {("PUT", "/api/library"): lib, ("PUT", "/api/triage"): {"appid": 1, "lane": "next"},
                  ("PUT", "/api/notes"): {"appid": 1, "note": "n"}, ("PUT", "/api/order"): {"order": [1]},
                  ("POST", "/api/export"): {}}
        with Server() as s:
            for method, path in routes:
                with self.subTest(route=f"{method} {path}"):
                    r = s.req(method, path, bodies.get((method, path)))
                    self.assertNotIn(r.status, (404, 405, 501), f"{method} {path} -> {r.status}")
                    self.assertLess(r.status, 500)

    def test_the_readme_table_has_no_route_this_suite_forgot(self):
        """Keeps this file honest: the routes it has dedicated tests for match the README table."""
        covered = {("GET", "/"), ("GET", "/stats"), ("GET", "/api/library"), ("PUT", "/api/library"), ("GET", "/api/triage"),
                   ("PUT", "/api/triage"), ("GET", "/api/notes"), ("PUT", "/api/notes"), ("GET", "/api/order"),
                   ("PUT", "/api/order"), ("GET", "/healthz"),
                   ("GET", "/api/prefs"), ("PUT", "/api/prefs"),  # test_prefs.py
                   ("POST", "/api/export"), ("GET", "/api/export/status"),  # these two: test_export_endpoints.py
                   ("GET", "/api/run-status"),  # test_hardening.py
                   ("GET", "/api/history")}  # test_history.py
        self.assertEqual(set(self.routes()), covered)


if __name__ == "__main__":
    unittest.main()
