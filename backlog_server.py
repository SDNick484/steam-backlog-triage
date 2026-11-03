#!/usr/bin/env python3
"""
backlog_server.py - serve the Backlog Triage dashboard.

Standard library only. Serves:
  GET  /                the dashboard page
  GET  /api/library     the latest steam_backlog.json (404 until the first export)
  PUT  /api/library     replace it (the page's "Load export" button)
  GET  /api/triage      {appid: lane} for Next / Someday / Skip / Done marks
  PUT  /api/triage      {"appid": 620, "lane": "next"}  ("" clears)
  GET  /healthz         "ok"

There is no authentication. Bind it to your LAN or a VPN, or put it behind an
authenticating proxy (for example Cloudflare Access); don't expose it directly.

Usage:
  backlog_server.py [--port 8484] [--bind 0.0.0.0] [--data DIR] [--page backlog.html]
                    [--nav "Deals=https://deals.example.com/"] ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LANES = {"", "next", "someday", "skip", "done"}
MAX_BODY = 32 * 1024 * 1024

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


def config_script(cfg: dict) -> str:
    """Expose server settings (page links) to the page as window.APP_CONFIG."""
    js = json.dumps(cfg).replace("</", "<\\/")
    return f"<script>window.APP_CONFIG={js};</script>"


class App:
    def __init__(self, page: str, data_dir: str, nav: list[dict]):
        self.page = page
        self.library = os.path.join(data_dir, "steam_backlog.json")
        self.triage = os.path.join(data_dir, "triage.json")
        self.head = SKELETON_HEAD.replace("{config}", config_script({"nav": nav}))
        self.lock = threading.Lock()

    def page_bytes(self) -> bytes:
        with open(self.page, "rb") as f:
            return self.head.encode() + f.read() + SKELETON_TAIL.encode()

    def read_triage(self) -> dict:
        try:
            with open(self.triage, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "backlog-server/1.0"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def send(self, status: int, body: bytes = b"", ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # The page checks for this header to know it's self-hosted (vs. a Claude artifact).
            self.send_header("X-Backlog-Server", "1")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, status: int, obj) -> None:
            self.send(status, json.dumps(obj).encode())

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                raise ValueError("missing or oversized body")
            return json.loads(self.rfile.read(n))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                try:
                    return self.send(200, app.page_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self.json(500, {"error": "dashboard page missing"})
            if path == "/api/library":
                try:
                    with open(app.library, "rb") as f:
                        return self.send(200, f.read())
                except FileNotFoundError:
                    return self.json(404, {"error": "no export yet"})
            if path == "/api/triage":
                with app.lock:
                    return self.json(200, app.read_triage())
            if path == "/healthz":
                return self.send(200, b"ok", "text/plain")
            self.json(404, {"error": "not found"})

        do_HEAD = do_GET

        def do_PUT(self):
            path = self.path.split("?", 1)[0]
            try:
                data = self.body()
            except (ValueError, json.JSONDecodeError) as e:
                return self.json(400, {"error": str(e)})
            if path == "/api/triage":
                appid, lane = data.get("appid"), data.get("lane", "")
                if not isinstance(appid, int) or lane not in LANES:
                    return self.json(400, {"error": "expected {appid: int, lane: next|someday|skip|done|''}"})
                with app.lock:
                    t = app.read_triage()
                    if lane:
                        t[str(appid)] = lane
                    else:
                        t.pop(str(appid), None)
                    atomic_write(app.triage, json.dumps(t, separators=(",", ":")).encode())
                return self.json(200, {"ok": True})
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
    args = ap.parse_args()
    os.makedirs(args.data, exist_ok=True)
    app = App(args.page, args.data, parse_nav(args.nav))
    srv = ThreadingHTTPServer((args.bind, args.port), make_handler(app))
    print(f"Backlog Triage on http://{args.bind}:{args.port}/ (data in {args.data})", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
