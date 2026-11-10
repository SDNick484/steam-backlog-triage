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
import math
import os
import sys
import threading
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `python -m unittest tests.test_history` works too

import backlog_query as q
import backlog_server
from _support import FIXTURES, ROOT, Server, atomic_write_json, load_fixture_library, seed_library, wait_for

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


# ====================================================================== C2: history.jsonl

STATS = json.loads((FIXTURES / "stats_library.json").read_text(encoding="utf-8"))
GAMES = [q.normalize(g, i) for i, g in enumerate(STATS["games"])]
# A spread of lanes over the 40 fixture games: some unplayed, some played, one id the library lacks.
TRIAGE = {910001: "next", 910004: "next", 910002: "someday", 910009: "someday", 910005: "skip", 910013: "done",
          910003: "done", 910008: "skip", 999999: "done"}


def history_rows(path: Path) -> list[dict]:
    return read_lines(path) if path.exists() else []


class HistoryRow(unittest.TestCase):
    """backlog_query.history_row: the numbers a day's row holds, pinned to the page's own statistics."""

    def tiles(self, games, triage):
        return q.stats_for(games, dsrc="main", lane_of=lambda g: triage.get(g.appid, ""))["tiles"]

    def test_the_counts_equal_the_statistics_tiles(self):
        row, t = q.history_row(GAMES, TRIAGE), self.tiles(GAMES, TRIAGE)
        self.assertEqual(row["games"], t["games"])
        self.assertEqual((row["never"], row["tried"], row["played"]), (t["never"], t["tried"], t["played"]))
        self.assertEqual(row["backlog"], t["backlogGames"])
        self.assertEqual(row["backlogHours"], t["backlogHours"])
        self.assertEqual(row["backlogWithTime"], t["backlogWithTime"])

    def test_play_hours_is_total_playtime_in_hours_to_a_tenth(self):
        t = self.tiles(GAMES, TRIAGE)
        self.assertEqual(row := q.history_row(GAMES, TRIAGE)["playHours"], math.floor(t["minutes"] / 6 + 0.5) / 10)
        self.assertGreater(row, 0)

    def test_lane_counts_equal_the_overview_and_add_up_to_the_library(self):
        row = q.history_row(GAMES, TRIAGE)
        self.assertEqual(row["laneCounts"], q.overview(GAMES, TRIAGE).lanes)
        self.assertEqual(sum(row["laneCounts"].values()), len(GAMES))
        self.assertEqual(set(row["laneCounts"]), {"next", "someday", "skip", "done", "none"})

    def test_a_triage_entry_for_a_game_not_in_the_library_counts_nowhere(self):
        self.assertEqual(q.history_row(GAMES, {999999: "done"})["laneCounts"]["done"], 0)

    def test_an_unknown_lane_value_counts_as_no_lane(self):
        row = q.history_row(GAMES, {910001: "bogus"})
        self.assertEqual(row["laneCounts"]["none"], len(GAMES))

    def test_closed_games_leave_the_backlog_and_played_games_are_never_in_it(self):
        none = q.history_row(GAMES, {})
        closed = q.history_row(GAMES, {g.appid: "done" for g in GAMES})
        self.assertEqual(closed["backlog"], 0)
        self.assertEqual(closed["backlogHours"], 0)
        self.assertEqual(none["backlog"], none["never"] + none["tried"])

    def test_an_empty_library_is_all_zeros(self):
        row = q.history_row([], {})
        self.assertEqual((row["games"], row["backlog"], row["backlogHours"], row["backlogWithTime"], row["playHours"]),
                         (0, 0, 0, 0, 0))

    def test_hours_round_half_up_like_the_page(self):
        raw = {"appid": 1, "name": "x", "playtime": 0, "hltb": {"main": 0.25, "extra": None, "complete": None}}
        g = [q.normalize(raw)]
        self.assertEqual(q.history_row(g, {})["backlogHours"], self.tiles(g, {})["backlogHours"])
        self.assertEqual(q.history_row(g, {})["backlogHours"], 0.3)

    def test_only_the_main_measure_counts_toward_backlog_hours(self):
        raw = {"appid": 1, "name": "x", "playtime": 0, "hltb": {"main": None, "extra": 8.0, "complete": 9.0}}
        row = q.history_row([q.normalize(raw)], {})
        self.assertEqual((row["backlog"], row["backlogWithTime"], row["backlogHours"]), (1, 0, 0))


class DayRows(unittest.TestCase):
    """History.record_day: one row per local day, replaced in place, then thinned to one a month after 2 years."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="history-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.clock = Clock(2026, 3, 1, 20, 0, 0)  # 20:00 at UTC-8 is 04:00 UTC the next day
        self.h = backlog_server.History(str(self.tmp), now=self.clock)
        self.path = self.tmp / "history.jsonl"

    def seed(self, dates: list[str]) -> None:
        self.path.write_text("".join(json.dumps({"date": d, "games": i}) + "\n" for i, d in enumerate(dates)),
                             encoding="utf-8")

    def dates(self) -> list[str]:
        return [r["date"] for r in history_rows(self.path)]

    def test_the_first_row_is_stamped_with_the_local_date(self):
        self.h.record_day({"games": 10})
        (row,) = history_rows(self.path)
        self.assertEqual(row, {"date": "2026-03-01", "games": 10})  # not 2026-03-02, the UTC date
        self.assertEqual(list(row)[0], "date")

    def test_a_second_write_the_same_day_replaces_the_row_in_place(self):
        self.h.record_day({"games": 10})
        self.clock.advance(hours=2)  # 22:00, still March 1st
        self.h.record_day({"games": 11})
        self.assertEqual(history_rows(self.path), [{"date": "2026-03-01", "games": 11}])

    def test_the_next_local_midnight_starts_a_new_row(self):
        self.h.record_day({"games": 10})
        self.clock.t = datetime.datetime(2026, 3, 1, 23, 59, 59, tzinfo=TZ)
        self.h.record_day({"games": 11})
        self.clock.advance(seconds=2)  # 00:00:01 on the 2nd
        self.h.record_day({"games": 12})
        self.assertEqual(history_rows(self.path), [{"date": "2026-03-01", "games": 11},
                                                   {"date": "2026-03-02", "games": 12}])

    def test_rows_stay_in_date_order_and_one_a_day(self):
        for d in range(1, 6):
            self.clock.t = datetime.datetime(2026, 3, d, 9, 0, tzinfo=TZ)
            self.h.record_day({"games": d})
            self.h.record_day({"games": d * 10})
        rows = history_rows(self.path)
        self.assertEqual([r["date"] for r in rows], [f"2026-03-0{d}" for d in range(1, 6)])
        self.assertEqual([r["games"] for r in rows], [10, 20, 30, 40, 50])

    def test_a_day_that_has_no_write_has_no_row(self):
        self.h.record_day({"games": 1})
        self.clock.advance(days=3)
        self.h.record_day({"games": 2})
        self.assertEqual(self.dates(), ["2026-03-01", "2026-03-04"])

    def test_daily_rows_are_kept_for_two_years(self):
        self.assertEqual(backlog_server.HISTORY_DAILY_DAYS, 730)
        # 2026-03-01 minus 730 days is 2024-03-01, which is still a daily row; 2024-02-29 is one day too old.
        self.seed(["2024-03-01", "2024-03-02", "2025-06-01"])
        self.h.record_day({"games": 9})
        self.assertEqual(self.dates(), ["2024-03-01", "2024-03-02", "2025-06-01", "2026-03-01"])

    def test_older_rows_are_thinned_to_the_last_row_of_each_month(self):
        self.seed(["2023-01-05", "2023-01-20", "2023-02-03", "2024-02-10", "2024-02-29", "2024-03-01", "2025-06-01"])
        self.h.record_day({"games": 9})
        self.assertEqual(self.dates(), ["2023-01-20", "2023-02-03", "2024-02-29", "2024-03-01", "2025-06-01",
                                        "2026-03-01"])
        kept = {r["date"]: r["games"] for r in history_rows(self.path)}
        self.assertEqual(kept["2023-01-20"], 1, "the later of the two January rows, not the first")
        self.assertEqual(kept["2024-02-29"], 4)

    def test_thinning_twice_changes_nothing(self):
        self.seed(["2023-01-05", "2023-01-20", "2023-02-03", "2025-06-01"])
        self.h.record_day({"games": 9})
        once = self.path.read_text(encoding="utf-8")
        self.h.record_day({"games": 9})
        self.assertEqual(self.path.read_text(encoding="utf-8"), once)

    def test_the_file_is_rewritten_whole_with_no_temp_files_left(self):
        self.seed(["2023-01-05", "2023-01-20"])
        self.h.record_day({"games": 9})
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["history.jsonl"])

    def test_unreadable_lines_are_dropped_when_the_file_is_rewritten(self):
        self.path.write_text('{"date": "2026-02-27", "games": 1}\nnot json\n\n[1, 2]\n{"games": 3}\n',
                             encoding="utf-8")
        self.h.record_day({"games": 9})
        self.assertEqual(self.dates(), ["2026-02-27", "2026-03-01"])

    def test_the_file_is_mode_600_like_the_other_data_files(self):
        self.h.record_day({"games": 1})
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")


class Snapshots(unittest.TestCase):
    """App.check_library and App.record_history, in-process, on a fake clock."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="history-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.clock = Clock(2026, 3, 1, 12, 0, 0)
        self.rows = self.tmp / "history.jsonl"

    def app(self, library: dict | None = None) -> "backlog_server.App":
        if library is not None:
            atomic_write_json(self.tmp / "steam_backlog.json", library)
        return backlog_server.App(str(ROOT / "backlog.html"), str(self.tmp), [], now=self.clock)

    def lib(self, n: int) -> dict:
        lib = load_fixture_library()
        lib["games"] = (lib["games"] * 10)[:n]  # ids repeat; only the count matters to these tests
        return lib

    def test_starting_up_writes_no_row(self):
        self.app(self.lib(6))
        self.assertEqual(history_rows(self.rows), [])

    def test_a_new_library_file_is_recorded_once(self):
        app = self.app(self.lib(6))
        atomic_write_json(self.tmp / "steam_backlog.json", self.lib(7))
        self.assertTrue(app.check_library())
        self.assertEqual([r["games"] for r in history_rows(self.rows)], [7])
        self.assertEqual(history_rows(self.rows)[0]["date"], "2026-03-01")
        self.assertFalse(app.check_library(), "nothing changed since the last look")
        self.assertEqual(len(history_rows(self.rows)), 1)

    def test_the_row_uses_the_triage_as_it_is_now(self):
        app = self.app(self.lib(6))
        (self.tmp / "triage.json").write_text(json.dumps({"1001": "done", "1002": "skip"}))
        newer = self.lib(6)
        newer["exportedAt"] = "2026-03-01T12:00:00Z"  # a different file, so a different ETag
        atomic_write_json(self.tmp / "steam_backlog.json", newer)
        app.check_library()
        (row,) = history_rows(self.rows)
        self.assertEqual((row["laneCounts"]["done"], row["laneCounts"]["skip"]), (1, 1))

    def test_the_first_library_ever_is_recorded_when_it_appears(self):
        app = self.app()  # a fresh install: no export yet
        self.assertFalse(app.check_library())
        atomic_write_json(self.tmp / "steam_backlog.json", self.lib(6))
        self.assertTrue(app.check_library())
        self.assertEqual([r["games"] for r in history_rows(self.rows)], [6])

    def test_a_library_that_cannot_be_read_is_skipped_without_an_error(self):
        app = self.app(self.lib(6))
        (self.tmp / "steam_backlog.json.new").write_text("{ not json")
        os.replace(self.tmp / "steam_backlog.json.new", self.tmp / "steam_backlog.json")
        app.check_library()  # must not raise
        self.assertEqual(history_rows(self.rows), [])

    def test_a_vanished_library_is_not_a_snapshot(self):
        app = self.app(self.lib(6))
        os.unlink(self.tmp / "steam_backlog.json")
        self.assertFalse(app.check_library())
        self.assertEqual(history_rows(self.rows), [])

    def test_a_history_that_cannot_be_written_does_not_raise(self):
        app = self.app(self.lib(6))
        (self.tmp / "history.jsonl").mkdir()
        atomic_write_json(self.tmp / "steam_backlog.json", self.lib(7))
        app.check_library()  # must not raise

    def test_the_steamid_and_persona_never_reach_a_row(self):
        app = self.app(self.lib(6))
        lib = self.lib(7)
        lib["steamid"] = "7656119" + "0" * 10
        atomic_write_json(self.tmp / "steam_backlog.json", lib)
        app.check_library()
        text = self.rows.read_text(encoding="utf-8")
        self.assertNotIn("7656119", text)
        self.assertNotIn("Test Persona", text)


class ListenQueue(unittest.TestCase):
    def test_the_server_queues_more_than_the_default_five_connections(self):
        self.assertGreaterEqual(backlog_server.BacklogHTTPServer.request_queue_size, 64)


class HistoryHttp(unittest.TestCase):
    """The real server, on the real clock: what a lane change, a loaded library and an export leave behind."""

    def start(self, *, seed: bool = True, **kw) -> Server:
        s = Server(seed=seed, **kw)
        self.addCleanup(s.cleanup)
        return s.start()

    @staticmethod
    def today_either_end(before: datetime.date, after: datetime.date):
        return {before.isoformat(), after.isoformat()}

    def test_a_lane_change_leaves_one_row_for_today_with_the_counts(self):
        s = self.start()
        before = datetime.datetime.now().astimezone().date()
        self.assertEqual(s.put("/api/triage", {"appid": 1001, "lane": "done"}).status, 200)
        after = datetime.datetime.now().astimezone().date()
        (row,) = history_rows(s.data / "history.jsonl")
        self.assertIn(row["date"], self.today_either_end(before, after))
        # The fixture library: 6 games, one played (1003), one tried (1002), four never launched.
        self.assertEqual((row["games"], row["never"], row["tried"], row["played"]), (6, 4, 1, 1))
        self.assertEqual(row["laneCounts"], {"next": 0, "someday": 0, "skip": 0, "done": 1, "none": 5})
        self.assertEqual(row["backlog"], 4)  # 1002, 1004, 1005, 1006; 1001 is done and 1003 is played
        self.assertEqual(row["backlogWithTime"], 2)  # 1002 (12 h) and 1006 (30 h) have a main time
        self.assertEqual(row["backlogHours"], 42)
        self.assertEqual(row["playHours"], 10.8)  # 645 minutes

    def test_more_changes_the_same_day_update_that_row(self):
        s = self.start()
        s.put("/api/triage", {"appid": 1001, "lane": "next"})
        s.put("/api/triage", {"appid": 1002, "lane": "skip"})
        rows = history_rows(s.data / "history.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["laneCounts"]["next"], 1)
        self.assertEqual(rows[0]["laneCounts"]["skip"], 1)
        self.assertEqual(rows[0]["backlog"], 4)  # 1001 stays (next); 1002 left

    def test_a_write_that_changes_nothing_writes_no_row(self):
        s = self.start()
        s.put("/api/triage", {"appid": 1001, "lane": ""})
        self.assertEqual(history_rows(s.data / "history.jsonl"), [])
        s.put("/api/triage", {"appid": 1001, "lane": "next"})
        first = (s.data / "history.jsonl").read_text()
        s.put("/api/triage", {"appid": 1001, "lane": "next"})
        self.assertEqual((s.data / "history.jsonl").read_text(), first)

    def test_a_server_that_only_starts_up_writes_nothing(self):
        s = self.start()
        self.assertFalse((s.data / "history.jsonl").exists())
        self.assertFalse((s.data / "lane_log.jsonl").exists())

    def test_a_lane_change_with_no_library_logs_the_change_but_has_no_counts_to_write(self):
        s = self.start(seed=False)
        self.assertEqual(s.put("/api/triage", {"appid": 1001, "lane": "next"}).status, 200)
        self.assertEqual(len(read_lines(s.data / "lane_log.jsonl")), 1)
        self.assertEqual(history_rows(s.data / "history.jsonl"), [])

    def test_loading_a_library_from_the_page_is_recorded(self):
        s = self.start()
        lib = load_fixture_library()
        lib["games"].append({"appid": 1007, "name": "Test Game Golf", "playtime": 0})
        self.assertEqual(s.put("/api/library", lib).status, 200)
        (row,) = history_rows(s.data / "history.jsonl")
        self.assertEqual(row["games"], 7)

    def test_an_export_that_finishes_is_recorded(self):
        new = Path(tempfile.mkdtemp(prefix="history-test-")) / "new-library.json"
        self.addCleanup(lambda: __import__("shutil").rmtree(new.parent, ignore_errors=True))
        lib = load_fixture_library()
        lib["games"].append({"appid": 1007, "name": "Test Game Golf", "playtime": 0})
        new.write_text(json.dumps(lib))
        s = Server(export_cmd=None, env={"STUB_NEW_LIBRARY": str(new)})
        self.addCleanup(s.cleanup)
        stub = s.stub("export-stub.sh", 'D="${EXPORT_DIR:?}"\ncp "$STUB_NEW_LIBRARY" "$D/.new-lib" && '
                                        'mv "$D/.new-lib" "$D/steam_backlog.json"\n')
        s.cmd += ["--export-cmd", str(stub)]
        s.start()
        self.assertEqual(s.post("/api/export", {}).status, 202)
        wait_for(lambda: s.get("/api/export/status").json()["state"] == "succeeded", 15, what="the export")
        rows = wait_for(lambda: history_rows(s.data / "history.jsonl"), 10, what="the row for the export")
        self.assertEqual([r["games"] for r in rows], [7])

    def test_an_export_that_cron_ran_is_noticed_by_the_poll(self):
        s = Server(extra=["--library-poll", "0.2"])
        self.addCleanup(s.cleanup)
        s.start()
        lib = load_fixture_library()
        lib["games"].append({"appid": 1007, "name": "Test Game Golf", "playtime": 0})
        atomic_write_json(s.data / "steam_backlog.json", lib)  # what the exporter does, from outside the server
        rows = wait_for(lambda: history_rows(s.data / "history.jsonl"), 10, what="the poll to notice the new library")
        self.assertEqual([r["games"] for r in rows], [7])

    def test_the_poll_can_be_turned_off(self):
        s = Server(extra=["--library-poll", "0"])
        self.addCleanup(s.cleanup)
        s.start()
        lib = load_fixture_library()
        lib["games"].append({"appid": 1007, "name": "Test Game Golf", "playtime": 0})
        atomic_write_json(s.data / "steam_backlog.json", lib)
        import time

        time.sleep(1.0)
        self.assertEqual(history_rows(s.data / "history.jsonl"), [])

    def test_a_history_file_that_cannot_be_written_never_fails_the_triage_write(self):
        s = self.start()
        s.stop()
        (s.data / "history.jsonl").mkdir()
        s.start()
        self.assertEqual(s.put("/api/triage", {"appid": 1001, "lane": "done"}).status, 200)
        self.assertEqual(s.get("/api/triage").json(), {"1001": "done"})
        self.assertEqual(len(read_lines(s.data / "lane_log.jsonl")), 1, "the lane log is a separate file")

    def test_concurrent_changes_leave_a_final_row_that_matches_the_final_state(self):
        """The row is computed under the lock the triage write holds, so a slow writer can't land an old count
        on top of a newer one."""
        s = Server(seed=False)
        self.addCleanup(s.cleanup)
        seed_library(s.data, STATS)
        s.start()
        ids = list(range(910001, 910021))

        def put(i: int, appid: int) -> None:
            s.put("/api/triage", {"appid": appid, "lane": "done" if i % 2 else "next"})

        threads = [threading.Thread(target=put, args=(i, a)) for i, a in enumerate(ids)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        (row,) = history_rows(s.data / "history.jsonl")
        triage = {int(k): v for k, v in json.loads((s.data / "triage.json").read_text()).items()}
        want = q.history_row(GAMES, triage)
        self.assertEqual({k: v for k, v in row.items() if k != "date"}, want)
        self.assertEqual(len(read_lines(s.data / "lane_log.jsonl")), 20)


# ====================================================================== C3: GET /api/history

class HistoryRoute(unittest.TestCase):
    def start(self, **kw) -> Server:
        s = Server(**kw)
        self.addCleanup(s.cleanup)
        return s.start()

    def test_before_anything_has_happened_both_lists_are_empty(self):
        r = self.start().get("/api/history")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json(), {"days": [], "lanes": []})
        self.assertEqual(r.headers.get("content-type"), "application/json")

    def test_it_returns_the_day_rows_and_the_lane_changes_in_file_order(self):
        s = self.start()
        s.put("/api/triage", {"appid": 1001, "lane": "next"})
        s.put("/api/triage", {"appid": 1001, "lane": "done"})
        body = s.get("/api/history").json()
        self.assertEqual(list(body), ["days", "lanes"])
        self.assertEqual(body["days"], history_rows(s.data / "history.jsonl"))
        self.assertEqual(body["days"][0]["laneCounts"]["done"], 1)
        lanes = body["lanes"]
        self.assertEqual([(x["appid"], x["from"], x["to"]) for x in lanes], [(1001, "", "next"), (1001, "next", "done")])
        self.assertTrue(all(isinstance(x["ts"], int) for x in lanes))

    def test_lines_that_are_not_json_objects_are_left_out(self):
        s = self.start()
        (s.data / "lane_log.jsonl").write_text('{"ts":1,"appid":5,"from":"","to":"done"}\nnot json\n[1]\n\n')
        (s.data / "history.jsonl").write_text('{"date":"2026-01-01","games":1}\n42\n{broken\n')
        body = s.get("/api/history").json()
        self.assertEqual(body["lanes"], [{"ts": 1, "appid": 5, "from": "", "to": "done"}])
        self.assertEqual(body["days"], [{"date": "2026-01-01", "games": 1}])

    def test_it_carries_an_etag_and_a_matching_request_gets_304(self):
        s = self.start()
        s.put("/api/triage", {"appid": 1001, "lane": "next"})
        first = s.get("/api/history")
        etag = first.headers.get("etag")
        self.assertRegex(etag, r'^"[0-9a-f]{32}"$')
        again = s.get("/api/history", headers={"If-None-Match": etag})
        self.assertEqual(again.status, 304)
        self.assertEqual(again.body, b"")
        self.assertEqual(again.headers.get("etag"), etag)
        self.assertEqual(s.get("/api/history", headers={"If-None-Match": "W/" + etag}).status, 304)
        self.assertEqual(s.get("/api/history", headers={"If-None-Match": '"other"'}).status, 200)

    def test_a_lane_change_changes_the_etag_and_a_no_op_does_not(self):
        s = self.start()
        s.put("/api/triage", {"appid": 1001, "lane": "next"})
        before = s.get("/api/history").headers["etag"]
        s.put("/api/triage", {"appid": 1001, "lane": "next"})
        self.assertEqual(s.get("/api/history").headers["etag"], before)
        s.put("/api/triage", {"appid": 1001, "lane": "done"})
        self.assertNotEqual(s.get("/api/history").headers["etag"], before)

    def test_it_is_additive_the_api_version_stays_2(self):
        r = self.start().get("/api/history")
        self.assertEqual(r.headers.get("x-backlog-api"), "2")
        self.assertEqual(backlog_server.API_VERSION, "2")

    def test_it_is_read_only(self):
        s = self.start()
        for method in ("PUT", "POST", "DELETE"):
            r = s.req(method, "/api/history", {"days": []})
            self.assertGreaterEqual(r.status, 400, method)
        self.assertFalse((s.data / "history.jsonl").exists())

    def test_head_has_the_headers_and_no_body(self):
        s = self.start()
        r = s.req("HEAD", "/api/history")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body, b"")

    def test_the_security_headers_are_on_it(self):
        r = self.start().get("/api/history")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_a_directory_in_place_of_a_file_reads_as_empty_not_an_error(self):
        s = self.start()
        (s.data / "history.jsonl").mkdir()
        self.assertEqual(s.get("/api/history").json(), {"days": [], "lanes": []})


if __name__ == "__main__":
    unittest.main()
