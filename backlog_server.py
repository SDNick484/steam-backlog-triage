#!/usr/bin/env python3
"""
backlog_server.py - serve the Backlog Triage dashboard.

Standard library only. Serves:
  GET  /                the dashboard page
  GET  /stats           the same page, showing its Statistics view (also /stats/)
  GET  /ask             the same page, showing Ask (also /ask/)
  GET  /api/library     the latest steam_backlog.json (404 until the first export)
  PUT  /api/library     replace it (the page's "Load export" button)
  GET  /api/triage      {appid: lane} for Next / Someday / Skip / Done marks
  PUT  /api/triage      {"appid": 620, "lane": "next"}  ("" clears)
  GET  /api/notes       {appid: note} for per-game notes
  PUT  /api/notes       {"appid": 620, "note": "..."}  ("" clears; max 2000 characters)
  GET  /api/order       [appid, ...] order of the Up next lane
  PUT  /api/order       {"order": [620, 1145360, ...]}  (a list of distinct app ids)
  POST /api/export      start a library export (run-export.sh) in the background
  GET  /api/export/status   the running or last export: state, phase, progress, recent log lines
  GET  /api/ask/status {"ask": true, "turns": 5} when this request may use Ask, else {"ask": false}
  POST /api/ask         {"thread": [{"q": "..."}, ...]}: a plain-language question about the library, answered
                        by Claude over read-only tools (ask.py; off until ask.env has a key)
  GET  /healthz         "ok"

Notes and order live in notes.json and order.json next to triage.json.

Every response carries X-Backlog-Server: 1 and X-Backlog-Api: 2 (the API version). GET
/api/library, /api/triage, /api/notes and /api/order send an ETag and honor If-None-Match,
answering 304 when it still matches. Their PUTs honor an optional If-Match (412 if the
resource changed since that ETag) and return the previous value; with no If-Match they
behave as they always did.

There is no authentication (Ask aside, which needs a Cloudflare Access login or, with ASK_LAN=1, a
direct LAN request). Bind it to your LAN or a VPN, or put it behind an
authenticating proxy (for example Cloudflare Access); don't expose it directly.

Exports run the --export-cmd command (default: run-export.sh next to this file) as a child
process, one at a time. That includes runs started by cron: they hold the same lock file
(<data>/.export.lock), so the server sees them as running too. The export never sees a key:
run-export.sh reads ~/.steam-env itself. Ask's Anthropic key is read from ask.env into memory, sent
only to api.anthropic.com, and never logged or sent to the browser.

Usage:
  backlog_server.py [--port 8484] [--bind 0.0.0.0] [--data DIR] [--page backlog.html]
                    [--nav "Deals=https://deals.example.com/"] ...
                    [--export-cmd PATH] [--export-timeout SECONDS] [--ask-env PATH]
"""

from __future__ import annotations

import argparse
import collections
import datetime
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ask
import backlog_query
from access_jwt import Verifier

# Bumped only for changes a client must know about. API 1 is the original server; API 2 adds
# ETags, conditional PUTs, `previous` values and the export routes. Clients read it from the
# X-Backlog-Api response header (no header means API 1).
API_VERSION = "2"
LANES = {"", "next", "someday", "skip", "done"}
MAX_BODY = 32 * 1024 * 1024
NOTE_MAX = 2000
ORDER_MAX = 5000
POST_MAX = 64 * 1024            # POST /api/export takes no real body; anything bigger is a mistake
EXPORT_TIMEOUT = 3 * 3600       # default wall-clock limit for one server-started export
STOP_GRACE = 30                 # seconds between SIGINT, SIGTERM and SIGKILL on a timed-out export
RING_LINES = 200                # output lines kept in memory per run
SHOWN_LINES = 20                # ...of which the status object carries the last few
LINE_MAX = 300

# backlog.html is written as a page body (it can also be published as a Claude artifact,
# whose viewer supplies the document shell). Wrap it in a minimal document here.
SKELETON_HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    "<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);"
    "padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px/1.5 system-ui,"
    "-apple-system,'Segoe UI',sans-serif}img{max-width:100%}[hidden]{display:none!important}"
    "</style>{config}</head><body>"
)
SKELETON_TAIL = "</body></html>"


def atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dump(obj) -> bytes:
    """The JSON bytes the API sends. ETags are computed over exactly these."""
    return json.dumps(obj).encode()


def etag_of(body: bytes) -> str:
    """Strong validator: the first 32 hex characters of SHA-256 over the exact bytes served."""
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


ETAG_TOKEN = re.compile(r'(?:W/)?"[^"]*"|\*')


def none_match(header: str | None, etag: str) -> bool:
    """True if an If-None-Match value matches `etag` under weak comparison (W/"x" matches "x").

    The value is a comma-separated list of entity tags, or `*`. Anything that doesn't parse
    simply doesn't match, so the client gets a normal 200 rather than an error.
    """
    if not header:
        return False
    for tok in ETAG_TOKEN.findall(header):
        if tok == "*" or (tok[2:] if tok.startswith("W/") else tok) == etag:
            return True
    return False


def config_script(cfg: dict) -> str:
    """Expose server settings (page links) to the page as window.APP_CONFIG."""
    js = json.dumps(cfg).replace("</", "<\\/")
    return f"<script>window.APP_CONFIG={js};</script>"


class App:
    def __init__(self, page: str, data_dir: str, nav: list[dict]):
        self.page = page
        self.data_dir = os.path.abspath(data_dir)
        self.library = os.path.join(data_dir, "steam_backlog.json")
        self.triage = os.path.join(data_dir, "triage.json")
        self.notes = os.path.join(data_dir, "notes.json")
        self.order = os.path.join(data_dir, "order.json")
        self.head = SKELETON_HEAD.replace("{config}", config_script({"nav": nav}))
        self.lock = threading.Lock()
        # The library is 1-2 MB, so its ETag is remembered per (inode, mtime, size). Every writer
        # replaces the file atomically (new inode), which makes that key safe.
        self._lib_mu = threading.Lock()
        self._lib_key: tuple[int, int, int] | None = None
        self._lib_etag: str | None = None
        # main() replaces this with one that has an export command; until then exports are off.
        self.exporter = Exporter(self, None, EXPORT_TIMEOUT)
        # Normalized games for Ask, keyed by the library's ETag: (etag, games, exportedAt).
        self._ask_mu = threading.Lock()
        self._ask_games: tuple[str, list, str | None] | None = None
        # main() replaces this with one built from ask.env; until then Ask is off.
        self.ask = ask.Ask(None, self.data_dir, self.ask_snapshot)

    def library_etag_and_body(self, want_body: bool) -> tuple[str, bytes | None]:
        """Return (etag, body) for the library; body is None only if it wasn't needed and the ETag
        was already known. Raises FileNotFoundError before the first export."""
        with open(self.library, "rb") as f:
            st = os.fstat(f.fileno())
            key = (st.st_ino, st.st_mtime_ns, st.st_size)
            with self._lib_mu:
                etag = self._lib_etag if key == self._lib_key else None
            if etag is not None and not want_body:
                return etag, None
            body = f.read()
        if etag is None:
            etag = etag_of(body)
            with self._lib_mu:
                self._lib_key, self._lib_etag = key, etag
        return etag, body

    def ask_snapshot(self) -> backlog_query.Snapshot | None:
        """The library as Ask's tools see it, or None before the first export. Games are normalized once per
        ETag; triage, notes and order are read under the same lock their writers take. The SteamID and persona
        are never copied in."""
        try:
            etag, body = self.library_etag_and_body(False)
            with self._ask_mu:
                cached = self._ask_games if self._ask_games and self._ask_games[0] == etag else None
            if cached is None:
                if body is None:
                    etag, body = self.library_etag_and_body(True)
                data = json.loads(body or b"")
                games = data.get("games") if isinstance(data, dict) else None
                if not isinstance(games, list):
                    return None
                exported = data.get("exportedAt")
                cached = (etag, [backlog_query.normalize(g, i) for i, g in enumerate(games) if isinstance(g, dict)],
                          exported if isinstance(exported, str) else None)
                with self._ask_mu:
                    self._ask_games = cached
        except (OSError, ValueError):  # no export yet, or one that can't be read
            return None
        with self.lock:
            triage, notes, order = self.read_triage(), self.read_notes(), self.read_order()
        ids = lambda m: {int(k): v for k, v in (m.items() if isinstance(m, dict) else ())
                         if isinstance(k, str) and k.isascii() and k.isdigit() and isinstance(v, str) and v}
        return backlog_query.Snapshot(
            games=list(cached[1]),
            triage={k: v for k, v in ids(triage).items() if v in LANES},
            notes=ids(notes),
            order=[a for a in order if is_app_id(a)] if isinstance(order, list) else [],
            exported_at=cached[2])

    def page_bytes(self) -> bytes:
        with open(self.page, "rb") as f:
            return self.head.encode() + f.read() + SKELETON_TAIL.encode()

    def read_triage(self) -> dict:
        return read_json(self.triage, {})

    def read_notes(self) -> dict:
        return read_json(self.notes, {})

    def read_order(self) -> list:
        return read_json(self.order, [])


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def is_app_id(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------- exports
# What the exporter prints is shown to the person looking at the page (and to API clients), so
# each line is cleaned before it is kept. The full, unredacted output goes to <data>/export-last.log
# for whoever administers the box and never crosses the API.
CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # every control character but tab
PROFILE_LINE = re.compile(r"(.+): (\d+) games")     # "<persona or SteamID>: 1874 games"
STEAMID = re.compile(r"7656119\d{10}")
SECRET_PARAM = re.compile(r"(?i)((?:^|[?&\s])(?:key|apikey|api_key|token|access_token|password)=)[^&\s\"']*")
HOME_PATH = re.compile(r"/home/[^/\s]+")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")
PROGRESS = re.compile(r"^\s+(\d+)/(\d+)$")


def redact_line(line: str, data_dir: str, persona: str | None = None) -> str:
    """Clean one line of exporter output, in this order: control characters; the profile line
    (it names the persona or SteamID); SteamIDs; the persona; secrets in URL query strings;
    the data directory and other home directories; IPv4 addresses; length. Game names and
    store hostnames stay: they're useful and not personal."""
    line = CONTROL.sub("", line)
    m = PROFILE_LINE.fullmatch(line)
    if m:
        line = f"<profile>: {m.group(2)} games"
    line = STEAMID.sub("7656119XXXXXXXXXX", line)
    if persona:
        line = line.replace(persona, "<persona>")
    line = SECRET_PARAM.sub(r"\1<redacted>", line)
    for d in sorted({data_dir.rstrip("/"), os.path.realpath(data_dir).rstrip("/")}, key=len, reverse=True):
        if len(d) > 1:
            line = line.replace(d, "<data>")
    line = HOME_PATH.sub("~", line)
    line = IPV4.sub("<ip>", line)
    return line if len(line) <= LINE_MAX else line[: LINE_MAX - 1] + "…"


def detect_phase(line: str) -> str | None:
    """Which part of the export a (redacted) output line announces, or None. Best-effort status
    for a person, not a protocol: these are the exporter's own messages, so if it changes them
    this table changes with it, and an unknown line just leaves the phase alone."""
    s = TIMESTAMP.sub("", line, count=1)
    if s.startswith("starting export"):
        return "starting"
    if re.fullmatch(r".+: \d+ games", s):
        return "library"
    if s.startswith("Steam Deck compatibility:"):
        return "deck"
    if s.startswith(("Store details:", "  (about ")):
        return "details"
    if s.startswith("ProtonDB:"):
        return "proton"
    if s.startswith("HowLongToBeat:"):
        return "hltb"
    if s.startswith("Wrote "):
        return "writing"
    return None


def iso(dt: datetime.datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


class Run:
    """One export started by this server."""

    def __init__(self, persona: str | None, etag_start: str | None):
        self.started = datetime.datetime.now(datetime.timezone.utc)
        self.id = self.started.strftime("%Y%m%dT%H%M%SZ")
        self.t0 = time.monotonic()
        self.t1: float | None = None
        self.finished: datetime.datetime | None = None
        self.done = False
        self.lines: collections.deque[str] = collections.deque(maxlen=RING_LINES)
        self.phase: str | None = None
        self.progress: dict | None = None
        self.exit_code: int | None = None
        self.reason: str | None = None
        self.persona = persona
        self.etag_start = etag_start
        self.etag_end: str | None = None


class Exporter:
    """Runs the export command one at a time and reports on it.

    "Running" means this server's own child is alive, or some other process holds the export
    lock (<data>/.export.lock; run-export.sh takes it, so a cron run counts). The server only ever
    probes that lock, for an instant; the child holds it for its whole life. Everything that
    decides "can I start?" or builds a status object happens under one mutex, so a status read
    never sees half an update and two POSTs can't both spawn."""

    def __init__(self, app: "App", cmd: str | None, timeout: int):
        self.app = app
        self.cmd = cmd
        self.timeout = timeout
        self.mu = threading.Lock()
        self.run: Run | None = None
        self.lock_path = os.path.join(app.data_dir, ".export.lock")
        self.log_path = os.path.join(app.data_dir, "export-last.log")
        self._meta_mu = threading.Lock()
        self._meta: tuple[str, str | None, str | None] | None = None  # (etag, exportedAt, persona)

    @property
    def configured(self) -> bool:
        return self.cmd is not None

    def lock_held(self) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        finally:
            os.close(fd)  # closing releases the probe lock at once
        return False

    def library_info(self) -> tuple[str | None, str | None, str | None]:
        """(etag, exportedAt, persona) of the library as it is now. The file is parsed only when
        its ETag changes, so polling the status doesn't re-read a 2 MB file every few seconds."""
        etag: str | None = None
        try:
            etag, body = self.app.library_etag_and_body(False)
            with self._meta_mu:
                if self._meta and self._meta[0] == etag:
                    return self._meta
            if body is None:
                etag, body = self.app.library_etag_and_body(True)
            data = json.loads(body or b"")
            exported = data.get("exportedAt") if isinstance(data, dict) else None
            persona = data.get("persona") if isinstance(data, dict) else None
            meta = (etag, exported if isinstance(exported, str) else None,
                    persona if isinstance(persona, str) and persona else None)
        except FileNotFoundError:
            return None, None, None
        except (OSError, ValueError):
            return etag, None, None  # a library we can't read still has a tag worth reporting
        with self._meta_mu:
            self._meta = meta
        return meta

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        with self.mu:
            return self._status()

    def _status(self) -> dict:
        etag, exported, _ = self.library_info()
        r = self.run
        if r is not None and not r.done:
            return self._of_run(r, "running", etag, exported)
        if self.lock_held():  # somebody else's export: cron, a shell, or one from before a restart
            return self._bare("running", "external", etag, exported)
        if r is not None:
            ok = r.exit_code == 0 and r.reason is None
            return self._of_run(r, "succeeded" if ok else "failed", etag, exported)
        return self._bare("idle", None, etag, exported)

    @staticmethod
    def _bare(state: str, origin: str | None, etag: str | None, exported: str | None) -> dict:
        return {"state": state, "origin": origin, "id": None, "startedAt": None, "finishedAt": None,
                "elapsedSeconds": None, "phase": None, "progress": None, "exitCode": None,
                "reason": None, "lines": [], "libraryEtag": etag, "libraryChanged": None,
                "exportedAt": exported}

    @staticmethod
    def _of_run(r: Run, state: str, etag: str | None, exported: str | None) -> dict:
        elapsed = (r.t1 if r.t1 is not None else time.monotonic()) - r.t0
        return {"state": state, "origin": "server", "id": r.id, "startedAt": iso(r.started),
                "finishedAt": iso(r.finished), "elapsedSeconds": int(elapsed), "phase": r.phase,
                "progress": dict(r.progress) if r.progress else None, "exitCode": r.exit_code,
                "reason": r.reason, "lines": list(r.lines)[-SHOWN_LINES:], "libraryEtag": etag,
                "libraryChanged": (r.etag_end != r.etag_start) if r.done else None,
                "exportedAt": exported}

    # ------------------------------------------------------------ starting a run
    def start(self) -> tuple[str, dict | None]:
        """("started" | "busy", status), or ("error", None) if the command couldn't be spawned."""
        with self.mu:
            if (self.run is not None and not self.run.done) or self.lock_held():
                return "busy", self._status()
            etag, _, persona = self.library_info()
            run = Run(persona, etag)
            try:
                # No shell, no stdin, stdout and stderr merged. The environment is the server's
                # own plus where to write; keys are never passed (run-export.sh reads ~/.steam-env).
                proc = subprocess.Popen([self.cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        env={**os.environ, "EXPORT_DIR": self.app.data_dir},
                                        # A server started in the background (nohup, supervisors) can have
                                        # SIGINT set to "ignore", and children inherit that; the exporter's
                                        # clean stop needs the default back.
                                        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
            except OSError as e:
                sys.stderr.write(f"export: could not start {self.cmd}: {e}\n")
                return "error", None
            self.run = run
            threading.Thread(target=self._supervise, args=(run, proc), daemon=True).start()
            return "started", self._status()

    def _open_log(self):
        try:  # truncated at the start of each run; mode 0600 since it is unredacted
            fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            return os.fdopen(fd, "w", encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _read(self, run: Run, proc: subprocess.Popen, log) -> None:
        for raw in iter(lambda: proc.stdout.readline(65536), b""):
            text = raw.decode("utf-8", "replace")
            if log is not None:
                try:
                    log.write(text if text.endswith("\n") else text + "\n")
                    log.flush()
                except OSError:
                    log = None  # a full disk shouldn't stop the export or the status
            line = text.rstrip("\r\n")
            if not line.strip():
                continue
            line = redact_line(line, self.app.data_dir, run.persona)
            phase, m = detect_phase(line), PROGRESS.match(line)
            with self.mu:
                run.lines.append(line)
                if phase is not None and phase != run.phase:
                    run.phase, run.progress = phase, None
                if m:
                    run.progress = {"done": int(m.group(1)), "total": int(m.group(2))}

    @staticmethod
    def _stop(proc: subprocess.Popen) -> int:
        """SIGINT first (the exporter catches it, saves its cache and writes what it has), then
        SIGTERM after STOP_GRACE seconds, then SIGKILL after another STOP_GRACE."""
        for sig, grace in ((signal.SIGINT, STOP_GRACE), (signal.SIGTERM, STOP_GRACE), (signal.SIGKILL, None)):
            try:
                proc.send_signal(sig)
            except OSError:
                pass  # already gone
            try:
                return proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
        return proc.wait()

    def _supervise(self, run: Run, proc: subprocess.Popen) -> None:
        log = self._open_log()
        reader = threading.Thread(target=self._read, args=(run, proc, log), daemon=True)
        reader.start()
        timed_out = False
        try:
            rc = proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            rc = self._stop(proc)
        reader.join(timeout=5)  # a grandchild holding the pipe open mustn't wedge the status
        etag_end = self.library_info()[0]
        with self.mu:
            run.exit_code = rc
            # A run the server stopped is "timeout" whatever the exporter then exited with: it
            # catches SIGINT and writes a partial library, which is exit 0 but not a full export.
            run.reason = ("timeout" if timed_out else None if rc == 0 else
                          "not_configured" if rc == 78 else "busy" if rc == 75 else "error")
            run.etag_end = etag_end
            run.finished = datetime.datetime.now(datetime.timezone.utc)
            run.t1 = time.monotonic()
            run.done = True
        if log is not None:
            log.close()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "backlog-server/1.0"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def send_response(self, code, message=None):
            # Here rather than in send() so that every response carries them, including the
            # standard library's own error pages (bad request line, unsupported method, ...).
            super().send_response(code, message)
            # The page checks X-Backlog-Server to know it's self-hosted (vs. a Claude artifact).
            self.send_header("X-Backlog-Server", "1")
            self.send_header("X-Backlog-Api", API_VERSION)

        def send(self, status: int, body: bytes = b"", ctype: str = "application/json",
                 headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            if status != 304:  # a 304 has no body, and no length for the 200 it stands in for
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and status != 304:
                self.wfile.write(body)

        def json(self, status: int, obj, headers: dict[str, str] | None = None) -> None:
            self.send(status, dump(obj), headers=headers)

        def small_resource(self, read) -> None:
            """GET for triage, notes and order: ETag over the body about to be sent, read under
            the same lock the writers take, so the tag and the body can't disagree."""
            with app.lock:
                body = dump(read())
            etag = etag_of(body)
            if none_match(self.headers.get("If-None-Match"), etag):
                return self.send(304, headers={"ETag": etag})
            self.send(200, body, headers={"ETag": etag})

        def if_match_ok(self, current: str) -> bool:
            """No If-Match header: always fine (today's behavior). Otherwise strong comparison
            against the resource's current ETag; `*` matches, and a W/ tag never does."""
            header = self.headers.get("If-Match")
            if header is None:
                return True
            return any(tok == "*" or tok == current for tok in ETAG_TOKEN.findall(header))

        def precondition_failed(self, current: str) -> None:
            self.json(412, {"error": "precondition failed", "etag": current}, headers={"ETag": current})

        def drain_body(self) -> None:
            """Read and drop a small request body, so an early error reply isn't a reset connection."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if 0 < n <= POST_MAX:
                self.rfile.read(n)

        def export_status(self) -> None:
            if not app.exporter.configured:
                return self.json(501, {"error": "export not configured on this server"})
            self.json(200, app.exporter.status())

        def refuse_cross_site(self) -> bool:
            """Answer 403 and return True when the browser says another site sent this request."""
            if not ask.cross_site_request(self.headers):
                return False
            self.drain_body()
            self.json(403, {"error": "cross-site request refused"})
            return True

        def ask_post(self) -> None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > POST_MAX:
                self.drain_body()
                return self.json(400, {"error": ask.INVALID})
            raw = self.rfile.read(n) if n else b""
            status, body, extra = app.ask.handle(self.headers, self.client_address[0],
                                                 self.headers.get("Content-Type") or "", raw)
            self.json(status, body, headers=extra)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/ask":
                return self.ask_post()  # Ask runs the same cross-site check as its first step
            if self.refuse_cross_site():
                return
            if path != "/api/export":
                self.drain_body()
                return self.json(404, {"error": "not found"})
            if not app.exporter.configured:
                self.drain_body()
                return self.json(501, {"error": "export not configured on this server"})
            # Require a JSON content type. A plain HTML form can't send one cross-site, so a page on
            # another site can't start an export through the browser's existing login (this server
            # has no authentication of its own; Cloudflare Access or a VPN is the real gate).
            media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                self.drain_body()
                return self.json(415, {"error": "expected Content-Type: application/json"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > POST_MAX:
                return self.json(400, {"error": "missing or oversized body"})
            raw = self.rfile.read(n) if n else b""
            if raw.strip():  # an empty body or any JSON object; the object is reserved for future options
                try:
                    ok = isinstance(json.loads(raw), dict)
                except ValueError:
                    ok = False
                if not ok:
                    return self.json(400, {"error": "expected a JSON object"})
            outcome, status = app.exporter.start()
            if outcome == "started":
                return self.json(202, status, headers={"Location": "/api/export/status"})
            if outcome == "busy":
                return self.json(409, {"error": "export already running", **status})
            self.json(500, {"error": "could not start the export"})

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                raise ValueError("missing or oversized body")
            return json.loads(self.rfile.read(n))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/stats", "/stats/", "/ask", "/ask/"):
                try:
                    return self.send(200, app.page_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self.json(500, {"error": "dashboard page missing"})
            if path == "/api/library":
                try:
                    etag, body = app.library_etag_and_body(want_body=False)
                    if none_match(self.headers.get("If-None-Match"), etag):
                        return self.send(304, headers={"ETag": etag})
                    if body is None:  # ETag was cached; now fetch the bytes that go with it
                        etag, body = app.library_etag_and_body(want_body=True)
                    return self.send(200, body, headers={"ETag": etag})
                except FileNotFoundError:
                    return self.json(404, {"error": "no export yet"})
            if path == "/api/triage":
                return self.small_resource(app.read_triage)
            if path == "/api/notes":
                return self.small_resource(app.read_notes)
            if path == "/api/order":
                return self.small_resource(app.read_order)
            if path == "/api/export/status":
                return self.export_status()
            if path == "/api/ask/status":
                return self.json(200, app.ask.status(self.headers, self.client_address[0]))
            if path == "/healthz":
                return self.send(200, b"ok", "text/plain")
            self.json(404, {"error": "not found"})

        do_HEAD = do_GET

        def do_DELETE(self):
            if self.refuse_cross_site():
                return
            self.send_error(501, "Unsupported method ('DELETE')")

        def do_PUT(self):
            path = self.path.split("?", 1)[0]
            if self.refuse_cross_site():
                return
            try:
                data = self.body()
            except (ValueError, json.JSONDecodeError) as e:
                return self.json(400, {"error": str(e)})
            if path == "/api/triage":
                if not isinstance(data, dict):
                    return self.json(400, {"error": "expected a JSON object"})
                appid, lane = data.get("appid"), data.get("lane", "")
                if not is_app_id(appid) or not isinstance(lane, str) or lane not in LANES:
                    return self.json(400, {"error": "expected {appid: int, lane: next|someday|skip|done|''}"})
                with app.lock:
                    t = app.read_triage()
                    current = etag_of(dump(t))
                    stale = not self.if_match_ok(current)
                    if not stale:
                        previous = t.get(str(appid), "")
                        if lane:
                            t[str(appid)] = lane
                        else:
                            t.pop(str(appid), None)
                        atomic_write(app.triage, json.dumps(t, separators=(",", ":")).encode())
                        new_etag = etag_of(dump(t))
                if stale:
                    return self.precondition_failed(current)
                return self.json(200, {"ok": True, "appid": appid, "lane": lane, "previous": previous},
                                 headers={"ETag": new_etag})
            if path == "/api/notes":
                if not isinstance(data, dict):
                    return self.json(400, {"error": "expected a JSON object"})
                appid, note = data.get("appid"), data.get("note", "")
                if not is_app_id(appid) or not isinstance(note, str):
                    return self.json(400, {"error": "expected {appid: int, note: string}"})
                note = note.strip()
                if len(note) > NOTE_MAX:
                    return self.json(400, {"error": f"notes are limited to {NOTE_MAX} characters"})
                with app.lock:
                    n = app.read_notes()
                    current = etag_of(dump(n))
                    stale = not self.if_match_ok(current)
                    if not stale:
                        previous = n.get(str(appid), "")
                        if note:
                            n[str(appid)] = note
                        else:
                            n.pop(str(appid), None)
                        atomic_write(app.notes, json.dumps(n, ensure_ascii=False, separators=(",", ":")).encode())
                        new_etag = etag_of(dump(n))
                if stale:
                    return self.precondition_failed(current)
                # `note` is the text actually stored (stripped), which can differ from what was sent.
                return self.json(200, {"ok": True, "appid": appid, "note": note, "previous": previous},
                                 headers={"ETag": new_etag})
            if path == "/api/order":
                order = data.get("order") if isinstance(data, dict) else None
                if not isinstance(order, list) or len(order) > ORDER_MAX or not all(is_app_id(a) for a in order):
                    return self.json(400, {"error": f"expected {{order: [app ids, up to {ORDER_MAX}]}}"})
                seen, clean = set(), []
                for a in order:
                    if a not in seen:
                        seen.add(a)
                        clean.append(a)
                with app.lock:
                    previous = app.read_order()
                    current = etag_of(dump(previous))
                    stale = not self.if_match_ok(current)
                    if not stale:
                        atomic_write(app.order, json.dumps(clean, separators=(",", ":")).encode())
                        new_etag = etag_of(dump(clean))
                if stale:
                    return self.precondition_failed(current)
                return self.json(200, {"ok": True, "count": len(clean), "order": clean, "previous": previous},
                                 headers={"ETag": new_etag})
            if path == "/api/library":
                games = data.get("games") if isinstance(data, dict) else None
                if not isinstance(games, list) or not all(isinstance(g, dict) and "appid" in g for g in games):
                    return self.json(400, {"error": "not a steam_backlog_export.py file"})
                with app.lock:
                    atomic_write(app.library, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
                return self.json(200, {"ok": True, "games": len(games)})
            self.json(404, {"error": "not found"})

    return Handler


def parse_nav(items: list[str]) -> list[dict]:
    out = []
    for item in items:
        label, sep, url = item.partition("=")
        if not sep or not label.strip() or not url.strip():
            raise SystemExit(f"--nav expects LABEL=URL, got {item!r}")
        out.append({"label": label.strip(), "url": url.strip()})
    return out


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Serve the Backlog Triage dashboard.")
    ap.add_argument("--port", type=int, default=8484)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--data", default=os.path.expanduser("~/exports"),
                    help="Directory holding steam_backlog.json and triage.json (default: ~/exports)")
    ap.add_argument("--page", default=os.path.join(here, "backlog.html"))
    ap.add_argument("--nav", action="append", default=[], metavar="LABEL=URL",
                    help="Add a link to the page header, e.g. --nav 'Deals=https://deals.example.com/'")
    ap.add_argument("--export-cmd", metavar="PATH",
                    help="Command that runs one library export, started with no arguments "
                         "(default: run-export.sh next to this file, if it is executable)")
    ap.add_argument("--export-timeout", type=int, default=EXPORT_TIMEOUT, metavar="SECONDS",
                    help="Stop an export that this server started after this long (default: %(default)s)")
    ap.add_argument("--ask-env", metavar="PATH", default=os.path.expanduser("~/.config/backlog-triage/ask.env"),
                    help="Ask settings: the Anthropic key and who may ask (default: %(default)s)")
    args = ap.parse_args()
    if args.export_timeout <= 0:
        raise SystemExit("--export-timeout must be a positive number of seconds")
    cmd = None
    if args.export_cmd:
        cmd = os.path.abspath(args.export_cmd)
        if not (os.path.isfile(cmd) and os.access(cmd, os.X_OK)):
            raise SystemExit(f"--export-cmd {args.export_cmd!r} isn't an executable file")
    else:
        beside = os.path.join(here, "run-export.sh")
        if os.path.isfile(beside) and os.access(beside, os.X_OK):
            cmd = beside
    os.makedirs(args.data, exist_ok=True)
    app = App(args.page, args.data, parse_nav(args.nav))
    app.exporter = Exporter(app, cmd, args.export_timeout)
    settings, ask_line = ask.load_settings(args.ask_env)
    verifier = None
    if settings is not None and settings.access:
        verifier = Verifier(settings.access_team, settings.access_aud, settings.emails)
    app.ask = ask.Ask(settings, app.data_dir, app.ask_snapshot, verifier)
    srv = ThreadingHTTPServer((args.bind, args.port), make_handler(app))
    print(f"Backlog Triage on http://{args.bind}:{args.port}/ (data in {args.data})", file=sys.stderr, flush=True)
    print("Exports: " + (f"enabled ({os.path.basename(cmd)}, {args.export_timeout}s limit)" if cmd
                         else "off (no --export-cmd and no run-export.sh beside the server)"),
          file=sys.stderr, flush=True)
    print(ask_line, file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
