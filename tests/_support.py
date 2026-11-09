"""Helpers shared by the test modules. Standard library only.

Not a test module itself (unittest discovery only collects test*.py).
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
NO_HLTB = FIXTURES / "nohltb"  # first on PYTHONPATH: hides howlongtobeatpy
SHIM = TESTS / "exporter_shim.py"
SERVER = ROOT / "backlog_server.py"
EXPORTER = ROOT / "steam_backlog_export.py"
RUN_EXPORT = ROOT / "run-export.sh"


def fake_steamid() -> str:
    """A syntactically valid SteamID64 built at run time, so no 17-digit ID is ever in the tree."""
    return "7656119" + "0" * 10


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05, what: str = "condition"):
    """Poll until predicate() returns something truthy; return it. Raises AssertionError on timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last value: {last!r})")


# ------------------------------------------------------------------ HTTP client

class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers  # lower-cased names
        self.body = body

    def json(self):
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def request(port: int, method: str, path: str, body=None, headers: dict[str, str] | None = None,
            timeout: float = 15.0) -> Response:
    """One request on a fresh connection (the server speaks HTTP/1.0). `body` may be bytes, str, or
    anything JSON-serializable (sent as application/json unless headers say otherwise)."""
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = json.dumps(body).encode()
            if not any(k.lower() == "content-type" for k in hdrs):
                hdrs["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        payload = r.read()
        return Response(r.status, {k.lower(): v for k, v in r.getheaders()}, payload)
    finally:
        conn.close()


def raw_request(port: int, head: bytes, timeout: float = 10.0) -> bytes:
    """Send raw bytes and read until the server closes. For requests http.client won't build
    (an oversized Content-Length with no body, a malformed request line)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(head)
        chunks = []
        while True:
            try:
                c = s.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not c:
                break
            chunks.append(c)
    return b"".join(chunks)


# ------------------------------------------------------------------ data files

def atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def load_fixture_library() -> dict:
    return json.loads((FIXTURES / "library.json").read_text(encoding="utf-8"))


def seed_library(data_dir: Path, library: dict | None = None) -> dict:
    lib = library if library is not None else load_fixture_library()
    atomic_write_json(Path(data_dir) / "steam_backlog.json", lib)
    return lib


# ------------------------------------------------------------------ stub export commands

def write_stub(path: Path, body: str) -> Path:
    """Write an executable /bin/sh script. Stands in for run-export.sh via --export-cmd."""
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


NOOP_STUB = "exit 0\n"


# ------------------------------------------------------------------ the dashboard server

class Server:
    """backlog_server.py running as a child process on a free port with its own data directory.

    export_cmd: path to pass as --export-cmd; "noop" (default) installs a stub that does nothing, so a
    test that never POSTs can't run anything real; None passes no flag at all and, together with
    isolated=True, runs a copy of the server with no run-export.sh beside it (exports unconfigured).
    """

    def __init__(self, data_dir: Path | None = None, *, export_cmd="noop", extra: list[str] | None = None,
                 isolated: bool = False, env: dict[str, str] | None = None, page: Path | None = None,
                 seed: bool = True, nav: list[str] | None = None, timeout_start: float = 15.0):
        self._tmp = Path(tempfile.mkdtemp(prefix="backlog-test-"))
        self.data = Path(data_dir) if data_dir else self._tmp / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        if seed and not (self.data / "steam_backlog.json").exists():
            seed_library(self.data)
        self.port = free_port()
        self.home = self._tmp / "home"
        self.home.mkdir(exist_ok=True)
        script = SERVER
        if isolated:
            solo = self._tmp / "solo"
            solo.mkdir()
            shutil.copy(SERVER, solo / "backlog_server.py")
            shutil.copy(ROOT / "backlog.html", solo / "backlog.html")
            for mod in ("ask.py", "backlog_query.py", "access_jwt.py"):  # backlog_server.py imports these
                shutil.copy(ROOT / mod, solo / mod)
            script = solo / "backlog_server.py"
        self.cmd = [sys.executable, str(script), "--port", str(self.port), "--bind", "127.0.0.1",
                    "--data", str(self.data)]
        if page:
            self.cmd += ["--page", str(page)]
        for n in nav or []:
            self.cmd += ["--nav", n]
        if export_cmd == "noop":
            export_cmd = write_stub(self._tmp / "noop-export.sh", NOOP_STUB)
        if export_cmd is not None:
            self.cmd += ["--export-cmd", str(export_cmd)]
        self.cmd += list(extra or [])
        self.env = {**os.environ, "HOME": str(self.home), "PYTHONUNBUFFERED": "1"}
        for k in ("STEAM_API_KEY", "STEAM_PROFILE", "STEAM_ENV_FILE", "EXPORT_DIR"):
            self.env.pop(k, None)  # a developer's own settings must never reach a test run
        self.env.update(env or {})
        self.timeout_start = timeout_start
        self.proc: subprocess.Popen | None = None
        self._log = self._tmp / "server.log"

    # ---- lifecycle
    def start(self) -> "Server":
        with open(self._log, "ab") as log:
            self.proc = subprocess.Popen(self.cmd, stdout=log, stderr=log, env=self.env, cwd=str(self._tmp))
        try:
            wait_for(self._healthy, self.timeout_start, what="the server to answer /healthz")
        except AssertionError:
            self.stop()
            raise AssertionError("server did not start:\n" + self.log_text())
        return self

    def _healthy(self) -> bool:
        if self.proc and self.proc.poll() is not None:
            raise AssertionError(f"server exited early ({self.proc.returncode}):\n{self.log_text()}")
        try:
            return request(self.port, "GET", "/healthz", timeout=2).status == 200
        except OSError:
            return False

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def cleanup(self) -> None:
        self.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.cleanup()

    # ---- conveniences
    @property
    def tmp(self) -> Path:
        return self._tmp

    def log_text(self) -> str:
        try:
            return self._log.read_text(errors="replace")
        except OSError:
            return ""

    def req(self, method: str, path: str, body=None, headers=None, **kw) -> Response:
        return request(self.port, method, path, body, headers, **kw)

    def get(self, path: str, headers=None) -> Response:
        return self.req("GET", path, headers=headers)

    def put(self, path: str, body, headers=None) -> Response:
        return self.req("PUT", path, body, headers)

    def post(self, path: str, body=None, headers=None) -> Response:
        return self.req("POST", path, body, headers)

    def stub(self, name: str, body: str) -> Path:
        return write_stub(self._tmp / name, body)


# ------------------------------------------------------------------ mock Steam / store / ProtonDB

class MockSteam:
    """One local HTTP server standing in for api.steampowered.com, store.steampowered.com and
    protondb.com. Point STEAM_API_BASE, STEAM_STORE_BASE and PROTONDB_BASE at `base`.

    Behavior per app id is set with `deck`, `details`, `reviews` and `proton`: appid -> a list of canned
    responses used in order (the last one repeats). A canned response is a (status, body, headers)
    tuple, where body is bytes or JSON-serializable. Anything not listed gets a normal 200 answer.
    Store tags: `tags` maps appid -> tag ids (default DEFAULT_TAG_IDS), `tags_omit` is a set of app ids left
    out of GetItems answers, `tags_batch` is a list of canned responses for GetItems calls (by call number,
    the last repeats) and `taglist` the same for GetTagList; `tag_batches` records the app ids of each call.
    `hits` counts requests per route key ("owned", "persona", "deck:<appid>", ...).
    """

    def __init__(self, games: list[dict] | None = None, persona: str | None = "Test Persona"):
        self.games = games if games is not None else default_owned_games()
        self.persona = persona
        self.deck: dict[int, list] = {}
        self.details: dict[int, list] = {}
        self.proton: dict[int, list] = {}
        self.reviews: dict[int, list] = {}
        self.tags: dict[int, list[int]] = {}
        self.tags_omit: set[int] = set()
        self.tags_batch: list = []
        self.taglist: list = []
        self.tag_batches: list[list[int]] = []
        self.hits: dict[str, int] = {}
        self.paths: list[str] = []
        self._mu = threading.Lock()
        self.gate: threading.Event | None = None  # when set, "deck" answers wait for it (see wait_started)
        self.started = threading.Event()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep test output quiet
                pass

            def do_GET(self):
                outer._handle(self)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "MockSteam":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self.gate:
            self.gate.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def env(self) -> dict[str, str]:
        return {"STEAM_API_BASE": self.base, "STEAM_STORE_BASE": self.base, "PROTONDB_BASE": self.base}

    def count(self, prefix: str) -> int:
        with self._mu:
            return sum(v for k, v in self.hits.items() if k.startswith(prefix))

    # ---- request handling
    def _canned(self, table: dict[int, list], appid: int, key: str):
        with self._mu:
            seq = table.get(appid)
            if not seq:
                return None
            n = self.hits[key]  # already incremented: 1-based
            return seq[min(n, len(seq)) - 1]

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        from urllib.parse import parse_qs, urlparse
        u = urlparse(h.path)
        q = parse_qs(u.query)
        route, appid = None, None
        if u.path.endswith("/ISteamUser/GetPlayerSummaries/v2/"):
            route = "persona"
        elif u.path.endswith("/IPlayerService/GetOwnedGames/v1/"):
            route = "owned"
        elif u.path.endswith("/saleaction/ajaxgetdeckappcompatibilityreport"):
            route, appid = "deck", int(q["nAppID"][0])
        elif u.path.endswith("/api/appdetails"):
            route, appid = "details", int(q["appids"][0])
        elif u.path.endswith("/IStoreBrowseService/GetItems/v1"):
            route = "tags"
        elif u.path.endswith("/IStoreService/GetTagList/v1/"):
            route = "taglist"
        elif u.path.startswith("/appreviews/"):
            route, appid = "reviews", int(u.path.rsplit("/", 1)[1])
        elif u.path.startswith("/api/v1/reports/summaries/"):
            route, appid = "proton", int(u.path.rsplit("/", 1)[1].split(".")[0])
        key = route if appid is None else f"{route}:{appid}"
        with self._mu:
            self.paths.append(u.path)
            if key:
                self.hits[key] = self.hits.get(key, 0) + 1
        if route == "persona":
            players = [{"personaname": self.persona}] if self.persona else []
            return self._send(h, 200, {"response": {"players": players}})
        if route == "owned":
            return self._send(h, 200, {"response": {"game_count": len(self.games), "games": self.games}})
        if route == "deck":
            self.started.set()
            if self.gate:
                self.gate.wait(30)
            canned = self._canned(self.deck, appid, key)
            if canned:
                return self._send(h, *canned)
            return self._send(h, 200, {"success": 1, "results": {"resolved_category": 3}})
        if route == "details":
            canned = self._canned(self.details, appid, key)
            if canned:
                return self._send(h, *canned)
            return self._send(h, 200, {str(appid): {"success": True, "data": {
                "genres": [{"id": "1", "description": "Action"}, {"id": "23", "description": "Indie"}],
                "metacritic": {"score": 85},
                "release_date": {"coming_soon": False, "date": "12 Mar, 2020"},
                "categories": [{"id": 28, "description": "Full controller support"}],
                "publishers": ["Test Publisher Inc."],
                "developers": ["Test Studio"],
            }}})
        if route == "taglist":
            with self._mu:
                canned = self.taglist[min(self.hits[key], len(self.taglist)) - 1] if self.taglist else None
            return self._send(h, *canned) if canned else self._send(h, 200, {"response": {
                "version_hash": "1", "tags": [{"tagid": i, "name": n} for i, n in DEFAULT_TAG_NAMES.items()]}})
        if route == "tags":
            req = json.loads(q["input_json"][0])
            asked = [i["appid"] for i in req["ids"]]
            with self._mu:
                self.tag_batches.append(asked)
                canned = self.tags_batch[min(self.hits[key], len(self.tags_batch)) - 1] if self.tags_batch else None
            if canned:
                return self._send(h, *canned)
            items = []
            for a in asked:
                if a in self.tags_omit:
                    continue
                items.append({"item_type": 0, "id": a, "success": 1, "visible": True, "appid": a, "name": f"App {a}",
                              "tagids": self.tags.get(a, DEFAULT_TAG_IDS),
                              "tags": [{"tagid": t, "weight": 100 - i} for i, t in enumerate(self.tags.get(a, DEFAULT_TAG_IDS))]})
            return self._send(h, 200, {"response": {"store_items": items}})
        if route == "reviews":
            canned = self._canned(self.reviews, appid, key)
            if canned:
                return self._send(h, *canned)
            return self._send(h, 200, {"success": 1, "query_summary": {
                "num_reviews": 0, "review_score": 8, "review_score_desc": "Very Positive",
                "total_positive": 90, "total_negative": 10, "total_reviews": 100}, "reviews": []})
        if route == "proton":
            canned = self._canned(self.proton, appid, key)
            if canned:
                return self._send(h, *canned)
            return self._send(h, 200, {"tier": "gold", "total": 120})
        self._send(h, 404, {"error": "unknown route in MockSteam"})

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body=b"", headers: dict[str, str] | None = None) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)


# Real Steam tag ids and names (IStoreService/GetTagList), in the order a game's tags would come back.
DEFAULT_TAG_NAMES = {19: "Action", 122: "RPG", 492: "Indie", 1628: "Metroidvania", 1695: "Open World",
                     1716: "Roguelike", 3871: "2D", 3959: "Roguelite", 3964: "Pixel Graphics", 4026: "Difficult",
                     4182: "Singleplayer", 29482: "Souls-like"}
DEFAULT_TAG_IDS = [29482, 4026, 122]  # Souls-like, Difficult, RPG


def default_owned_games() -> list[dict]:
    """What GetOwnedGames returns: deliberately out of name order, to check the exporter sorts."""
    return [
        {"appid": 2003, "name": "Test Game Charlie", "playtime_forever": 600, "playtime_deck_forever": 120,
         "rtime_last_played": 1710000000},
        {"appid": 2001, "name": "test game alpha", "playtime_forever": 0},
        {"appid": 2004, "name": "Test Game Delta", "playtime_forever": 5, "rtime_last_played": 1700000000},
        {"appid": 2002, "name": "Test Game Bravo", "playtime_forever": 45, "playtime_deck_forever": 10,
         "rtime_last_played": 1720000000},
    ]


# ------------------------------------------------------------------ running the exporter

def exporter_env(mock: MockSteam | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, "PYTHONPATH": str(NO_HLTB), "PYTHONUNBUFFERED": "1"}
    for k in ("STEAM_API_KEY", "STEAM_PROFILE", "STEAM_API_BASE", "STEAM_STORE_BASE", "PROTONDB_BASE"):
        env.pop(k, None)
    if mock:
        env.update(mock.env())
    env.update(extra or {})
    return env


def run_exporter(workdir: Path, args: list[str], mock: MockSteam | None, *, sleep_log: Path | None = None,
                 extra_env: dict[str, str] | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """Run steam_backlog_export.py through tests/exporter_shim.py (which skips real sleeps) with
    howlongtobeatpy hidden. Returns the CompletedProcess (text mode); never raises on a non-zero exit."""
    env = exporter_env(mock, extra_env)
    if sleep_log:
        env["SHIM_SLEEP_LOG"] = str(sleep_log)
    cmd = [sys.executable, str(SHIM), str(EXPORTER), *args]
    return subprocess.run(cmd, cwd=str(workdir), env=env, capture_output=True, text=True, timeout=timeout)
