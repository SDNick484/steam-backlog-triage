# Steam Backlog Triage

A self-hosted dashboard for working through a Steam backlog, with an eye on what plays well on a Steam Deck.

An exporter pulls your Steam library and enriches every game with:
- **Steam Deck compatibility**: Valve's Verified / Playable / Unsupported rating
- **HowLongToBeat times**: main story and main + extras
- **Store details** (optional): genres, Metacritic score, release year, controller support
- **ProtonDB**: the community compatibility tier (Native to Borked, or Pending) and how many reports it's based on

The dashboard then shows:
- **Library overview:** how much of your library you've played, and a grid of unplayed games by Deck rating and length. Click a cell to filter.
- **Couch picks:** the six best backlog candidates for the Deck, ranked by how little you've played them, Deck rating, length and Metacritic score.
- **Triage lanes:** mark games *Next*, *Someday*, *Skip* or *Done*. The marks are saved on the server, so every device sees the same lists.
- **Notes and order** (self-hosted only): add a note to any game, and reorder the *Up next* lane by dragging rows or with the *Move up* and *Move down* buttons. Both are saved on the server in `notes.json` and `order.json`.
- **The full list:** search, plus a filter panel for genre (any or all), HowLongToBeat length (main, extras or completionist), ProtonDB tier, Metacritic minimum, release year, Deck status, controller support, play state, triage lane and notes. Each filter has an *include unknown* option for games with no data. Filters combine with AND, and the state is kept in the URL, so a filtered view can be bookmarked. Couch picks respect the filters.

Everything is standard-library Python, plus the optional [`howlongtobeatpy`](https://pypi.org/project/howlongtobeatpy/) package for HowLongToBeat times. The page is a single HTML file with no build step.

## Files

| File | What it does |
|---|---|
| `steam_backlog_export.py` | Exports your library to `steam_backlog.json`, caching lookups so later runs are fast |
| `backlog_server.py` | Serves the dashboard and its small JSON API |
| `backlog.html` | The dashboard page |
| `run-export.sh` | Wrapper for cron/systemd that reads your keys from `~/.steam-env` |
| `install/alpine.sh` | Installer for Alpine Linux (OpenRC); works well in a small Proxmox LXC |
| `contrib/systemd/` | Service and timer units for systemd hosts |

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

## Security

The server has no login.
- **Keep it private:** bind it to your LAN or a VPN (Tailscale, WireGuard), or put it behind an authenticating reverse proxy such as Cloudflare Tunnel + Access.
- **Don't expose it directly.** Anyone who can reach it can change triage marks or replace the library.
- **Keys stay on the server.** They live only in `~/.steam-env` and are never sent to the page.

## API

| Method | Path | |
|---|---|---|
| GET | `/` | Dashboard |
| GET / PUT | `/api/library` | The current export (PUT replaces it; used by the page's **Load export** button) |
| GET / PUT | `/api/triage` | Triage marks; PUT `{"appid": 620, "lane": "next"}` (`""` clears) |
| GET / PUT | `/api/notes` | Notes; PUT `{"appid": 620, "note": "..."}` (`""` clears; up to 2,000 characters) |
| GET / PUT | `/api/order` | Up next order; PUT `{"order": [620, 1145360]}` (distinct app ids) |
| GET | `/healthz` | `ok` |

## Claude artifact mode

Notes and the Up next order are only saved by the self-hosted server. The artifact copy shows the lanes and the library but has no note or reorder controls.

`backlog.html` can also be published as a [Claude](https://claude.ai) artifact. When it isn't served by `backlog_server.py`, it stores the library and triage marks in the artifact's database. It also offers an "Ask Claude for a pick" box. Self-hosted, these features simply stay off.

## Data sources

- Steam Web API: library and playtime.
- Steam store: Steam Deck compatibility and store details.
- ProtonDB: community Proton compatibility summaries. This is an unofficial endpoint, so missing or unexpected data is shown as unknown.
- HowLongToBeat, through `howlongtobeatpy`, which uses an unofficial API that can change. If it breaks, exports still complete without length times.

This project isn't affiliated with Valve, HowLongToBeat or any store.

## License

MIT
