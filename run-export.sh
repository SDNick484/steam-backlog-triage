#!/bin/sh
# run-export.sh - run the Steam library export with the settings in ~/.steam-env.
#
# ~/.steam-env (chmod 600):
#   STEAM_API_KEY='your Steam Web API key'      # https://steamcommunity.com/dev/apikey
#   STEAM_PROFILE='7656119XXXXXXXXXX'          # SteamID64, or a profile URL
#
# Writes ~/exports/steam_backlog.json, which backlog_server.py serves to the dashboard.
# Meant for cron or a systemd timer; logs go to stdout/stderr.
#
# One export runs at a time, whoever starts it (cron, a timer, the dashboard's Refresh library
# button, a shell). The run holds an exclusive lock on $EXPORT_DIR/.export.lock until it exits.
#
# The exporter records how each run ended in $EXPORT_DIR/run_status.json (the dashboard's banner and
# /healthz?deep=1 read it). A run that exits here, with 75 or 78, never reaches the exporter and writes nothing.
#
# Exit status (sysexits):
#   0   the export finished (including "Interrupted; writing what's done so far")
#   75  another export is already running (the lock is held)
#   78  STEAM_API_KEY or STEAM_PROFILE isn't set
#   *   the exporter failed
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ENVF="${STEAM_ENV_FILE:-$HOME/.steam-env}"
OUT="${EXPORT_DIR:-$HOME/exports}"
PY="$HERE/venv/bin/python"
[ -x "$PY" ] || PY=python3

[ -f "$ENVF" ] && . "$ENVF"
if [ -z "${STEAM_API_KEY:-}" ] || [ -z "${STEAM_PROFILE:-}" ]; then
  echo "$(date '+%F %T') skipped: set STEAM_API_KEY and STEAM_PROFILE in $ENVF"
  exit 78
fi
case "$STEAM_PROFILE" in
  [0-9]*) WHO="--steamid $STEAM_PROFILE" ;;
  *)      WHO="--profile $STEAM_PROFILE" ;;
esac

mkdir -p "$OUT"
cd "$OUT"
# Take the lock on fd 9 and keep it: the shell leaves fd 9 open across the exec below, so the
# exporter itself holds the lock until it exits. flock is a BusyBox applet on Alpine and part of
# util-linux elsewhere; both use flock(2), as does the dashboard server when it checks.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$OUT/.export.lock"
  if ! flock -n 9; then
    echo "$(date '+%F %T') skipped: another export is running"
    exit 75
  fi
else
  # Without flock every run would look "busy" and the weekly export would never happen, so run
  # unlocked and say so.
  echo "$(date '+%F %T') warning: flock not found; not guarding against concurrent exports"
fi
echo "$(date '+%F %T') starting export"
export STEAM_API_KEY
# shellcheck disable=SC2086
exec "$PY" "$HERE/steam_backlog_export.py" $WHO --details \
  --out "$OUT/steam_backlog.json" --cache "$OUT/steam_backlog_cache.json"
