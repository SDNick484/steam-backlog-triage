"""run-export.sh: settings from the env file, the lock, and its sysexits-style exit codes.

Expected behavior is the header of run-export.sh and section 5.2 of docs/backlog-api-contract.md (in SDNick484/mcp-server-steam-backlog) (exit 78 when the
keys are missing, 75 when another export holds the lock, 0 on success). The script runs from a copy of the repo
files in a temp dir, against the mock Steam server (tests/_support.py), with howlongtobeatpy hidden.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _support import EXPORTER, RUN_EXPORT, MockSteam, exporter_env, fake_steamid  # noqa: E402

KEY = "FAKEKEY0123456789"


@unittest.skipUnless(shutil.which("flock"), "flock is needed (util-linux or BusyBox)")
class RunExport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.here = self.root / "app"
        self.here.mkdir()
        shutil.copy(RUN_EXPORT, self.here / "run-export.sh")
        shutil.copy(EXPORTER, self.here / "steam_backlog_export.py")
        self.out = self.root / "exports"
        self.home = self.root / "home"
        self.home.mkdir()
        self.envf = self.root / "steam-env"
        self.mock = MockSteam(games=[{"appid": 3001, "name": "Test Game Solo", "playtime_forever": 3}])
        self.mock.__enter__()
        self.addCleanup(self.mock.__exit__, None, None, None)

    def write_env(self, text: str) -> None:
        self.envf.write_text(text)
        self.envf.chmod(0o600)

    def run_script(self, extra_env=None) -> subprocess.CompletedProcess:
        env = exporter_env(self.mock, {"HOME": str(self.home), "STEAM_ENV_FILE": str(self.envf),
                                       "EXPORT_DIR": str(self.out), **(extra_env or {})})
        return subprocess.run(["sh", str(self.here / "run-export.sh")], env=env, capture_output=True, text=True,
                              timeout=120)

    def test_missing_settings_exit_78_and_say_where_to_set_them(self):
        """Header: '78  STEAM_API_KEY or STEAM_PROFILE isn't set'. Nothing is requested."""
        for name, text in [("no file", None), ("empty file", ""), ("key only", f"STEAM_API_KEY='{KEY}'\n"),
                           ("profile only", f"STEAM_PROFILE='{fake_steamid()}'\n"),
                           ("empty values", "STEAM_API_KEY=''\nSTEAM_PROFILE=''\n")]:
            with self.subTest(name):
                if text is not None:
                    self.write_env(text)
                p = self.run_script()
                self.assertEqual(p.returncode, 78, p.stdout + p.stderr)
                self.assertRegex(p.stdout, r"skipped: set STEAM_API_KEY and STEAM_PROFILE in .*steam-env")
                self.assertEqual(self.mock.hits, {})
                self.assertFalse((self.out / "steam_backlog.json").exists())

    def test_a_held_lock_exits_75_and_runs_nothing(self):
        """Header: '75  another export is already running (the lock is held)' - whoever holds $EXPORT_DIR/.export.lock."""
        self.write_env(f"STEAM_API_KEY='{KEY}'\nSTEAM_PROFILE='{fake_steamid()}'\n")
        self.out.mkdir()
        fd = os.open(self.out / ".export.lock", os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = self.run_script()
        self.assertEqual(p.returncode, 75, p.stdout + p.stderr)
        self.assertIn("skipped: another export is running", p.stdout)
        self.assertEqual(self.mock.hits, {})
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.run_script().returncode, 0, "free again once the other holder let go")

    def test_a_good_run_writes_the_library_and_releases_the_lock(self):
        """Header: writes steam_backlog.json (and the cache) under the export dir; a numeric STEAM_PROFILE is a
        SteamID; the run holds the lock only until it exits."""
        self.write_env(f"STEAM_API_KEY='{KEY}'\nSTEAM_PROFILE='{fake_steamid()}'\n")
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertRegex(p.stdout, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} starting export")
        self.assertTrue((self.out / "steam_backlog.json").is_file())
        self.assertTrue((self.out / "steam_backlog_cache.json").is_file())
        self.assertIn("Test Persona: 1 games", p.stderr)
        self.assertGreaterEqual(self.mock.count("details:"), 1, "run-export.sh passes --details")
        fd = os.open(self.out / ".export.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_the_lock_is_held_by_the_exporter_while_it_runs(self):
        """Header: 'the exporter itself holds the lock until it exits' (fd 9 survives the exec). The mock holds the
        exporter inside its first Deck request, and the test checks that the lock is taken at that moment."""
        import threading
        self.write_env(f"STEAM_API_KEY='{KEY}'\nSTEAM_PROFILE='{fake_steamid()}'\n")
        self.mock.gate = threading.Event()
        result = {}
        t = threading.Thread(target=lambda: result.update(p=self.run_script()))
        t.start()
        try:
            self.assertTrue(self.mock.started.wait(30), "the exporter never reached the Deck request")
            fd = os.open(self.out / ".export.lock", os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        finally:
            self.mock.gate.set()
            t.join(60)
        self.assertEqual(result["p"].returncode, 0, result["p"].stderr)

    def test_a_failing_exporter_passes_its_exit_status_through(self):
        """Header: '*  the exporter failed'. A key Steam rejects makes the exporter exit non-zero with a message."""
        self.write_env(f"STEAM_API_KEY='{KEY}'\nSTEAM_PROFILE='https://steamcommunity.com/id/nobody-here/'\n")
        p = self.run_script()
        self.assertNotIn(p.returncode, (0, 75, 78), p.stdout + p.stderr)
        self.assertIn("nobody-here", p.stderr)

    def test_the_key_is_not_echoed(self):
        """The script must not print the key it read from the env file."""
        self.write_env(f"STEAM_API_KEY='{KEY}'\nSTEAM_PROFILE='{fake_steamid()}'\n")
        p = self.run_script()
        self.assertNotIn(KEY, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
