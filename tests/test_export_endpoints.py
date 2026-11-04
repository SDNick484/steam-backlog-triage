"""POST /api/export and GET /api/export/status, with run-export.sh replaced by stub scripts.

The server finds its export command from --export-cmd (contract §4.1), so a stub is passed the same way
the real script would be. Stubs wait on marker files instead of sleeping, so no test depends on timing.

Expected behavior: docs/backlog-api-contract.md §4 (and §5.2 for the exit codes) in
SDNick484/mcp-server-steam-backlog.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import (  # noqa: E402
    FIXTURES, Server, fake_steamid, load_fixture_library, raw_request, wait_for, write_stub,
)

STATUS_KEYS = {"state", "origin", "id", "startedAt", "finishedAt", "elapsedSeconds", "phase", "progress",
               "exitCode", "reason", "lines", "libraryEtag", "libraryChanged", "exportedAt"}
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

# What run-export.sh prints first, then a library replaced the way the exporter does it (write beside it,
# rename over it). $STUB_NEW_LIBRARY is a file set in the server's environment.
SUCCEED_AND_REPLACE = r'''
set -eu
D="${EXPORT_DIR:?}"
echo "$(date '+%F %T') starting export"
echo "Test Persona: 3 games"
echo "Steam Deck compatibility: fetching 3 games..."
echo "  3/3"
cp "$STUB_NEW_LIBRARY" "$D/.stub.tmp"
mv "$D/.stub.tmp" "$D/steam_backlog.json"
echo "Wrote $D/steam_backlog.json: 3 games, 1 never launched, 0 with HLTB times."
'''

# Blocks until the test creates $EXPORT_DIR/.release; tells the test it is running via .started.
WAIT_FOR_RELEASE = r'''
set -eu
D="${EXPORT_DIR:?}"
echo "$(date '+%F %T') starting export"
: > "$D/.started"
while [ ! -e "$D/.release" ]; do sleep 0.05; done
echo "released"
'''


class Base(unittest.TestCase):
    seed = True
    server_args: list[str] = []

    def make(self, **kw) -> Server:
        """A server with a no-op export command (for tests that never start a run)."""
        env = {"STUB_NEW_LIBRARY": str(self.new_library), **kw.pop("env", {})}
        s = Server(seed=kw.pop("seed", self.seed), env=env, export_cmd="noop", extra=self.server_args, **kw)
        self.addCleanup(s.cleanup)
        return s.start()

    def setUp(self):
        lib = load_fixture_library()
        lib["exportedAt"] = "2031-02-03T04:05:06+00:00"
        lib["games"].append({"appid": 1999, "name": "Test Game Zulu", "playtime": 0, "deckPlaytime": 0,
                             "lastPlayed": 0, "deck": 3, "hltb": None, "genres": [], "metacritic": None,
                             "year": None, "controller": None, "proton": None})
        import tempfile
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.new_library = Path(self._d.name) / "new-library.json"
        self.new_library.write_text(json.dumps(lib))
        self.new_exported_at = lib["exportedAt"]

    # ---- helpers
    def server_with(self, body: str, **kw) -> Server:
        """A server whose --export-cmd is a stub with this /bin/sh body."""
        s = Server(seed=kw.pop("seed", self.seed), env={"STUB_NEW_LIBRARY": str(self.new_library),
                                                        **kw.pop("env", {})},
                   export_cmd=None, extra=[], isolated=False, **kw)
        stub = s.stub("export-stub.sh", body)
        s.cmd += ["--export-cmd", str(stub)] + list(self.server_args)
        self.addCleanup(s.cleanup)
        return s.start()

    @staticmethod
    def status(s: Server) -> dict:
        r = s.get("/api/export/status")
        assert r.status == 200, (r.status, r.text())
        return r.json()

    def finished(self, s: Server, timeout: float = 15.0) -> dict:
        return wait_for(lambda: (lambda st: st if st["state"] != "running" else None)(self.status(s)), timeout,
                        what="the export to finish")

    @staticmethod
    def release(s: Server) -> None:
        (s.data / ".release").write_text("")

    @staticmethod
    def started(s: Server) -> None:
        wait_for(lambda: (s.data / ".started").exists(), 10, what="the stub to start")


# ====================================================================== idle, shape, start

class StatusObject(Base):
    def test_idle_status_before_anything_ran(self):
        """Contract §4.6: `idle`: nothing has run since the server started and no external run is going;
        origin is null when idle; §4.5: always 200 with the status object."""
        s = self.make()
        st = self.status(s)
        self.assertEqual(set(st), STATUS_KEYS)
        self.assertEqual(st["state"], "idle")
        for k in ("origin", "id", "startedAt", "finishedAt", "elapsedSeconds", "phase", "progress", "exitCode",
                  "reason", "libraryChanged"):
            self.assertIsNone(st[k], k)
        self.assertEqual(st["lines"], [])
        self.assertEqual(st["libraryEtag"], s.get("/api/library").headers["etag"])
        self.assertEqual(st["exportedAt"], load_fixture_library()["exportedAt"])

    def test_status_with_no_library_yet(self):
        """Contract §4.6: libraryEtag is "the library's current ETag ... or null if there's no export yet"."""
        s = self.make(seed=False)
        st = self.status(s)
        self.assertIsNone(st["libraryEtag"])
        self.assertIsNone(st["exportedAt"])
        self.assertEqual(st["state"], "idle")

    def test_status_carries_api_headers_and_is_never_404(self):
        """Contract §4.5: "It never returns 404 on an API-2 server. A 404 here means API 1."""
        r = self.make().get("/api/export/status")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["x-backlog-api"], "2")


class StartingARun(Base):
    def test_post_returns_202_with_a_running_status_and_a_location(self):
        """Contract §4.4: "started: 202 Accepted: the status object (§4.6), state: "running", origin: "server";
        plus header Location: /api/export/status"; §4.6: id is the UTC start time YYYYMMDDTHHMMSSZ."""
        s = self.server_with(WAIT_FOR_RELEASE)
        r = s.post("/api/export", {})
        self.assertEqual(r.status, 202)
        self.assertEqual(r.headers["location"], "/api/export/status")
        st = r.json()
        self.assertEqual(set(st), STATUS_KEYS)
        self.assertEqual((st["state"], st["origin"]), ("running", "server"))
        self.assertRegex(st["id"], r"^\d{8}T\d{6}Z$")
        self.assertRegex(st["startedAt"], ISO)
        self.assertIsNone(st["finishedAt"])
        self.assertIsInstance(st["elapsedSeconds"], int)
        self.assertIsNone(st["exitCode"])
        self.assertIsNone(st["reason"])
        self.assertIsNone(st["libraryChanged"])
        self.release(s)

    def test_a_second_post_while_running_is_409_with_that_runs_status(self):
        """Contract §4.4: "a run is already going (either origin): 409 Conflict: the status object of that run,
        plus "error": "export already running"." §4.3: spawning "happens under a server mutex so two POSTs can't
        both start a child"."""
        s = self.server_with(WAIT_FOR_RELEASE)
        first = s.post("/api/export", {}).json()
        self.started(s)
        r = s.post("/api/export", {})
        self.assertEqual(r.status, 409)
        body = r.json()
        self.assertEqual(body["error"], "export already running")
        self.assertEqual((body["state"], body["origin"], body["id"]), ("running", "server", first["id"]))
        self.assertEqual(self.status(s)["state"], "running")
        self.release(s)
        self.assertEqual(self.finished(s)["state"], "succeeded")
        self.assertEqual(s.post("/api/export", {}).status, 202, "once it finished, a new run may start")
        self.finished(s)

    def test_parallel_posts_start_exactly_one_child(self):
        """Contract §4.3: two POSTs can't both start a child."""
        import threading
        s = self.server_with(WAIT_FOR_RELEASE.replace(': > "$D/.started"', 'echo x >> "$D/.starts"'))
        codes = []
        threads = [threading.Thread(target=lambda: codes.append(s.post("/api/export", {}).status)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(codes), [202] + [409] * 7)
        wait_for(lambda: (s.data / ".starts").exists(), 10, what="the child")
        self.release(s)
        self.finished(s)
        self.assertEqual((s.data / ".starts").read_text().count("x"), 1, "the stub ran once")

    def test_request_body_forms(self):
        """Contract §4.4: "Request: Content-Type: application/json is required, with an empty body or {}. ... Any
        JSON object body is accepted and ignored (reserved for future options)"."""
        s = self.make()
        for name, body, headers in [
            ("empty body", None, {"Content-Type": "application/json"}),
            ("{}", b"{}", {"Content-Type": "application/json"}),
            ("an object", b'{"future": true}', {"Content-Type": "application/json"}),
            ("charset parameter", b"{}", {"Content-Type": "application/json; charset=utf-8"}),
            ("upper case", b"{}", {"Content-Type": "Application/JSON"}),
        ]:
            with self.subTest(name):
                r = s.post("/api/export", body, headers)
                self.assertEqual(r.status, 202, r.text())
                self.finished(s)

    def test_wrong_or_missing_content_type_is_415(self):
        """Contract §4.4: "Any other content type gets 415 {"error": "expected Content-Type: application/json"}.
        That keeps a plain cross-site HTML form from starting an export"; §4.9 shows form-urlencoded."""
        s = self.make()
        for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x",
                      "application/jsonp", None):
            with self.subTest(ctype=ctype):
                r = s.post("/api/export", b"{}", {"Content-Type": ctype} if ctype else {})
                self.assertEqual(r.status, 415)
                self.assertEqual(r.json(), {"error": "expected Content-Type: application/json"})
        self.assertEqual(self.status(s)["state"], "idle", "a refused request must not start anything")

    def test_malformed_bodies_are_400(self):
        """Contract §4.4 plus backlog_server.py: a body that isn't a JSON object, or is oversized, is a 400."""
        s = self.make()
        for body in (b"[1]", b'"x"', b"123", b"null", b"{nope"):
            with self.subTest(body=body):
                r = s.post("/api/export", body, {"Content-Type": "application/json"})
                self.assertEqual(r.status, 400)
                self.assertEqual(r.json(), {"error": "expected a JSON object"})
        head = (b"POST /api/export HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 70000\r\n\r\n")
        self.assertTrue(raw_request(s.port, head).startswith(b"HTTP/1.0 400"))
        self.assertEqual(self.status(s)["state"], "idle")

    def test_no_export_command_configured_is_501(self):
        """Contract §4.1: "If no export command is configured (no option and no run-export.sh beside the server),
        both export routes answer 501 {"error": "export not configured on this server"}"."""
        s = Server(isolated=True, export_cmd=None)
        self.addCleanup(s.cleanup)
        s.start()
        for r in (s.get("/api/export/status"), s.post("/api/export", {})):
            self.assertEqual(r.status, 501)
            self.assertEqual(r.json(), {"error": "export not configured on this server"})
            self.assertEqual(r.headers["x-backlog-api"], "2")

    def test_a_command_that_cannot_be_executed_is_a_generic_500(self):
        """Contract §4.4: "spawn failed (exec error): 500 {"error": "could not start the export"}; detail goes
        to the server's stderr only". §7: no paths in messages."""
        s = self.server_with("")
        stub = s.data.parent / "export-stub.sh"
        stub.write_text("#!/nonexistent/interpreter\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        r = s.post("/api/export", {})
        self.assertEqual(r.status, 500)
        self.assertEqual(r.json(), {"error": "could not start the export"})
        self.assertNotIn("nonexistent", r.text())
        self.assertIn("could not start", s.log_text(), "the detail goes to the server's stderr")
        self.assertEqual(self.status(s)["state"], "idle")


# ====================================================================== finishing

class Finishing(Base):
    def test_success_replaces_the_library_and_reports_it(self):
        """Contract §4.6: `succeeded` "only for exit code 0"; libraryChanged "whether the library ETag differs from
        the one at startedAt"; §4.9 "Finished, library changed" example."""
        s = self.server_with(SUCCEED_AND_REPLACE)
        before = s.get("/api/library").headers["etag"]
        self.assertEqual(s.post("/api/export", {}).status, 202)
        st = self.finished(s)
        after = s.get("/api/library").headers["etag"]
        self.assertNotEqual(before, after)
        self.assertEqual(st["state"], "succeeded")
        self.assertEqual((st["origin"], st["exitCode"], st["reason"]), ("server", 0, None))
        self.assertRegex(st["finishedAt"], ISO)
        self.assertGreaterEqual(st["elapsedSeconds"], 0)
        self.assertIs(st["libraryChanged"], True)
        self.assertEqual(st["libraryEtag"], after)
        self.assertEqual(st["exportedAt"], self.new_exported_at)
        self.assertEqual(st["phase"], "writing")
        self.assertEqual(st["lines"][0].split(" ", 2)[2], "starting export")
        self.assertIn("Wrote <data>/steam_backlog.json: 3 games, 1 never launched, 0 with HLTB times.", st["lines"])

    def test_a_run_that_leaves_the_library_alone_reports_unchanged(self):
        """Contract §4.6: libraryChanged is false when the ETag is the same at the end."""
        s = self.server_with('echo "nothing new"\n')
        s.post("/api/export", {})
        st = self.finished(s)
        self.assertEqual(st["state"], "succeeded")
        self.assertIs(st["libraryChanged"], False)

    def test_the_first_ever_export_counts_as_a_change(self):
        """Contract §4.6: "libraryEtag: ... null if there's no export yet"; the first library makes it differ."""
        s = self.server_with(SUCCEED_AND_REPLACE, seed=False)
        self.assertIsNone(self.status(s)["libraryEtag"])
        s.post("/api/export", {})
        st = self.finished(s)
        self.assertIs(st["libraryChanged"], True)
        self.assertEqual(st["libraryEtag"], s.get("/api/library").headers["etag"])

    def test_exit_codes_map_to_reasons(self):
        """Contract §4.6 `reason` table and §5.2: 78 -> "not_configured", 75 -> "busy", any other non-zero ->
        "error"; "Negative = killed by that signal number"; success "only for exit code 0"."""
        cases = [("exit 0", "succeeded", 0, None), ("exit 1", "failed", 1, "error"),
                 ("exit 3", "failed", 3, "error"), ("exit 75", "failed", 75, "busy"),
                 ("exit 78", "failed", 78, "not_configured"), ("kill -9 $$", "failed", -9, "error")]
        for body, state, code, reason in cases:
            with self.subTest(body):
                s = self.server_with(body + "\n")
                self.assertEqual(s.post("/api/export", {}).status, 202)
                st = self.finished(s)
                self.assertEqual((st["state"], st["exitCode"], st["reason"]), (state, code, reason))

    def test_missing_keys_example(self):
        """Contract §4.9, "Keys not set": exit 78, reason not_configured, and the skipped line with the data path
        redacted to ~ (the real script prints it with the user's home)."""
        s = self.server_with('echo "$(date \'+%F %T\') skipped: set STEAM_API_KEY and STEAM_PROFILE in '
                             '/home/steam/.steam-env"\nexit 78\n')
        s.post("/api/export", {})
        st = self.finished(s)
        self.assertEqual((st["state"], st["exitCode"], st["reason"]), ("failed", 78, "not_configured"))
        self.assertTrue(st["lines"][0].endswith("skipped: set STEAM_API_KEY and STEAM_PROFILE in ~/.steam-env"))

    def test_the_status_is_kept_until_the_next_run_starts(self):
        """Contract §4.6: "After a run ends, the server keeps reporting it (succeeded/failed) until the next one
        starts"."""
        s = self.server_with("exit 3\n")
        s.post("/api/export", {})
        first = self.finished(s)
        for _ in range(3):
            self.assertEqual(self.status(s)["id"], first["id"])
            self.assertEqual(self.status(s)["state"], "failed")

    def test_timeout_stops_the_run_and_calls_it_a_timeout(self):
        """Contract §4.2: on timeout "the server sends SIGINT (the exporter catches it ...)"; §4.6: reason
        "timeout" - "the server stopped it at --export-timeout"; exitCode is negative when a signal killed it."""
        self.server_args = ["--export-timeout", "1"]
        s = self.server_with("exec sleep 60\n")
        s.post("/api/export", {})
        st = self.finished(s, timeout=20)
        self.assertEqual(st["state"], "failed")
        self.assertEqual(st["reason"], "timeout")
        self.assertLess(st["exitCode"], 0, "killed by a signal")
        self.assertLess(st["elapsedSeconds"], 15)


# ====================================================================== output, phases, redaction

class Output(Base):
    def test_the_status_carries_the_last_twenty_lines(self):
        """Contract §4.6: lines are "the last 20 output lines of a server run"."""
        s = self.server_with('i=1; while [ $i -le 50 ]; do echo "line $i"; i=$((i+1)); done\n')
        s.post("/api/export", {})
        st = self.finished(s)
        self.assertEqual(st["lines"], [f"line {n}" for n in range(31, 51)])

    def test_blank_lines_are_dropped(self):
        """backlog_server.py _read(): lines that are empty after stripping are skipped."""
        s = self.server_with('echo a; echo; echo "   "; echo b\n')
        s.post("/api/export", {})
        self.assertEqual(self.finished(s)["lines"], ["a", "b"])

    def test_stderr_is_merged_into_the_same_stream(self):
        """Contract §4.2: "stdin /dev/null and stdout+stderr merged into one pipe"."""
        s = self.server_with('echo out; echo err >&2; cat > /dev/null && echo stdin-closed\n')
        s.post("/api/export", {})
        lines = self.finished(s)["lines"]
        self.assertEqual(sorted(lines), ["err", "out", "stdin-closed"])

    def test_the_child_gets_export_dir_and_no_keys(self):
        """Contract §4.2: the environment "is the server's environment plus EXPORT_DIR=<--data dir> ... Nothing
        else is added: no keys pass through the server"."""
        s = self.server_with('env > "$EXPORT_DIR/child-env.txt"\n')
        s.post("/api/export", {})
        self.finished(s)
        env = dict(line.split("=", 1) for line in (s.data / "child-env.txt").read_text().splitlines() if "=" in line)
        self.assertEqual(env["EXPORT_DIR"], str(s.data))
        self.assertNotIn("STEAM_API_KEY", env)
        self.assertNotIn("STEAM_PROFILE", env)

    def test_the_full_unredacted_log_stays_on_disk_with_mode_600(self):
        """Contract §4.2: "every line (unredacted) to <data>/export-last.log, truncated at the start of each
        server-started run. That file is for the admin on the box; it never crosses the API"; README: mode 600."""
        s = self.server_with('echo "first run key=SECRET-ONE"\n')
        s.post("/api/export", {})
        self.finished(s)
        log = s.data / "export-last.log"
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertIn("key=SECRET-ONE", log.read_text())
        self.assertNotIn("SECRET-ONE", json.dumps(self.status(s)))
        s.stub("export-stub.sh", 'echo "second run"\n')
        s.post("/api/export", {})
        wait_for(lambda: "second run" in log.read_text(), 10, what="the second run's log")
        self.assertNotIn("first run", log.read_text(), "the log is truncated at the start of each run")

    def test_phases_and_progress_follow_the_exporters_messages(self):
        """Contract §4.7: "The first matching rule wins; lines matching nothing leave the phase unchanged";
        progress "from lines matching ^\\s+(\\d+)/(\\d+)$ ... reset to null on a phase change"."""
        steps = [
            ('echo "$(date \'+%F %T\') starting export"', "starting", None),
            ('echo "Test Persona: 1874 games"', "library", None),
            ('echo "Steam Deck compatibility: fetching 3 games..."', "deck", None),
            ('echo "  (about 32 minutes for store details)"', "details", None),
            ('echo "Store details: fetching 1203 games..."; echo "  350/1203"', "details", {"done": 350, "total": 1203}),
            ('echo "something the table does not know"', "details", {"done": 350, "total": 1203}),
            ('echo "ProtonDB: fetching 9 games..."', "proton", None),
            ('echo "  5/9"', "proton", {"done": 5, "total": 9}),
            ('echo "HowLongToBeat: looking up 4 games..."', "hltb", None),
            ('echo "Wrote /x/steam_backlog.json: 3 games, 1 never launched"', "writing", None),
        ]
        script = 'D="$EXPORT_DIR"\n'
        for i, (cmd, _, _) in enumerate(steps):
            script += f'{cmd}\n: > "$D/.printed-{i}"\nwhile [ ! -e "$D/.go-{i}" ]; do sleep 0.05; done\n'
        s = self.server_with(script)
        s.post("/api/export", {})
        for i, (_, phase, progress) in enumerate(steps):
            wait_for(lambda: (s.data / f".printed-{i}").exists(), 10, what=f"step {i}")
            st = wait_for(lambda: (lambda x: x if (x["phase"], x["progress"]) == (phase, progress) else None)(
                self.status(s)), 10, what=f"phase {phase!r} progress {progress!r} at step {i}")
            self.assertEqual(st["state"], "running")
            (s.data / f".go-{i}").write_text("")
        self.assertEqual(self.finished(s)["phase"], "writing")

    def test_long_lines_are_cut_at_300_characters(self):
        """Contract §4.6 / §4.8 step 8: "each at most 300 characters (longer lines are cut and end in …)"."""
        s = self.server_with("printf 'x%0400d\\n' 0\n")
        s.post("/api/export", {})
        (line,) = self.finished(s)["lines"]
        self.assertEqual(len(line), 300)
        self.assertTrue(line.endswith("…"))


class Redaction(Base):
    STUB = r'''
set -eu
D="$EXPORT_DIR"
printf '%s\n' "Test Persona: 12 games"
printf '%s\n' "GET https://api.example.test/IPlayerService/GetOwnedGames/v1/?key=FAKEKEY0123456789&steamid=$FAKE_ID&format=json"
printf '%s\n' "retry with token=abc123 apikey=zzz9 Password=hunter2 access_token=tok777 api_key=ak888"
printf '%s\n' "peers 192.0.2.55 and 10.1.2.3 answered"
printf '%s\n' "reading /home/someone/.steam-env"
printf '%s\n' "writing $D/steam_backlog.json"
printf '%s\n' "hello Test Persona, welcome back"
printf 'bell\a escape\033[31mred reset\n'
printf '%s\n' "owner $FAKE_ID owns it"
printf 'long %0400d\n' 0
'''

    def setUp(self):
        super().setUp()
        self.fake_id = fake_steamid()
        self.s = self.server_with(self.STUB, env={"FAKE_ID": self.fake_id})
        self.assertEqual(self.s.post("/api/export", {}).status, 202)
        self.st = self.finished(self.s)
        self.lines = self.st["lines"]
        self.blob = json.dumps(self.st) + self.s.get("/api/export/status").text()

    def test_nothing_sensitive_appears_anywhere_in_the_status(self):
        """Contract §4.8 and §4.2: a fake key, an IPv4 address and a SteamID in the output "must not appear" in the
        status object. Checked against the whole serialized response, not just `lines`."""
        for secret in ("FAKEKEY0123456789", self.fake_id, "192.0.2.55", "10.1.2.3", "hunter2", "abc123", "zzz9",
                       "tok777", "ak888", "/home/someone", str(self.s.data), "Test Persona", "\x1b", "\x07"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, self.blob)

    def test_each_rule_leaves_its_marker(self):
        """Contract §4.8, rules 2-7, in order: profile line, SteamIDs, persona, secrets in URLs, paths, IPs."""
        expect = {
            "<profile>: 12 games",
            "GET https://api.example.test/IPlayerService/GetOwnedGames/v1/?key=<redacted>&steamid=7656119XXXXXXXXXX&format=json",
            "retry with token=<redacted> apikey=<redacted> Password=<redacted> access_token=<redacted> api_key=<redacted>",
            "peers <ip> and <ip> answered",
            "reading ~/.steam-env",
            "writing <data>/steam_backlog.json",
            "hello <persona>, welcome back",
            "owner 7656119XXXXXXXXXX owns it",
        }
        self.assertTrue(expect <= set(self.lines), f"missing: {expect - set(self.lines)}\n got: {self.lines}")

    def test_control_characters_are_dropped(self):
        """Contract §4.8 rule 1: "Drop control characters other than tab"."""
        self.assertIn("bell escape[31mred reset", self.lines)

    def test_long_lines_are_cut_after_redaction(self):
        """Contract §4.8 rule 8: "Cut to 300 characters"."""
        long = [x for x in self.lines if x.startswith("long ")]
        self.assertEqual(len(long), 1)
        self.assertEqual(len(long[0]), 300)
        self.assertTrue(long[0].endswith("…"))

    def test_the_admin_log_on_disk_is_the_unredacted_original(self):
        """Contract §4.2: the full unredacted output goes to <data>/export-last.log, mode 0600, and never crosses
        the API."""
        text = (self.s.data / "export-last.log").read_text()
        self.assertIn("key=FAKEKEY0123456789", text)
        self.assertIn(self.fake_id, text)
        self.assertEqual(stat.S_IMODE((self.s.data / "export-last.log").stat().st_mode), 0o600)


# ====================================================================== another process holds the lock

class ExternalRun(Base):
    def hold_lock(self, s: Server):
        fd = os.open(s.data / ".export.lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lambda: os.close(fd))
        return fd

    def test_a_lock_held_by_another_process_shows_as_an_external_run(self):
        """Contract §4.3: "Running" means ... another process holds the export lock (<data>/.export.lock ...). The
        weekly cron run takes the same lock, so it shows up here as origin: "external"."; §4.6: external runs have
        id/startedAt/finishedAt/elapsedSeconds/phase null and empty lines."""
        s = self.make()
        fd = self.hold_lock(s)
        st = self.status(s)
        self.assertEqual((st["state"], st["origin"]), ("running", "external"))
        for k in ("id", "startedAt", "finishedAt", "elapsedSeconds", "phase", "progress", "exitCode", "reason",
                  "libraryChanged"):
            self.assertIsNone(st[k], k)
        self.assertEqual(st["lines"], [])
        self.assertEqual(st["exportedAt"], load_fixture_library()["exportedAt"])
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.status(s)["state"], "idle")

    def test_post_during_an_external_run_is_409_and_starts_nothing(self):
        """Contract §4.9: "Start while the weekly cron run is going: 409 Conflict {"error": "export already
        running", "state": "running", "origin": "external", ...}"."""
        s = self.server_with(WAIT_FOR_RELEASE)
        fd = self.hold_lock(s)
        r = s.post("/api/export", {})
        self.assertEqual(r.status, 409)
        body = r.json()
        self.assertEqual((body["error"], body["state"], body["origin"], body["id"]),
                         ("export already running", "running", "external", None))
        self.assertFalse((s.data / ".started").exists(), "no child may be spawned")
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(s.post("/api/export", {}).status, 202, "free again once the other run ends")
        self.started(s)
        self.release(s)
        self.finished(s)

    def test_the_server_never_keeps_the_lock_itself(self):
        """Contract §4.3: "It never holds the lock itself; the child does". With no run going, the lock is free
        for anyone to take, and the probe leaves it free."""
        s = self.make()
        for _ in range(5):
            self.status(s)
        fd = os.open(s.data / ".export.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
