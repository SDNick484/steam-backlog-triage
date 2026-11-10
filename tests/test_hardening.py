"""Ops hardening: the same-origin guard on mutating routes, headers on every response, the run status file
and the deep health check. Standard library only.

    python3 -m unittest -v tests.test_hardening
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ask  # noqa: E402
from _support import ROOT, MockSteam, Server, fake_steamid, load_fixture_library, run_exporter  # noqa: E402
import backlog_server  # noqa: E402


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


# ====================================================================== H4: run_status.json (the exporter writes it)

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
STATUS_KEYS = {"version", "last_attempt", "last_ok", "consecutive_failures", "error_class"}
FAKE_KEY = "FAKEKEY0123456789"


class ExporterRunStatus(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.mock = MockSteam(games=[{"appid": 4000 + i, "name": f"Status Game {i:02d}", "playtime_forever": 0}
                                     for i in range(12)])
        self.mock.__enter__()
        self.addCleanup(self.mock.__exit__, None, None, None)

    def run_ex(self, *args, key=FAKE_KEY, steamid=None, env=None):
        argv = list(args) + ["--steamid", steamid or fake_steamid()]
        if key:
            argv += ["--key", key]
        return run_exporter(self.dir, argv, self.mock, extra_env=env)

    def status(self) -> dict:
        return json.loads((self.dir / "run_status.json").read_text())

    def test_success_shape(self):
        p = self.run_ex()
        self.assertEqual(p.returncode, 0, p.stderr)
        st = self.status()
        self.assertEqual(set(st), STATUS_KEYS)
        self.assertEqual(st["version"], 1)
        self.assertRegex(st["last_attempt"], STAMP)
        self.assertEqual(st["last_ok"], st["last_attempt"])
        self.assertEqual((st["consecutive_failures"], st["error_class"]), (0, None))
        self.assertEqual([f.name for f in self.dir.glob("run_status.json*")], ["run_status.json"], "no tmp left")

    def test_steam_failure_class_and_nonzero_exit_kept(self):
        p = self.run_ex(steamid="not-a-steamid")
        self.assertNotEqual(p.returncode, 0)
        st = self.status()
        self.assertEqual((st["error_class"], st["consecutive_failures"], st["last_ok"]), ("steam", 1, None))

    def test_missing_key_is_other(self):
        p = self.run_ex(key=None)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.status()["error_class"], "other")

    def test_failure_keeps_last_ok_and_counts_then_success_resets(self):
        self.assertEqual(self.run_ex().returncode, 0)
        first = self.status()
        self.run_ex(steamid="bad")
        self.run_ex(steamid="bad")
        st = self.status()
        self.assertEqual(st["last_ok"], first["last_ok"])
        self.assertEqual(st["consecutive_failures"], 2)
        self.assertEqual(st["error_class"], "steam")
        self.assertEqual(self.run_ex().returncode, 0)
        st = self.status()
        self.assertEqual((st["consecutive_failures"], st["error_class"]), (0, None))
        self.assertEqual(st["last_ok"], st["last_attempt"])

    def test_corrupt_previous_file_is_ignored(self):
        (self.dir / "run_status.json").write_text("{not json")
        self.run_ex(steamid="bad")
        st = self.status()
        self.assertEqual((st["consecutive_failures"], st["last_ok"]), (1, None))

    def test_a_given_up_reviews_step_is_class_details(self):
        for g in self.mock.games:
            self.mock.reviews[g["appid"]] = [(200, b"<html>slow down</html>", {})]
        p = self.run_ex("--details")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Stopping the reviews step", p.stderr)
        st = self.status()
        self.assertEqual((st["error_class"], st["consecutive_failures"], st["last_ok"]), ("details", 1, None))
        self.assertTrue((self.dir / "steam_backlog.json").exists(), "the library is still written")

    def test_a_failing_hltb_step_is_class_hltb(self):
        fake = self.dir / "fakehltb"
        fake.mkdir()
        (fake / "howlongtobeatpy.py").write_text(
            "class HowLongToBeat:\n    def search(self, *a, **k):\n        raise RuntimeError('down')\n")
        p = self.run_ex(env={"PYTHONPATH": str(fake)})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("HLTB keeps failing", p.stderr)
        self.assertEqual(self.status()["error_class"], "hltb")

    def test_unwritable_data_dir_does_not_fail_the_run(self):
        out = self.dir / "sub"
        out.mkdir()
        (out / "run_status.json").mkdir()  # os.replace onto a directory fails
        p = run_exporter(self.dir, ["--steamid", fake_steamid(), "--key", FAKE_KEY, "--out", str(out / "lib.json")],
                         self.mock)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Could not write run_status.json", p.stderr)


class RunExportScriptWritesStatus(unittest.TestCase):
    """The common path: run-export.sh (what cron and Refresh both run) ends with run_status.json written."""

    @unittest.skipUnless(shutil.which("flock"), "flock is needed")
    def test_through_run_export_sh(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            app = root / "app"
            app.mkdir()
            shutil.copy(ROOT / "run-export.sh", app / "run-export.sh")
            shutil.copy(ROOT / "steam_backlog_export.py", app / "steam_backlog_export.py")
            env_file = root / "env"
            env_file.write_text(f"STEAM_API_KEY='{FAKE_KEY}'\nSTEAM_PROFILE='{fake_steamid()}'\n")
            out = root / "exports"
            with MockSteam() as mock:
                from _support import exporter_env
                env = exporter_env(mock, {"HOME": str(root), "STEAM_ENV_FILE": str(env_file), "EXPORT_DIR": str(out)})
                p = subprocess.run(["sh", str(app / "run-export.sh")], env=env, capture_output=True, text=True,
                                   timeout=120)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            st = json.loads((out / "run_status.json").read_text())
            self.assertEqual((st["consecutive_failures"], st["error_class"]), (0, None))


# ====================================================================== H4: the server side

def stamp(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kw) -> str:
    return stamp(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**kw))


class RunStatusRoute(Base):
    def write(self, obj) -> None:
        (self.srv.data / "run_status.json").write_text(obj if isinstance(obj, str) else json.dumps(obj))

    def test_null_before_the_first_run(self):
        r = self.srv.get("/api/run-status")
        self.assertEqual((r.status, r.json()), (200, None))
        self.assertEqual(r.headers["x-backlog-api"], "2")

    def test_serves_the_file(self):
        st = {"version": 1, "last_attempt": ago(hours=1), "last_ok": ago(days=3), "consecutive_failures": 2,
              "error_class": "hltb"}
        self.write(st)
        self.assertEqual(self.srv.get("/api/run-status").json(), st)

    def test_malformed_files_read_as_null(self):
        good = {"version": 1, "last_attempt": ago(hours=1), "last_ok": None, "consecutive_failures": 0,
                "error_class": None}
        for label, bad in {"not json": "{", "list": [], "version 2": {**good, "version": 2},
                           "bad class": {**good, "error_class": "Traceback (most recent call last)"},
                           "negative": {**good, "consecutive_failures": -1},
                           "bool count": {**good, "consecutive_failures": True},
                           "no attempt": {k: v for k, v in good.items() if k != "last_attempt"}}.items():
            with self.subTest(label):
                self.write(bad)
                self.assertIsNone(self.srv.get("/api/run-status").json())

    def test_export_status_object_is_unchanged(self):
        self.write({"version": 1, "last_attempt": ago(hours=1), "last_ok": ago(hours=1), "consecutive_failures": 0,
                    "error_class": None})
        self.assertNotIn("runStatus", self.srv.get("/api/export/status").json())


class DeepHealth(Base):
    def deep(self):
        r = self.srv.get("/healthz?deep=1")
        self.assertTrue(r.headers["content-type"].startswith("application/json"), r.headers)
        self.assertEqual(r.headers["cache-control"], "no-store")
        return r.status, r.json()

    def write(self, **kw):
        st = {"version": 1, "last_attempt": ago(hours=1), "last_ok": ago(hours=1), "consecutive_failures": 0,
              "error_class": None}
        st.update(kw)
        (self.srv.data / "run_status.json").write_text(json.dumps(st))

    def test_plain_healthz_is_unchanged(self):
        r = self.srv.get("/healthz")
        self.assertEqual((r.status, r.body, r.headers["content-type"]), (200, b"ok", "text/plain"))
        self.assertEqual(self.srv.get("/healthz?deep=0").body, b"ok")

    def test_healthy(self):
        self.write()
        code, b = self.deep()
        self.assertEqual(code, 200)
        self.assertEqual(set(b), {"ok", "build", "last_ok", "age_seconds", "consecutive_failures"})
        self.assertIs(b["ok"], True)
        self.assertEqual(b["consecutive_failures"], 0)
        self.assertTrue(3500 <= b["age_seconds"] <= 3700, b)
        self.assertEqual(b["build"], "unknown", "a plain checkout has no build stamp")

    def test_last_failure_is_503_even_if_recent(self):
        self.write(consecutive_failures=1, error_class="steam")
        code, b = self.deep()
        self.assertEqual((code, b["ok"], b["consecutive_failures"]), (503, False, 1))

    def test_stale_is_503(self):
        self.write(last_ok=ago(days=11), last_attempt=ago(hours=1))
        code, b = self.deep()
        self.assertEqual((code, b["ok"]), (503, False))
        self.assertGreater(b["age_seconds"], 10 * 86400)

    def test_ten_days_is_still_fine_eleven_is_not(self):
        self.write(last_ok=ago(days=9, hours=23))
        self.assertEqual(self.deep()[0], 200)

    def test_never_succeeded_is_503_with_nulls(self):
        self.write(last_ok=None, consecutive_failures=3, error_class="other")
        code, b = self.deep()
        self.assertEqual((code, b["ok"], b["last_ok"], b["age_seconds"]), (503, False, None, None))

    def test_missing_file_falls_back_to_the_library_mtime(self):
        self.assertEqual(self.srv.get("/api/run-status").json(), None)
        code, b = self.deep()  # the library was just seeded
        self.assertEqual((code, b["ok"], b["consecutive_failures"]), (200, True, 0))
        self.assertRegex(b["last_ok"], STAMP)
        self.assertLess(b["age_seconds"], 60)
        old = datetime.datetime.now().timestamp() - 12 * 86400
        os.utime(self.srv.data / "steam_backlog.json", (old, old))
        code, b = self.deep()
        self.assertEqual((code, b["ok"]), (503, False))
        self.assertGreater(b["age_seconds"], 11 * 86400)

    def test_nothing_at_all(self):
        (self.srv.data / "steam_backlog.json").unlink()
        code, b = self.deep()
        self.assertEqual((code, b["ok"], b["last_ok"], b["age_seconds"], b["consecutive_failures"]),
                         (503, False, None, None, 0))

    def test_head_works(self):
        self.write()
        self.assertEqual(self.srv.req("HEAD", "/healthz?deep=1").status, 200)


# ====================================================================== H5: build id

class BuildId(unittest.TestCase):
    def test_unexpanded_placeholder_is_unknown(self):
        self.assertTrue(backlog_server.BUILD_STAMP.startswith("$Format"))
        self.assertEqual(backlog_server.build_id(), "unknown")

    def test_reads_a_stamped_value(self):
        for stamped, want in (("abc1234", "abc1234"), ("$Format:%h$", "unknown"), ("", "unknown"),
                              ("not a hash", "unknown"), ("ABC1234", "unknown")):
            with self.subTest(stamped):
                old = backlog_server.BUILD_STAMP
                backlog_server.BUILD_STAMP = stamped
                try:
                    self.assertEqual(backlog_server.build_id(), want)
                finally:
                    backlog_server.BUILD_STAMP = old

    @unittest.skipUnless(shutil.which("git"), "git is needed")
    def test_git_archive_substitutes_and_the_code_reads_it(self):
        attrs = (ROOT / ".gitattributes").read_text()
        self.assertRegex(attrs, r"(?m)^backlog_server\.py\s+export-subst$")
        with tempfile.TemporaryDirectory() as t:
            repo, out = Path(t) / "repo", Path(t) / "out"
            repo.mkdir()
            out.mkdir()
            g = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run([*g, "init", "-q"], check=True)
            for f in ("backlog_server.py", "ask.py", "backlog_query.py", "access_jwt.py", "backlog.html"):
                shutil.copy(ROOT / f, repo / f)
            shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
            subprocess.run([*g, "add", "-A"], check=True)
            subprocess.run([*g, "commit", "-q", "-m", "x"], check=True)
            short = subprocess.run([*g, "rev-parse", "--short", "HEAD"], check=True, capture_output=True,
                                   text=True).stdout.strip()
            tar = Path(t) / "a.tar"
            subprocess.run([*g, "archive", "-o", str(tar), "HEAD"], check=True)
            subprocess.run(["tar", "-xf", str(tar), "-C", str(out)], check=True)
            self.assertIn(f'BUILD_STAMP = "{short}"', (out / "backlog_server.py").read_text())
            r = subprocess.run([sys.executable, "-c", "import backlog_server as b; print(b.build_id())"],
                               cwd=out, capture_output=True, text=True, check=True)
            self.assertEqual(r.stdout.strip(), short)
            # The checkout's own file is never touched, and the other files are not substituted.
            self.assertTrue(backlog_server.BUILD_STAMP.startswith("$Format"))

    def test_the_stamp_line_is_the_one_the_installer_replaces(self):
        text = (ROOT / "backlog_server.py").read_text()
        self.assertEqual(len(re.findall(r'(?m)^BUILD_STAMP = "\$Format:%h\$"$', text)), 1)


if __name__ == "__main__":
    unittest.main()
