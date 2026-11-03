#!/usr/bin/env python3
"""
steam_backlog_export.py - export your Steam library for the Backlog Triage dashboard.

What it collects for every game you own:
  * name, total playtime, Steam Deck playtime, last-played time   (Steam Web API)
  * Steam Deck compatibility: Verified / Playable / Unsupported     (Steam store)
  * HowLongToBeat main / main+extra / completionist hours           (optional, howlongtobeatpy)
  * genres, Metacritic score, release year, controller support     (optional, --details)

Everything it looks up is cached in steam_backlog_cache.json, so the first run is the
slow one and later runs only fetch games that are new or whose cache entry has expired.
Ctrl-C is safe: progress is saved as it goes.

Requirements: Python 3.8+. Standard library only, plus `pip install howlongtobeatpy`
if you want HowLongToBeat times (the script skips them with a warning otherwise).

Usage:
  export STEAM_API_KEY=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX   # https://steamcommunity.com/dev/apikey
  python3 steam_backlog_export.py --profile https://steamcommunity.com/id/yourname/
  python3 steam_backlog_export.py --steamid 7656119XXXXXXXXXX --details
  python3 steam_backlog_export.py --vanity yourname --skip-hltb

Your Steam profile's "Game details" privacy setting must be Public, or the Web API
returns an empty library.

Output: steam_backlog.json (load it into the dashboard with "Load export").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# Overridable so the script can be tested against a local mock server.
API_BASE = os.environ.get("STEAM_API_BASE", "https://api.steampowered.com")
STORE_BASE = os.environ.get("STEAM_STORE_BASE", "https://store.steampowered.com")

USER_AGENT = "steam-backlog-export/1.0 (+personal use)"

# Cache lifetimes, in days. Deck ratings change occasionally; HLTB times drift slowly.
TTL_DECK = 21
TTL_HLTB = 90
TTL_DETAILS = 90

DECK_LABELS = {0: "Unknown", 1: "Unsupported", 2: "Playable", 3: "Verified"}

# Steam store category ids that describe controller support.
CAT_FULL_CONTROLLER = 28
CAT_PARTIAL_CONTROLLER = 18


# --------------------------------------------------------------------------- HTTP

class RateLimiter:
    """Allow at most one request per `interval` seconds across all threads."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


def http_json(url: str, limiter: RateLimiter | None = None, retries: int = 4):
    """GET a URL and parse JSON. Backs off on 429/5xx. Returns None on 4xx other than 429."""
    delay = 5.0
    for attempt in range(retries + 1):
        if limiter:
            limiter.wait()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                if not body:
                    return None
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                if attempt == retries:
                    raise
                wait = float(e.headers.get("Retry-After") or delay)
                log(f"  HTTP {e.code} from {urllib.parse.urlparse(url).netloc}; waiting {wait:.0f}s")
                time.sleep(wait)
                delay = min(delay * 2, 120)
                continue
            if e.code in (401, 403) and "api.steampowered.com" in url:
                raise SystemExit(
                    "Steam rejected the API key (HTTP %d). Check STEAM_API_KEY / --key." % e.code
                )
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == retries:
                log(f"  giving up on {url.split('?')[0]}: {e}")
                return None
            time.sleep(delay)
            delay = min(delay * 2, 120)
    return None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- Steam Web API

def resolve_steamid(key: str, profile: str | None, vanity: str | None, steamid: str | None) -> str:
    if steamid:
        if not re.fullmatch(r"\d{17}", steamid):
            raise SystemExit("--steamid should be the 17-digit SteamID64 (starts with 7656119).")
        return steamid
    if profile:
        m = re.search(r"/profiles/(\d{17})", profile)
        if m:
            return m.group(1)
        m = re.search(r"/id/([^/?#]+)", profile)
        if not m:
            raise SystemExit("Couldn't read a SteamID or vanity name from --profile URL.")
        vanity = m.group(1)
    if not vanity:
        raise SystemExit("Give one of --profile, --vanity or --steamid.")
    q = urllib.parse.urlencode({"key": key, "vanityurl": vanity})
    data = http_json(f"{API_BASE}/ISteamUser/ResolveVanityURL/v1/?{q}")
    resp = (data or {}).get("response", {})
    if resp.get("success") != 1:
        raise SystemExit(f"Steam couldn't resolve the vanity name '{vanity}'.")
    return resp["steamid"]


def get_persona(key: str, steamid: str) -> str | None:
    q = urllib.parse.urlencode({"key": key, "steamids": steamid})
    data = http_json(f"{API_BASE}/ISteamUser/GetPlayerSummaries/v2/?{q}")
    players = (data or {}).get("response", {}).get("players", [])
    return players[0].get("personaname") if players else None


def get_owned_games(key: str, steamid: str) -> list[dict]:
    q = urllib.parse.urlencode({
        "key": key,
        "steamid": steamid,
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "format": "json",
    })
    data = http_json(f"{API_BASE}/IPlayerService/GetOwnedGames/v1/?{q}")
    resp = (data or {}).get("response", {})
    games = resp.get("games")
    if games is None:
        raise SystemExit(
            "Steam returned no games. Set your profile's 'Game details' privacy to Public "
            "and try again (it can take a few minutes to take effect)."
        )
    return games


# --------------------------------------------------------------------------- Store lookups

def fetch_deck_category(appid: int, limiter: RateLimiter) -> int | None:
    q = urllib.parse.urlencode({"nAppID": appid, "l": "english"})
    data = http_json(f"{STORE_BASE}/saleaction/ajaxgetdeckappcompatibilityreport?{q}", limiter)
    if data is None:
        return None  # network trouble: don't cache, try again next run
    results = data.get("results") or {}
    cat = results.get("resolved_category")
    return int(cat) if isinstance(cat, int) and cat in DECK_LABELS else 0


def fetch_details(appid: int, limiter: RateLimiter) -> dict | None:
    q = urllib.parse.urlencode({"appids": appid, "l": "english", "cc": "us"})
    data = http_json(f"{STORE_BASE}/api/appdetails?{q}", limiter)
    if data is None:
        return None
    entry = data.get(str(appid)) or {}
    if not entry.get("success"):
        return {"genres": [], "metacritic": None, "year": None, "controller": None}
    d = entry.get("data") or {}
    cats = {c.get("id") for c in d.get("categories") or []}
    controller = "full" if CAT_FULL_CONTROLLER in cats else "partial" if CAT_PARTIAL_CONTROLLER in cats else None
    year = None
    m = re.search(r"(19|20)\d{2}", (d.get("release_date") or {}).get("date") or "")
    if m:
        year = int(m.group(0))
    return {
        "genres": [g.get("description") for g in d.get("genres") or [] if g.get("description")],
        "metacritic": (d.get("metacritic") or {}).get("score"),
        "year": year,
        "controller": controller,
    }


# --------------------------------------------------------------------------- HowLongToBeat

_TRADEMARKS = re.compile(r"[™®©]")
_EDITION = re.compile(
    r"\s*[-:]?\s*\b(game of the year|goty|definitive|complete|enhanced|remastered|anniversary|"
    r"director'?s cut|deluxe|ultimate|gold|special|standard)\b.*$",
    re.IGNORECASE,
)


def hltb_query_names(name: str) -> list[str]:
    base = _TRADEMARKS.sub("", name).strip()
    names = [base]
    trimmed = _EDITION.sub("", base).strip()
    if trimmed and trimmed != base:
        names.append(trimmed)
    return names


def fetch_hltb(hltb, name: str) -> dict | None:
    """Return {'main','extra','complete','match'} in hours, or {} when nothing matched well."""
    best = None
    for query in hltb_query_names(name):
        try:
            results = hltb.search(query, similarity_case_sensitive=False)
        except Exception as e:  # the library scrapes an unofficial API; be forgiving
            log(f"  HLTB error for {name!r}: {e}")
            return None
        for r in results or []:
            if best is None or r.similarity > best.similarity:
                best = r
        if best is not None and best.similarity >= 0.85:
            break
    if best is None or best.similarity < 0.6:
        return {}

    def hours(v):
        return round(float(v), 1) if v else None

    return {
        "main": hours(best.main_story),
        "extra": hours(best.main_extra),
        "complete": hours(best.completionist),
        "match": best.game_name,
    }


# --------------------------------------------------------------------------- Cache

class Cache:
    def __init__(self, path: str, refresh: bool):
        self.path = path
        self.data: dict = {"deck": {}, "hltb": {}, "details": {}}
        self._lock = threading.Lock()
        self._dirty = 0
        if os.path.exists(path) and not refresh:
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                for k in self.data:
                    self.data[k] = loaded.get(k, {})
            except (OSError, json.JSONDecodeError):
                log(f"Cache {path} unreadable; starting fresh.")

    def get(self, kind: str, appid: int, ttl_days: int):
        e = self.data[kind].get(str(appid))
        if not e:
            return None
        if time.time() - e.get("t", 0) > ttl_days * 86400:
            return None
        return e

    def put(self, kind: str, appid: int, value) -> None:
        with self._lock:
            self.data[kind][str(appid)] = {"t": int(time.time()), "v": value}
            self._dirty += 1
            if self._dirty >= 25:
                self._save_locked()

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, separators=(",", ":"))
        os.replace(tmp, self.path)
        self._dirty = 0


# --------------------------------------------------------------------------- Main

def run_parallel(label: str, appids: list[int], fn, workers: int) -> None:
    if not appids:
        log(f"{label}: everything cached.")
        return
    log(f"{label}: fetching {len(appids)} games...")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fn, a) for a in appids]
        for _ in as_completed(futures):
            done += 1
            if done % 50 == 0 or done == len(appids):
                log(f"  {done}/{len(appids)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Export your Steam library for the Backlog Triage dashboard.")
    who = ap.add_argument_group("which account")
    who.add_argument("--profile", help="Profile URL, e.g. https://steamcommunity.com/id/yourname/")
    who.add_argument("--vanity", help="Custom URL name (the part after /id/)")
    who.add_argument("--steamid", help="17-digit SteamID64")
    ap.add_argument("--key", default=os.environ.get("STEAM_API_KEY"), help="Steam Web API key (default: $STEAM_API_KEY)")
    ap.add_argument("--out", default="steam_backlog.json", help="Output file (default: %(default)s)")
    ap.add_argument("--cache", default="steam_backlog_cache.json", help="Cache file (default: %(default)s)")
    ap.add_argument("--skip-hltb", action="store_true", help="Don't look up HowLongToBeat times")
    ap.add_argument("--details", action="store_true",
                    help="Also fetch genres, Metacritic, release year, controller support "
                         "(store API allows ~200 lookups per 5 min, so the first run is slow)")
    ap.add_argument("--refresh", action="store_true", help="Ignore the cache and fetch everything again")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N games (for a quick test)")
    args = ap.parse_args()

    if not args.key:
        raise SystemExit("Set STEAM_API_KEY or pass --key (get one at https://steamcommunity.com/dev/apikey).")

    steamid = resolve_steamid(args.key, args.profile, args.vanity, args.steamid)
    persona = get_persona(args.key, steamid)
    owned = get_owned_games(args.key, steamid)
    owned.sort(key=lambda g: (g.get("name") or "").lower())
    if args.limit:
        owned = owned[: args.limit]
    log(f"{persona or steamid}: {len(owned)} games")

    cache = Cache(args.cache, args.refresh)
    appids = [g["appid"] for g in owned]
    names = {g["appid"]: g.get("name") or f"App {g['appid']}" for g in owned}

    try:
        # Steam Deck compatibility: light endpoint, ~4 req/s is comfortable.
        deck_limiter = RateLimiter(0.25)

        def do_deck(appid: int) -> None:
            cat = fetch_deck_category(appid, deck_limiter)
            if cat is not None:
                cache.put("deck", appid, cat)

        todo = [a for a in appids if cache.get("deck", a, TTL_DECK) is None]
        run_parallel("Steam Deck compatibility", todo, do_deck, workers=4)

        # Store details: appdetails is limited to roughly 200 requests / 5 minutes.
        if args.details:
            details_limiter = RateLimiter(1.6)

            def do_details(appid: int) -> None:
                d = fetch_details(appid, details_limiter)
                if d is not None:
                    cache.put("details", appid, d)

            todo = [a for a in appids if cache.get("details", a, TTL_DETAILS) is None]
            if todo:
                log(f"  (about {len(todo) * 1.6 / 60:.0f} minutes for store details)")
            run_parallel("Store details", todo, do_details, workers=2)

        # HowLongToBeat: one at a time with a pause, to be a polite guest.
        if not args.skip_hltb:
            try:
                from howlongtobeatpy import HowLongToBeat  # type: ignore
            except ImportError:
                log("HowLongToBeat: skipped (pip install howlongtobeatpy to enable).")
            else:
                hltb = HowLongToBeat()
                todo = [a for a in appids if cache.get("hltb", a, TTL_HLTB) is None]
                if not todo:
                    log("HowLongToBeat: everything cached.")
                else:
                    log(f"HowLongToBeat: looking up {len(todo)} games...")
                failures = 0
                for i, appid in enumerate(todo, 1):
                    res = fetch_hltb(hltb, names[appid])
                    if res is None:
                        failures += 1
                        if failures >= 10 and failures == i:
                            log("  HLTB keeps failing (their site may have changed); skipping the rest.")
                            break
                    else:
                        cache.put("hltb", appid, res)
                    if i % 25 == 0 or i == len(todo):
                        log(f"  {i}/{len(todo)}")
                    time.sleep(0.6)
    except KeyboardInterrupt:
        log("\nInterrupted; writing what's done so far.")
    finally:
        cache.save()

    games = []
    for g in owned:
        a = g["appid"]
        deck = cache.get("deck", a, 10**6)
        hl = cache.get("hltb", a, 10**6)
        det = cache.get("details", a, 10**6)
        hv = hl["v"] if hl else None
        games.append({
            "appid": a,
            "name": names[a],
            "playtime": int(g.get("playtime_forever") or 0),
            "deckPlaytime": int(g.get("playtime_deck_forever") or 0),
            "lastPlayed": int(g.get("rtime_last_played") or 0),
            "deck": deck["v"] if deck else 0,
            "hltb": {k: hv.get(k) for k in ("main", "extra", "complete")} if hv else None,
            "genres": (det["v"].get("genres") if det else None) or [],
            "metacritic": det["v"].get("metacritic") if det else None,
            "year": det["v"].get("year") if det else None,
            "controller": det["v"].get("controller") if det else None,
        })

    out = {
        "schema": SCHEMA_VERSION,
        "source": "steam_backlog_export.py",
        "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steamid": steamid,
        "persona": persona,
        "games": games,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    n = len(games)
    counts = {label: sum(1 for x in games if x["deck"] == k) for k, label in DECK_LABELS.items()}
    unplayed = sum(1 for x in games if x["playtime"] == 0)
    with_hltb = sum(1 for x in games if x["hltb"] and x["hltb"].get("main"))
    log(f"\nWrote {args.out}: {n} games, {unplayed} never launched, {with_hltb} with HLTB times.")
    log("Deck: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v))
    log(f"Size: {os.path.getsize(args.out) / 1024:.0f} KB. Load it in the dashboard with 'Load export'.")


if __name__ == "__main__":
    main()
