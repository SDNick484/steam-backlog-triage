#!/bin/sh
# run-export.sh - run the Steam library export with the settings in ~/.steam-env.
#
# ~/.steam-env (chmod 600):
#   STEAM_API_KEY='your Steam Web API key'      # https://steamcommunity.com/dev/apikey
#   STEAM_PROFILE='7656119XXXXXXXXXX'          # SteamID64, or a profile URL
#
# Writes ~/exports/steam_backlog.json, which backlog_server.py serves to the dashboard.
# Meant for cron or a systemd timer; logs go to stdout/stderr.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ENVF="${STEAM_ENV_FILE:-$HOME/.steam-env}"
OUT="${EXPORT_DIR:-$HOME/exports}"
PY="$HERE/venv/bin/python"
[ -x "$PY" ] || PY=python3

[ -f "$ENVF" ] && . "$ENVF"
if [ -z "${STEAM_API_KEY:-}" ] || [ -z "${STEAM_PROFILE:-}" ]; then
  echo "$(date '+%F %T') skipped: set STEAM_API_KEY and STEAM_PROFILE in $ENVF"
  exit 0
fi
case "$STEAM_PROFILE" in
  [0-9]*) WHO="--steamid $STEAM_PROFILE" ;;
  *)      WHO="--profile $STEAM_PROFILE" ;;
esac

mkdir -p "$OUT"
cd "$OUT"
echo "$(date '+%F %T') starting export"
export STEAM_API_KEY
# shellcheck disable=SC2086
exec "$PY" "$HERE/steam_backlog_export.py" $WHO --details \
  --out "$OUT/steam_backlog.json" --cache "$OUT/steam_backlog_cache.json"
