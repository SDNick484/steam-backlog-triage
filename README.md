# Steam Backlog Triage

A self-hosted dashboard for working through a Steam backlog, with an eye on what plays well on a Steam Deck.

An exporter pulls your Steam library and enriches every game with:
- **Steam Deck compatibility**: Valve's Verified / Playable / Unsupported rating
- **HowLongToBeat times**: main story and main + extras
- **Store details** (optional): genres, Metacritic score, release year, controller support, publishers and developers
- **Steam user reviews** (optional, with the store details): review count, percent positive and Steam's label ("Very Positive")
- **ProtonDB**: the community compatibility tier (Native to Borked, or Pending) and how many reports it's based on

The dashboard then shows:
- **Library overview:** how much of your library you've played, and a grid of unplayed games by Deck rating and length. Click a cell to filter.
- **Couch picks:** the six best backlog candidates for the Deck, ranked by how little you've played them, Deck rating, length and Metacritic score.
- **Triage lanes:** mark games *Next*, *Someday*, *Skip* or *Done*. The marks are saved on the server, so every device sees the same lists.
- **Notes and order** (self-hosted only): add a note to any game, and reorder the *Up next* lane by dragging rows or with the *Move up* and *Move down* buttons. Both are saved on the server in `notes.json` and `order.json`.
- **The full list:** search, plus a filter panel for genre (any or all), HowLongToBeat length (main, extras or completionist), ProtonDB tier, Metacritic minimum, release year, Deck status, controller support, play state, triage lane and notes. Each filter has an *include unknown* option for games with no data. Filters combine with AND, and the state is kept in the URL, so a filtered view can be bookmarked. Couch picks respect the filters. There are also filters for publisher and developer (spellings such as "Devolver Digital" and "DEVOLVER DIGITAL, Inc." are grouped), a Metacritic maximum, Steam user review levels, and *Only unknown* checkboxes that list the games with no data for a field.
- **Statistics:** the *Stats* link in the header (or `/stats`) shows the same library as charts: games, time played and never-launched share by genre, publisher, developer, Deck rating, ProtonDB tier, controller support, length, triage lane, Metacritic, Steam reviews and release year. It uses the same view, search and filters as the list, and the filters stay in the URL when you move between the two. Switch between games, hours played and % never launched; each row's strip splits its games into never launched, tried (under 2 h) and played (2 h or more). Click any row, "Unknown" rows included, to open the list with exactly those games. A game with several genres, publishers or developers counts under each of them. *Copy as CSV* copies a card's table. An export made before publishers, developers or reviews were collected still works: those cards say how many games they know, and fill in after the next `--details` run.

Everything is standard-library Python, plus the optional [`howlongtobeatpy`](https://pypi.org/project/howlongtobeatpy/) package for HowLongToBeat times. The page is a single HTML file with no build step.

## Files

| File | What it does |
|---|---|
| `steam_backlog_export.py` | Exports your library to `steam_backlog.json`, caching lookups so later runs are fast |
| `backlog_server.py` | Serves the dashboard and its small JSON API |
| `backlog.html` | The dashboard page |
| `SCORING.md` | How the couch score is computed (the spec other tools implement) |
| `run-export.sh` | Wrapper for cron/systemd and the dashboard's **Refresh library** button; reads your keys from `~/.steam-env` and takes the export lock |
| `install/alpine.sh` | Installer for Alpine Linux (OpenRC); works well in a small Proxmox LXC |
| `contrib/systemd/` | Service and timer units for systemd hosts |
| `tests/`, `.github/` | Tests and CI (see [Tests and CI](#tests-and-ci)) |

## What you need

- **A Steam Web API key**, from <https://steamcommunity.com/dev/apikey>. Any domain name works for the form.
- **Your SteamID64**, the 17-digit number starting with `7656119`. Find it in the Steam client under your name → **Account details**. A custom profile URL works too.
- **A public profile**, with **Game details** set to Public (Steam → Edit Profile → Privacy Settings). Otherwise the API returns an empty library.

## Quick start (any Linux)

```sh
pip install --user howlongtobeatpy          # optional, for HowLongToBeat times
export STEAM_API_KEY=your_key
python3 steam_backlog_export.py --steamid 7656119XXXXXXXXXX --details --out ~/exports/steam_backlog.json
python3 backlog_server.py --data ~/exports   # then open http://localhost:8484/
```

The first export takes a while for a big library:
- **Store details** (`--details`): Steam allows about 200 lookups per 5 minutes, so roughly 50 minutes per 1,800 games.
- **Steam user reviews** (`--details`): one extra request per game to the store's review summary (about one per second, no key needed), so roughly 30 more minutes per 1,800 games on the first run. Results are cached for 30 days, so later runs only fetch games that are new or whose entry has expired. A failed request is not cached and is retried on the next run. If five requests in a row fail (Steam rate-limits the store, usually after a long run of store-detail requests), the exporter stops the reviews step, says so in its log and carries on; run it again later, and it fetches only the games it is missing.
- **ProtonDB**: about 0.3 s per game, cached for 7 days, so a first run for 1,800 games takes about 9 minutes.
- **HowLongToBeat:** about half a second per game.

Results are cached in `steam_backlog_cache.json`. Later runs only fetch games that are new or whose cached entries have expired. `--limit 20` is handy for a quick test.

## Install on Alpine (OpenRC)

```sh
git clone https://github.com/SDNick484/steam-backlog-triage.git
cd steam-backlog-triage
STEAM_PROFILE=7656119XXXXXXXXXX STEAM_API_KEY=your_key sh install/alpine.sh
```

The installer sets up:
- an unprivileged `steam` user
- the code in `/opt/steam-backlog-triage`, with a venv for HowLongToBeat lookups
- your keys in `/home/steam/.steam-env` (mode 600)
- a weekly export (Mondays 04:15), added as its own marked block in the `steam` user's crontab
- the dashboard as an OpenRC service on port 8484

Re-running the installer updates the code and keeps your settings and data. It also remembers the options below, so pass one again only to change it. Other options:
- `PORT`: the dashboard's port.
- `EXPORT_SCHEDULE`: a cron expression for the export.
- `TZ_NAME`: sets the system time zone.
- `NAV=Label=URL`: adds a header link, for example to [Steam Wishlist Deals](https://github.com/SDNick484/steam-wishlist-deals).

Start the first export without waiting for the schedule:

```sh
su steam -c /opt/steam-backlog-triage/run-export.sh
```

## systemd hosts

Copy the files in `contrib/systemd/` to `/etc/systemd/system/` and adjust the paths and user to match your install. Put `STEAM_API_KEY` and `STEAM_PROFILE` in the service user's `~/.steam-env`, then run:

```sh
systemctl daemon-reload
systemctl enable --now backlog-triage backlog-export.timer
```

## Refresh library

When the server can run exports, the dashboard header shows a **Refresh library** button. It starts the exporter on the server, shows its phase and progress while it runs, and loads the new library when it finishes. Your filters and triage marks stay as they are. If an export is already running (for example the weekly cron one), the button shows that and waits for it. It is hidden when the page isn't served by `backlog_server.py` or the server has no export command. Exports take as long as they take: a first run for a large library can be an hour or more, and later runs only fetch what's new or expired.

## Header links

When the page is served by `backlog_server.py` its header always shows **Backlog** and **Stats** links, followed by any extra links given with `--nav LABEL=URL` (for example `--nav "Deals=https://deals.example.com/"`). The Backlog and Stats links carry your current filters.

## Security

The server has no login.
- **Keep it private:** bind it to your LAN or a VPN (Tailscale, WireGuard), or put it behind an authenticating reverse proxy such as Cloudflare Tunnel + Access.
- **Don't expose it directly.** Anyone who can reach it can change triage marks or replace the library.
- **Keys stay on the server.** They live only in `~/.steam-env` and are never sent to the page.

## API

Every response carries `X-Backlog-Server: 1` and `X-Backlog-Api: 2`.

| Method | Path | |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/stats` (also `/stats/`) | The same page, showing its Statistics view |
| GET / PUT | `/api/library` | The current export (PUT replaces it; used by the page's **Load export** button) |
| GET / PUT | `/api/triage` | Triage marks; PUT `{"appid": 620, "lane": "next"}` (`""` clears) |
| GET / PUT | `/api/notes` | Notes; PUT `{"appid": 620, "note": "..."}` (`""` clears; up to 2,000 characters) |
| GET / PUT | `/api/order` | Up next order; PUT `{"order": [620, 1145360]}` (distinct app ids) |
| POST | `/api/export` | Start an export in the background: 202, or 409 if one is already running. The request needs `Content-Type: application/json` (415 otherwise) |
| GET | `/api/export/status` | The running or last export: `state`, `phase`, `progress`, recent redacted log lines, and whether the library changed |
| GET | `/healthz` | `ok` |

**ETags.** The library, triage, notes and order responses carry an `ETag`, and send `304 Not Modified` when you send a matching `If-None-Match`. A PUT to triage, notes or order may send `If-Match` with an ETag from an earlier GET; if the resource has changed since, the server answers `412` with the current ETag and writes nothing. Without `If-Match` a PUT works as it always did, which is how the page uses them. Each successful PUT also returns the value it replaced as `previous`.

**Exports.** `POST /api/export` runs `run-export.sh` (or `--export-cmd PATH`) as a child process, one run at a time. The server never sees your keys; `run-export.sh` reads `~/.steam-env` itself, so the server's user needs a `HOME` that points at the directory holding it. A run takes a lock on `<data>/.export.lock`, so a cron export counts as a running export too: the status shows it with `origin: "external"`, and a POST gets 409. Cron and the server must therefore use the same data directory. Log lines in the status are redacted (SteamID, key parameters, paths, IP addresses); the full output stays in `export-last.log` in the data directory (mode 600). A run over `--export-timeout` seconds (default 10800) is stopped with SIGINT, then SIGTERM, then SIGKILL, so the exporter can save its cache. A server with no export command answers 501.

## Claude artifact mode

Notes and the Up next order are only saved by the self-hosted server. The artifact copy shows the lanes and the library but has no note or reorder controls.

`backlog.html` can also be published as a [Claude](https://claude.ai) artifact. When it isn't served by `backlog_server.py`, it stores the library and triage marks in the artifact's database. It also offers an "Ask Claude for a pick" box. Self-hosted, these features simply stay off.

## Data sources

- Steam Web API: library and playtime.
- Steam store: Steam Deck compatibility and store details.
- ProtonDB: community Proton compatibility summaries. This is an unofficial endpoint, so missing or unexpected data is shown as unknown.
- HowLongToBeat, through `howlongtobeatpy`, which uses an unofficial API that can change. If it breaks, exports still complete without length times.

This project isn't affiliated with Valve, HowLongToBeat or any store.

## Tests and CI

The product needs only the Python standard library. The tests use `unittest`, and CI adds test-only tools (node, Playwright, shellcheck) that are never installed on a server.

```sh
# Server routes, export endpoints, exporter (against a mock Steam), run-export.sh, privacy scanner, score parity
python3 -m unittest -v tests.test_server tests.test_export_endpoints tests.test_exporter \
  tests.test_run_export tests.test_privacy_scan tests.test_score_vectors tests.test_stats

# The page in a real browser (pip install -r tests/browser/requirements.txt && playwright install chromium)
python3 -m unittest -v tests.browser.test_page tests.browser.test_filters tests.browser.test_stats_page

# The Alpine installer, stubbed (changes /etc and /opt: run it in a container)
docker run --rm -v "$PWD":/src:ro python:3.12-alpine sh /src/tests/installer/check.sh

# shellcheck, py_compile, systemd-analyze verify
sh .github/scripts/lint.sh
```

What each part checks:

- **Server tests** cover every route in the API table above, using [`docs/backlog-api-contract.md`](https://github.com/SDNick484/mcp-server-steam-backlog/blob/main/docs/backlog-api-contract.md) in the MCP server's repository as the expected behavior (the contract text is quoted in each test's docstring).
- **Export endpoint tests** use stub scripts for `--export-cmd`: 202, 409 while running (including a lock held by another process), exit codes, timeouts, and redaction of a fake key, an IPv4 address and a SteamID in the status lines.
- **Exporter tests** run the real exporter against a local mock of the Steam API, the store and ProtonDB (`STEAM_API_BASE`, `STEAM_STORE_BASE`, `PROTONDB_BASE`), with `--limit`, without `howlongtobeatpy`, cache reuse, ProtonDB garbage, and a 429 followed by success. Sleeps are replaced by a recorder, so the suite never waits.
- **Score parity** runs the page's `score()` under node against `tests/score_vectors.json`. In CI a missing node is a failure, not a skip.
- **Page test** loads `backlog.html` in Chromium with no console errors, checks that lanes, the Up next order and notes persist across a second load, and that **Refresh library** runs a stub export and reloads.
- **Installer test** runs `install/alpine.sh` in `python:3.12-alpine` twice (and a third time with one option changed): `~/.steam-env` and the saved options are kept, there is exactly one `BEGIN`/`END` cron block, and unrelated cron lines survive.
- **MCP contract tests**: CI checks out [`mcp-server-steam-backlog`](https://github.com/SDNick484/mcp-server-steam-backlog) at a pinned commit and runs its `tests/test_contract.py` against this checkout's server (`BACKLOG_CHECKOUT`, `BACKLOG_REQUIRE_REAL=1`). A weekly run does the same against that repo's `main`. Bump `MCP_PINNED_REF` in `.github/workflows/ci.yml` when the contract changes.

CI runs on pushes to `main`, pull requests, weekly, and on demand. The unit tests run on Python 3.11 to 3.14; the other jobs use one version. Every third-party action is pinned to a full commit SHA with its version in a comment, and Dependabot updates them weekly.

### Privacy scan

This repository is public, so a CI job (`.github/scripts/privacy_scan.py`) fails if a tracked file or a commit contains a SteamID, an IPv4 address that isn't loopback, documentation (RFC 5737) or private (RFC 1918), an email that isn't a `noreply` or `example` address, or any term from a private denylist. CI logs are public too, so the scanner prints only the file, line and rule, never the matched text. Files under `tests/fixtures/` may contain IP addresses, but never SteamIDs.

The denylist holds things no pattern can know (your hostnames, domain, name, time zone, device names). It lives in a repository secret, so it is never committed. To add it:

1. On GitHub, open this repository and choose **Settings**.
2. In the left sidebar, choose **Secrets and variables**, then **Actions**.
3. Under **Repository secrets**, choose **New repository secret**.
4. Name it exactly `PRIVACY_DENYLIST`.
5. In **Secret**, enter one term per line. Matching ignores case and looks at file contents, file names and commit messages. Lines starting with `#` are ignored.
6. Choose **Add secret**.

Until it exists, the job runs the pattern checks only and shows a warning. Pull requests from forks, and Dependabot's, can't read the secret and get the same warning. To check locally: `PRIVACY_DENYLIST="$(cat my-terms.txt)" python3 .github/scripts/privacy_scan.py`. A line can opt out with the text `privacy-scan: allow` (say why beside it).

## License

MIT
