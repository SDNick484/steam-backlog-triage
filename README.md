# Steam Backlog Triage

A self-hosted dashboard for working through a Steam backlog, with an eye on what plays well on a Steam Deck.

An exporter pulls your Steam library and enriches every game with:
- **Steam Deck compatibility**: Valve's Verified / Playable / Unsupported rating
- **HowLongToBeat times**: main story and main + extras
- **Store details** (optional): genres, Metacritic score, release year, controller support, publishers and developers
- **Steam user reviews** (optional, with the store details): review count, percent positive and Steam's label ("Very Positive")
- **Store tags** (optional, with the store details): the labels players put on a game, such as Souls-like, Metroidvania or Roguelike. Each game keeps Steam's top 10 user tags by vote weight, in Steam's order, in a `tags` list of names (an empty list when unknown or when `--details` wasn't used). This sits beside `genres`, which is Valve's fixed list (Action, RPG, Indie, ...); neither replaces the other.
- **ProtonDB**: the community compatibility tier (Native to Borked, or Pending) and how many reports it's based on

The dashboard then shows:
- **Library overview:** how much of your library you've played, and a grid of unplayed games by Deck rating and length. Click a cell to filter.
- **Couch picks:** the six best backlog candidates for the Deck, ranked by how little you've played them, Deck rating, length and Metacritic score.
- **Triage lanes:** mark games *Next*, *Someday*, *Skip* or *Done*. The marks are saved on the server, so every device sees the same lists.
- **Notes and order** (self-hosted only): add a note to any game, and reorder the *Up next* lane by dragging rows or with the *Move up* and *Move down* buttons. Both are saved on the server in `notes.json` and `order.json`.
- **The full list:** search, plus a filter panel for genre (any or all), HowLongToBeat length (main, extras or completionist), ProtonDB tier, Metacritic minimum, release year, Deck status, controller support, play state, triage lane and notes. Each filter has an *include unknown* option for games with no data. Filters combine with AND, and the state is kept in the URL, so a filtered view can be bookmarked. Couch picks respect the filters. There are also filters for publisher and developer (spellings such as "Devolver Digital" and "DEVOLVER DIGITAL, Inc." are grouped), a Metacritic maximum, Steam user review levels, a **Tags** filter (a searchable multi-select; a game with several tags matches any of the ones you pick) and *Only unknown* checkboxes that list the games with no data for a field.
- **Statistics:** the *Stats* link in the header (or `/stats`) shows the same library as charts: games, time played and never-launched share by genre, publisher, developer, Deck rating, ProtonDB tier, controller support, length, triage lane, Metacritic, Steam reviews, release year and **Tags**. It uses the same view, search and filters as the list, and the filters stay in the URL when you move between the two. Switch between games, hours played and % never launched; each row's strip splits its games into never launched, tried (under 2 h) and played (2 h or more). Click any row, "Unknown" rows included, to open the list with exactly those games. A game with several genres, publishers, developers or tags counts under each of them. *Copy as CSV* copies a card's table. An export made before publishers, developers, reviews or tags were collected still works: those cards say how many games they know ("Tags known for N of M"), and fill in after the next `--details` run.

**Tag matching is forgiving about spelling.** Tags are grouped and matched by a key: lower case, letters and digits only, so "Souls-like", "Soulslike" and "souls like" are one tag, shown as Steam spells it (the most common spelling wins the label). A short alias table also maps "Soulsborne" to Souls-like. The tag pickers on the list and Stats pages search the same way, and the URL accepts any spelling (`#tag=soulslike`). Real tags that differ by more than spelling stay separate: Roguelike and Roguelite are two tags. A game with no tag data is "unknown" for the filter: it never matches a tag, and *Include unknown* or *Only unknown* brings it in.

Everything is standard-library Python, plus the optional [`howlongtobeatpy`](https://pypi.org/project/howlongtobeatpy/) package for HowLongToBeat times. The page is a single HTML file with no build step.

## Files

| File | What it does |
|---|---|
| `steam_backlog_export.py` | Exports your library to `steam_backlog.json`, caching lookups so later runs are fast |
| `backlog_server.py` | Serves the dashboard and its small JSON API |
| `backlog.html` | The dashboard page |
| `backlog_query.py`, `access_jwt.py`, `ask.py` | Ask (AI search): the read-only backlog queries, Cloudflare Access token checks, and the question loop. Imported by the server at start-up, so keep them next to `backlog_server.py` |
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
- **Steam user reviews** (`--details`): one extra request per game to the store's review summary (no key needed). The store rate-limits this endpoint hard, so requests are spaced 3 seconds apart: roughly 95 minutes per 1,900 games on the first run. Results are cached for 30 days, so later runs only fetch games that are new or whose entry has expired. A failed request is not cached, is not retried within the run (retrying while Steam is refusing requests only prolongs the block) and is tried again on the next run. If five requests in a row fail, the exporter stops the reviews step, says so in its log and carries on. Steam's block can last hours; before running again, check that `https://store.steampowered.com/appreviews/620?json=1&num_per_page=0` shows JSON in a browser on the same network. Each run fetches only the games it is missing.
- **Store tags** (`--details`): fetched in batches of 50 games per request from the store's `IStoreBrowseService/GetItems` (no key needed), plus one `IStoreService/GetTagList` request that turns tag ids into names. Requests are spaced 1.6 seconds apart, so 1,900 games is about 39 requests, roughly a minute. Tags are cached for 30 days (the tag-name list too, as one entry), so later runs only fetch games that are new or whose entry has expired. A failed batch logs a warning, is not cached and is tried again on the next run; it never stops the export. **After upgrading**, the first `--details` run fetches tags for every game once (about the number of games divided by 50, plus one, extra requests); watch for the `Store tags:` lines in the log. The page keeps the top 10 tags per game (`TAGS_MAX` in the exporter).
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
- an empty settings file for [Ask](#ai-search-ask) at `/home/steam/.config/backlog-triage/ask.env` (mode 600), created once and never overwritten
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
- **Keys stay on the server.** They live only in `~/.steam-env` (Steam) and `ask.env` (Ask) and are never sent to the page. The export never sees the Anthropic key.

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

## AI search (Ask)

**Ask** lets you question your backlog in plain language ("short Deck Verified games I've never launched", "what in Up next runs well on Linux?") at `https://steam.example.com/ask`. It answers with the matching games as the same rows the list shows, so you can move them between lanes and edit notes from there. A small **Ask** pill appears next to the page title when it is on. Ask is off until an API key is set, and it only exists on the self-hosted server (not in the Claude artifact copy).

Ask can filter and count by Steam tags ("Metroidvanias under 10 hours", "how many Souls-likes do I own?"). It matches tags as loosely as the page does, and it is told that a game may have no tag data (unknown, not "no tags").

**Setup**

1. In the Anthropic Console, create an API key, ideally in its own workspace with a monthly spend limit.
2. Edit the settings file the installer created and fill in the lines below: `vim /home/steam/.config/backlog-triage/ask.env`. Type the key into the file yourself; don't paste it into a chat, a ticket or a shell command line.
3. Run `rc-service backlog-triage restart`.

The file is `KEY=VALUE` lines; quotes are optional and `#` starts a comment. The server refuses to read it if group or others can read or write it (`chmod 600`). The installer creates it empty and never touches an existing one, so re-running the installer keeps your settings. If you don't use the installer, pass `--ask-env PATH` to `backlog_server.py` (default `~/.config/backlog-triage/ask.env`).

| Setting | Default | Meaning |
|---|---|---|
| `ANTHROPIC_SEARCH_KEY` | empty (Ask is off) | API key |
| `ANTHROPIC_SEARCH_MODEL` | `claude-haiku-5-5` | Model used to answer |
| `ASK_DAILY_LIMIT` | `200` | Questions per day across everyone |
| `ASK_ACCESS_TEAM` | empty | Your Cloudflare Access team domain, e.g. `your-team.cloudflareaccess.com` |
| `ASK_ACCESS_AUD` | empty | The dashboard's Access application Audience (AUD) tag |
| `ASK_EMAILS` | empty (any user Access lets in) | Comma-separated emails allowed to ask, e.g. `you@example.com` |
| `ASK_LAN` | `0` | `1` lets direct requests from your LAN ask (see below) |

**Who can ask.** Ask needs to know who is asking, because it spends money. There are two ways in:

- **Through Cloudflare Access** (Tunnel + Access in front of the dashboard). The server checks the signed Access token on each request (RS256 signature, audience, issuer, expiry), so the `Cf-Access-Authenticated-User-Email` header alone is never trusted. Set `ASK_ACCESS_TEAM` and `ASK_ACCESS_AUD`, and optionally `ASK_EMAILS` to limit it to certain people. Find the team domain in Cloudflare Zero Trust under Settings, Custom pages (it is your `your-team.cloudflareaccess.com` name). Find the AUD under Access, Applications, your dashboard application, Overview, Application Audience (AUD) Tag. The application must cover the dashboard's own hostname, e.g. `steam.example.com`.
- **From your LAN**, with `ASK_LAN=1`. A request is accepted only if all of these hold: it comes straight from a private or loopback address, it carries no Cloudflare headers (`Cf-Ray`, `Cf-Connecting-Ip`), and you opened the page by IP address (`http://<server-ip>:8484/ask`), not by a hostname. The hostname rule stops a DNS-rebinding page from reaching Ask. If you put a reverse proxy on the same machine, leave `ASK_LAN` off: its requests arrive from a private address.

With neither set up, Ask answers 401. Leave `ASK_ACCESS_*` empty and `ASK_LAN` at `0` if you want it off even with a key.

**Limits.** A thread holds at most 5 questions (start a new question to clear it). Each question runs at most 5 rounds of lookups and 60 seconds, and at most 2 questions run at once (others get a "busy" answer you can retry). The daily cap counts questions since local midnight in the server's time zone; when it is reached Ask answers "daily limit reached" until the next day. `ASK_DAILY_LIMIT=0` turns every question away.

**Cost** (Claude Haiku 5.5, model id `claude-haiku-5-5`, at $0.10 / $0.50 per MTok input / output for prompts up to 100k tokens, checked 2026-11-07 at platform.claude.com/docs/en/about-claude/pricing and platform.claude.com/docs/en/models/overview):

- **Typical question** (2 to 3 rounds, about 10k input and 600 output tokens in total): about $0.0013.
- **Worst case:** one question can reach roughly 100 to 150k input tokens (up to 5 rounds, each resending about 3.7k tokens of instructions and tools, with several tool calls per round and each result up to 12,000 characters) and about 10k output tokens: about $0.015 to $0.02. The 40k-token forced-finish rule is checked only between rounds, so it does not cap this. Each round's prompt stays well under 100k tokens, so the base price applies.
- **At the default cap of 200 a day:** about $0.26 a day typically, and at most about $4 a day if every question hit the worst case ($0.02 x 200).

Prices change; check the pricing page, and the daily cap and a spend limit on the Console workspace are what bound spend.

**Data sent to Anthropic** (api.anthropic.com only): each question and the results of the lookups the model makes, which can include game names, lanes, your Up next order, your notes, playtimes and library facts. Never your SteamID, profile name, file paths or keys. Your API key stays on the server: it is read into memory, never logged and never sent to the browser.

**Log.** `ask_log.jsonl` in the data directory gets one JSON line per question: `ts`, `who`, `rounds`, `input_tokens`, `output_tokens`, `dropped_ids` and `status`. The question text is never stored. Entries older than 90 days are removed.

## Claude artifact mode

Notes and the Up next order are only saved by the self-hosted server. The artifact copy shows the lanes and the library but has no note or reorder controls.

`backlog.html` can also be published as a [Claude](https://claude.ai) artifact. When it isn't served by `backlog_server.py`, it stores the library and triage marks in the artifact's database. It also offers an "Ask Claude for a pick" box. Self-hosted, these features simply stay off.

## Data sources

- Steam Web API: library and playtime.
- Steam store: Steam Deck compatibility and store details, and user tags through the store's `GetItems` and `GetTagList` services. These are the store front end's own calls, not a documented API, and `GetItems` only returns tags when asked with `include_tag_count` (`include_tag_ids` is silently ignored), so a change on Steam's side would show as games with no tags, not as a failed export.
- ProtonDB: community Proton compatibility summaries. This is an unofficial endpoint, so missing or unexpected data is shown as unknown.
- HowLongToBeat, through `howlongtobeatpy`, which uses an unofficial API that can change. If it breaks, exports still complete without length times.

This project isn't affiliated with Valve, HowLongToBeat or any store.

## Tests and CI

The product needs only the Python standard library. The tests use `unittest`, and CI adds test-only tools (node, Playwright, shellcheck) that are never installed on a server.

```sh
# Server routes, export endpoints, exporter (against a mock Steam), run-export.sh, privacy scanner, score parity
python3 -m unittest -v tests.test_server tests.test_export_endpoints tests.test_exporter \
  tests.test_run_export tests.test_privacy_scan tests.test_score_vectors tests.test_stats \
  tests.test_backlog_query tests.test_access_jwt tests.test_ask

# The page in a real browser (pip install -r tests/browser/requirements.txt && playwright install chromium)
python3 -m unittest -v tests.browser.test_page tests.browser.test_filters tests.browser.test_stats_page \
  tests.browser.test_ask_page

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
- **Installer test** runs `install/alpine.sh` in `python:3.12-alpine` twice (and a third time with one option changed): `~/.steam-env`, `ask.env` and the saved options are kept, the Ask modules are installed and import, there is exactly one `BEGIN`/`END` cron block, and unrelated cron lines survive.
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
