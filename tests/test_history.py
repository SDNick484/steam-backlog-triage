"""History: lane_log.jsonl, history.jsonl and GET /api/history (backlog_server.py, backlog_query.history_row).

Standard library only:  python3 -m unittest tests.test_history -v

The storage tests run History and App in-process with a fake clock (a fixed UTC-8 offset, so the local day
is not the UTC day). The HTTP tests run the real server as a child process on the real clock, so they never
assert a calendar date except "today, by the server's clock", which they compare against both ends of the
test to stay correct across midnight.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `python -m unittest tests.test_history` works too

import backlog_server
from _support import FIXTURES, Server, seed_library, wait_for

TZ = datetime.timezone(datetime.timedelta(hours=-8))


class Clock:
    """A callable returning a settable, timezone-aware 'now' (the server's local time)."""

    def __init__(self, *args: int):
        self.t = datetime.datetime(*args, tzinfo=TZ)

    def __call__(self) -> datetime.datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += datetime.timedelta(**kw)


def read_lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ====================================================================== C1: lane_log.jsonl

class LaneLogStorage(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="history-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.clock = Clock(2026, 3, 1, 20, 0, 0)  # 20:00 at UTC-8 is 04:00 UTC the next day
        self.h = backlog_server.History(str(self.tmp), now=self.clock)
        self.path = self.tmp / "lane_log.jsonl"

    def test_a_change_appends_one_json_line_with_ts_appid_from_to(self):
        self.assertTrue(self.h.log_lane(620, "", "next"))
        (row,) = read_lines(self.path)
        self.assertEqual(row, {"ts": int(self.clock.t.timestamp()), "appid": 620, "from": "", "to": "next"})

    def test_ts_is_utc_epoch_seconds_not_a_local_time(self):
        self.h.log_lane(620, "next", "done")
        (row,) = read_lines(self.path)
        # 2026-03-02T04:00:00Z, though the server's local clock says March 1st.
        self.assertEqual(row["ts"], int(datetime.datetime(2026, 3, 2, 4, 0, tzinfo=datetime.timezone.utc).timestamp()))

    def test_each_change_is_its_own_line_in_order(self):
        self.h.log_lane(1, "", "next")
        self.clock.advance(minutes=5)
        self.h.log_lane(1, "next", "done")
        self.h.log_lane(2, "", "skip")
        rows = read_lines(self.path)
        self.assertEqual([(r["appid"], r["from"], r["to"]) for r in rows],
                         [(1, "", "next"), (1, "next", "done"), (2, "", "skip")])
        self.assertLess(rows[0]["ts"], rows[1]["ts"])

    def test_clearing_a_lane_is_logged_as_to_empty(self):
        self.h.log_lane(7, "someday", "")
        (row,) = read_lines(self.path)
        self.assertEqual((row["from"], row["to"]), ("someday", ""))

    def test_a_write_that_changes_nothing_logs_nothing(self):
        self.assertFalse(self.h.log_lane(620, "next", "next"))
        self.assertFalse(self.h.log_lane(620, "", ""))
        self.assertFalse(self.path.exists(), "no file is created for a no-op")

    def test_the_log_is_capped_and_the_oldest_lines_go_first(self):
        h = backlog_server.History(str(self.tmp), now=self.clock, lane_max=5)
        for i in range(1, 8):
            h.log_lane(i, "", "next")
        self.assertEqual([r["appid"] for r in read_lines(self.path)], [3, 4, 5, 6, 7])

    def test_an_already_oversized_file_is_cut_back_on_the_next_append(self):
        self.path.write_text("".join(json.dumps({"ts": i, "appid": i, "from": "", "to": "next"}) + "\n"
                                     for i in range(1, 13)), encoding="utf-8")
        h = backlog_server.History(str(self.tmp), now=self.clock, lane_max=5)
        h.log_lane(99, "", "done")
        self.assertEqual([r["appid"] for r in read_lines(self.path)], [9, 10, 11, 12, 99])

    def test_the_default_cap_is_fifty_thousand_lines(self):
        self.assertEqual(backlog_server.LANE_LOG_MAX, 50_000)
        self.assertEqual(self.h.lane_max, 50_000)

    def test_pruning_leaves_no_temp_files_behind(self):
        h = backlog_server.History(str(self.tmp), now=self.clock, lane_max=2)
        for i in range(1, 6):
            h.log_lane(i, "", "next")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["lane_log.jsonl"])


class LaneLogHttp(unittest.TestCase):
    def setUp(self):
        self.srv = Server().start()
        self.addCleanup(self.srv.cleanup)
        self.path = self.srv.data / "lane_log.jsonl"

    def lines(self) -> list[dict]:
        return read_lines(self.path) if self.path.exists() else []

    def test_a_triage_write_that_changes_a_lane_is_logged(self):
        r = self.srv.put("/api/triage", {"appid": 1001, "lane": "next"})
        self.assertEqual(r.status, 200)
        (row,) = self.lines()
        self.assertEqual((row["appid"], row["from"], row["to"]), (1001, "", "next"))
        self.assertIsInstance(row["ts"], int)

    def test_from_is_the_previous_lane_the_server_knew(self):
        self.srv.put("/api/triage", {"appid": 1001, "lane": "next"})
        self.srv.put("/api/triage", {"appid": 1001, "lane": "done"})
        self.srv.put("/api/triage", {"appid": 1001, "lane": ""})
        self.assertEqual([(r["from"], r["to"]) for r in self.lines()], [("", "next"), ("next", "done"), ("done", "")])

    def test_putting_the_lane_a_game_is_already_in_logs_nothing(self):
        self.srv.put("/api/triage", {"appid": 1001, "lane": "next"})
        r = self.srv.put("/api/triage", {"appid": 1001, "lane": "next"})
        self.assertEqual(r.status, 200)
        self.assertEqual(len(self.lines()), 1)

    def test_clearing_a_game_that_has_no_lane_logs_nothing(self):
        r = self.srv.put("/api/triage", {"appid": 1001, "lane": ""})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.lines(), [])

    def test_a_write_refused_by_if_match_logs_nothing(self):
        r = self.srv.put("/api/triage", {"appid": 1001, "lane": "next"}, {"If-Match": '"' + "0" * 32 + '"'})
        self.assertEqual(r.status, 412)
        self.assertEqual(self.lines(), [])

    def test_a_rejected_body_logs_nothing(self):
        self.assertEqual(self.srv.put("/api/triage", {"appid": "x", "lane": "next"}).status, 400)
        self.assertEqual(self.srv.put("/api/triage", {"appid": 1, "lane": "bogus"}).status, 400)
        self.assertEqual(self.lines(), [])

    def test_notes_and_order_writes_are_not_lane_changes(self):
        self.srv.put("/api/notes", {"appid": 1001, "note": "co-op"})
        self.srv.put("/api/order", {"order": [1001, 1002]})
        self.assertEqual(self.lines(), [])

    def test_the_log_is_written_whoever_sent_the_change(self):
        """The server can't tell the page from the MCP server: both are a PUT. A client that sends an
        If-Match (as the MCP does) is logged exactly like one that doesn't."""
        etag = self.srv.get("/api/triage").headers["etag"]
        self.assertEqual(self.srv.put("/api/triage", {"appid": 5, "lane": "skip"}, {"If-Match": etag}).status, 200)
        self.assertEqual([(r["appid"], r["to"]) for r in self.lines()], [(5, "skip")])

    def test_twenty_concurrent_changes_make_twenty_whole_lines(self):
        ids = list(range(910001, 910021))
        errors: list[Exception] = []

        def put(appid: int) -> None:
            try:
                self.assertEqual(self.srv.put("/api/triage", {"appid": appid, "lane": "next"}).status, 200)
            except Exception as e:  # noqa: BLE001 - reported below, from the main thread
                errors.append(e)

        threads = [threading.Thread(target=put, args=(a,)) for a in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rows = self.lines()  # every line must parse: no interleaved writes
        self.assertEqual(sorted(r["appid"] for r in rows), ids)
        self.assertTrue(all((r["from"], r["to"]) == ("", "next") for r in rows))
        triage = json.loads((self.srv.data / "triage.json").read_text())
        self.assertEqual(sorted(int(k) for k in triage), ids)

    def test_a_log_that_cannot_be_written_never_fails_the_triage_write(self):
        """Logging is best effort: the person's click is the primary write."""
        self.srv.stop()
        self.path.mkdir()  # a directory where the file should be: every append fails
        self.srv.start()
        r = self.srv.put("/api/triage", {"appid": 1001, "lane": "done"})
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads((self.srv.data / "triage.json").read_text()), {"1001": "done"})
        self.assertEqual(self.srv.get("/api/triage").json(), {"1001": "done"})
        self.assertEqual(self.srv.get("/healthz").status, 200)  # still serving

    def test_the_api_version_is_unchanged(self):
        self.assertEqual(backlog_server.API_VERSION, "2")
        self.assertEqual(self.srv.get("/api/triage").headers["x-backlog-api"], "2")


if __name__ == "__main__":
    unittest.main()
