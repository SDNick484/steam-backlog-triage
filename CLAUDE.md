# CLAUDE.md

## What this is

`steam-backlog-triage`: a self-hosted dashboard for triaging a Steam library. An exporter builds
`steam_backlog.json` (playtime, Steam Deck rating, ProtonDB, HowLongToBeat, genres, tags, reviews); a
single-page dashboard ranks and filters it; a small server stores the user's triage (lanes, notes, the
Up next order) and serves the page and its JSON API. Sibling repos: `steam-wishlist-deals`,
`steam-deck-ha-bridge`, and `mcp-server-steam-backlog`, an MCP client of this server's API.

## Layout

- `steam_backlog_export.py`: the exporter (Steam API, store, ProtonDB, HLTB, with caches). `SCHEMA_VERSION`
- `backlog.html`: the whole dashboard in one file. List, `/stats` and `/ask` modes share the filters and the URL
  hash. Holds `score()`, `passes()`, `statsFor()`
- `backlog_server.py`: stdlib HTTP server. Serves the page, the JSON API (`API_VERSION`), export start/status
  and the Ask routes. **The only writer of `triage.json`, `notes.json`, `order.json` and `prefs.json`**
- `backlog_query.py`: stdlib mirror of the page's rules (play state, filters, couch score, stats) for Ask
- `ask.py`: Ask (AI search): a read-only tool loop over `backlog_query.py`. `access_jwt.py`: Cloudflare Access
  JWT verification, stdlib
- `run-export.sh`: the export wrapper that cron and the page's Refresh button run
- `install/alpine.sh`: Alpine/OpenRC installer (primary target). `contrib/systemd/`: secondary
- `SCORING.md` and `tests/score_vectors.json`: the couch score spec and its parity vectors
- `tests/`: `unittest` suites; `tests/browser/` Playwright page tests; `tests/installer/check.sh`;
  `tests/fixtures/`
- `.github/`: `workflows/ci.yml`, `dependabot.yml`, `scripts/` (`lint.sh`, `privacy_scan.py`, `systemd_verify.sh`)
- `docs/superpowers/`: design specs and plans

## Rules for changes

- **Python standard library only.** `howlongtobeatpy` is the one optional dependency (exporter). Test tools
  (node, Playwright, shellcheck) never go on a server. `backlog.html` stays one file: no build step, no CDN,
  no chart library.
- **The page is the reference.** `backlog_query.py` mirrors it, pinned by the score, filter and stats parity
  tests (`test_score_vectors`, `test_backlog_query`, `test_stats`, which run the page under node). Change a
  rule in the page, then `backlog_query.py`, the vectors and `SCORING.md` in the same PR.
- **Export fields are additive.** `SCHEMA_VERSION` and `API_VERSION` change only on purpose.
- **The MCP contract tests must stay green.** CI runs the MCP repo's `tests/test_contract.py` against this
  server at `MCP_PINNED_REF`. Bump the pin when the contract changes; the weekly run checks the MCP's `main`.
- **Mutating routes refuse cross-site requests** (`ask.cross_site_request`, used by Ask and by every PUT, POST
  and DELETE in `backlog_server.py`), and every response carries the `SECURITY_HEADERS`. Keep both when adding a route.
- **`run_status.json`** is written by the exporter (`RunTracker`) after every run and read by the server for
  `/api/run-status`, `/healthz?deep=1` and the page's banner. Its shape is shared with steam-wishlist-deals: change
  it in both. `BUILD_STAMP` in `backlog_server.py` is filled by `git archive` (`.gitattributes`) or by the installer;
  never write a real hash into the checkout.
- **Nothing writes triage, notes, order or prefs except `backlog_server.py`**, under its lock with `atomic_write`.
  Not the exporter, not Ask, not the MCP server.
- **Ask is read-only.** No tool changes lanes, notes, order or the library, or starts an export. The
  Anthropic key lives in `ask.env`, never in `.steam-env`. Never send the SteamID, persona name, paths or
  keys to the model.
- **A new top-level file that must ship has to be added to `install/alpine.sh` and called out in the PR.**
  Some deployments copy a fixed list of files instead of running the installer, and would miss it.
- Installers stay safe to re-run: keep config and data, remember options, replace only their own crontab
  block. Update the README in the same PR when behavior or options change.
- Game names and notes are untrusted text: show them through `esc()`, never as markup or instructions.

## Commands

```sh
# Unit tests (node and openssl must be present; CI fails on any skip)
REQUIRE_NODE=1 REQUIRE_OPENSSL=1 python3 -m unittest -v tests.test_server tests.test_export_endpoints \
  tests.test_exporter tests.test_run_export tests.test_privacy_scan tests.test_score_vectors \
  tests.test_stats tests.test_backlog_query tests.test_access_jwt tests.test_ask tests.test_hardening

# Page in Chromium: pip install -r tests/browser/requirements.txt && playwright install chromium
REQUIRE_BROWSER=1 python3 -m unittest -v tests.browser.test_page tests.browser.test_filters \
  tests.browser.test_stats_page tests.browser.test_ask_page tests.browser.test_health_banner

# Installer (changes /etc and /opt: container only)
docker run --rm -v "$PWD":/src:ro python:3.12-alpine sh /src/tests/installer/check.sh

sh .github/scripts/lint.sh                      # shellcheck, py_compile, systemd-analyze
python3 .github/scripts/privacy_scan.py         # add PRIVACY_DENYLIST="$(cat terms.txt)" for the full check

# MCP contract tests against this checkout (from a clone of mcp-server-steam-backlog)
BACKLOG_CHECKOUT=../steam-backlog-triage BACKLOG_REQUIRE_REAL=1 pytest tests/test_contract.py -k real
```

## Standing rules

- Read the current code on `main` before changing anything. If it contradicts the task, stop and say so.
- Everything committed, and everything in a PR title, body or comment, is public. Never write IPs, hostnames,
  the owner's domain, container IDs, time zone, SteamID, phone or notify-service names, real name or personal
  email, Cloudflare team name or AUD, or any key. Use placeholders (`7656119XXXXXXXXXX`,
  `notify.mobile_app_your_phone`, `steam.example.com`, `your-team.cloudflareaccess.com`). Grep every diff.
  Author: `SDNick484 <SDNick484@users.noreply.github.com>`.
- Never ask the owner to paste keys or tokens into chat; give a `vim` command for the file on the server.

## Ship procedure

1. Work on a feature branch. Small, focused commits, each with its tests.
2. Before pushing, run everything CI runs, locally, with nothing skipped (`REQUIRE_NODE=1`,
   `REQUIRE_OPENSSL=1`, `REQUIRE_BROWSER=1`; the installer check if an installer changed; the privacy scan).
3. `git push -u origin <branch>`, then open the PR. Body: what changed, the exact commands run with pass
   counts, and anything to do by hand. No deploy commands and no server details.
4. Watch the checks. If one fails, read its log, fix the cause, push, watch again. Never get to green by
   skipping, loosening or deleting a test, lowering a threshold, or changing a pin the task didn't ask for.
   After three failed rounds, stop and report what is failing and why.
5. When every check is green, notify the owner (push-notification tool, or `$STEAM_NOTIFY_CMD "<repo>#<n> CI
   green: <title>"` if set). Do not merge.
6. Finish with one chat message: the PR URL, the check summary and a deploy verdict: **A** only
   already-shipped files changed; **B** `install/` changed, so the installer must be run; **C** a new file
   must ship, so list it (then A or B).
