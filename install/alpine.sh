#!/bin/sh
# install/alpine.sh - install Steam Backlog Triage on Alpine Linux (OpenRC + BusyBox crond).
#
# Run as root from a checkout of this repository:
#   git clone https://github.com/SDNick484/steam-backlog-triage.git
#   cd steam-backlog-triage && sh install/alpine.sh
#
# Optional settings, passed as environment variables:
#   STEAM_PROFILE    SteamID64 or profile URL (written to ~/.steam-env on first install)
#   STEAM_API_KEY    Steam Web API key        (written to ~/.steam-env on first install)
#   PORT             dashboard port (default 8484)
#   EXPORT_SCHEDULE  cron schedule for the export (default "15 4 * * 1": Mondays 04:15)
#   NAV              header link to another page, LABEL=URL with no spaces,
#                    e.g. NAV=Deals=http://deals-host:8485/
#   TZ_NAME          set the system time zone, e.g. America/New_York (default: leave as is)
#
# Ask (AI search) is configured in ~/.config/backlog-triage/ask.env, created empty on first install; it stays
# off until you set ANTHROPIC_SEARCH_KEY there. See the README's "AI search (Ask)" section.
#
# Logs under ~/logs are rotated weekly by /etc/logrotate.d/steam-backlog-triage (this app's two files only).
#
# Safe to re-run: code is updated, ~/.steam-env, ask.env and your data are kept, and the options above
# are remembered (in /etc/steam-backlog-triage.conf). Set NAV= (empty) to remove a link.
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=/opt/steam-backlog-triage
APP_USER=steam
HOME_DIR=/home/$APP_USER
# Options from a previous install are reused unless given again on the command line.
SAVED=/etc/steam-backlog-triage.conf
SAVED_PORT=""; SAVED_EXPORT_SCHEDULE=""; SAVED_NAV=""
[ -f "$SAVED" ] && . "$SAVED"
PORT="${PORT:-${SAVED_PORT:-8484}}"
EXPORT_SCHEDULE="${EXPORT_SCHEDULE:-${SAVED_EXPORT_SCHEDULE:-15 4 * * 1}}"
NAV="${NAV-${SAVED_NAV:-}}"
TZ_NAME="${TZ_NAME:-}"

say() { printf '\n==> %s\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
[ -f /etc/alpine-release ] || { echo "This installer is for Alpine Linux. See the README for other systems."; exit 1; }
case "$NAV" in *[[:space:]\'\"]*) echo "NAV must not contain spaces or quotes."; exit 1;; esac

say "Installing packages"
apk add --no-cache python3 py3-pip tzdata ca-certificates logrotate >/dev/null </dev/null

if [ -n "$TZ_NAME" ]; then
  say "Setting time zone to $TZ_NAME"
  cp "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime
  echo "$TZ_NAME" > /etc/timezone
fi

say "Creating the unprivileged '$APP_USER' user"
id "$APP_USER" >/dev/null 2>&1 || adduser -D -h "$HOME_DIR" -s /bin/sh "$APP_USER"
mkdir -p "$PREFIX" "$HOME_DIR/exports" "$HOME_DIR/logs"

say "Installing to $PREFIX"
install -m 0755 "$SRC/steam_backlog_export.py" "$SRC/backlog_server.py" "$SRC/run-export.sh" "$PREFIX/"
install -m 0644 "$SRC/backlog.html" "$SRC/backlog_query.py" "$SRC/access_jwt.py" "$SRC/ask.py" "$PREFIX/"

# Stamp the build id (shown by /healthz?deep=1) into the installed copy of the server, never the checkout: the
# short commit of this checkout when git can tell. Without git, or from a plain download, the placeholder stays
# and the build reads "unknown". A copy that git archive already stamped has no placeholder left to replace.
BUILD=$(git -C "$SRC" -c safe.directory="$SRC" rev-parse --short HEAD 2>/dev/null </dev/null || true)
case "$BUILD" in
  ""|*[!0-9a-f]*) BUILD="" ;;
esac
# shellcheck disable=SC2016  # the $ in the patterns below is literal: it is the placeholder's own
if [ -n "$BUILD" ]; then
  sed -i "s/^BUILD_STAMP = \"\$Format:%h\$\"\$/BUILD_STAMP = \"$BUILD\"/" "$PREFIX/backlog_server.py"
  echo "Build $BUILD"
elif grep -q '^BUILD_STAMP = "\$Format' "$PREFIX/backlog_server.py"; then
  echo "Build unknown (no git checkout)"
fi

say "Setting up a Python venv for HowLongToBeat lookups"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv" </dev/null
if "$PREFIX/venv/bin/pip" install --quiet --upgrade howlongtobeatpy </dev/null; then
  echo "howlongtobeatpy installed"
else
  echo "WARNING: couldn't install howlongtobeatpy; exports will skip HowLongToBeat times."
fi

ENVF="$HOME_DIR/.steam-env"
if [ ! -f "$ENVF" ]; then
  umask 077
  cat > "$ENVF" <<EOF
# Used by $PREFIX/run-export.sh
STEAM_API_KEY='${STEAM_API_KEY:-}'
STEAM_PROFILE='${STEAM_PROFILE:-}'
EOF
  umask 022
  echo "Wrote $ENVF"
fi

# Ask settings. Created once (empty, so Ask stays off) and never touched again.
ASK_DIR="$HOME_DIR/.config/backlog-triage"
ASKF="$ASK_DIR/ask.env"
mkdir -p "$ASK_DIR"
chmod 700 "$ASK_DIR"
if [ ! -f "$ASKF" ]; then
  umask 077
  cat > "$ASKF" <<'EOF'
# Settings for Ask (AI search), read by backlog_server.py at start-up. KEY=VALUE, one per line.
# Ask is off until ANTHROPIC_SEARCH_KEY is set. Edit this file (it must stay mode 600), then run:
#   rc-service backlog-triage restart
# See the README section "AI search (Ask)" for where to find each value.

# Anthropic API key, ideally from its own Console workspace with a spend limit.
ANTHROPIC_SEARCH_KEY=
# Model id; empty means claude-haiku-5-5.
ANTHROPIC_SEARCH_MODEL=
# Questions per day across everyone; empty means 200.
ASK_DAILY_LIMIT=
# Cloudflare Access team domain, e.g. your-team.cloudflareaccess.com
ASK_ACCESS_TEAM=
# The dashboard's Access application Audience (AUD) tag.
ASK_ACCESS_AUD=
# Comma-separated emails allowed to ask; empty means any user Access lets in.
ASK_EMAILS=
# 1 = also allow direct requests from your LAN, opened by IP address (see the README); empty or 0 = off.
ASK_LAN=
EOF
  umask 022
  echo "Wrote $ASKF"
fi
chown -R "$APP_USER:$APP_USER" "$HOME_DIR"
chown -R root:root "$PREFIX"

say "Rotating this app's logs"
# Only this app's two files, in a file of its own: replaced on every run, no other logrotate config is touched.
# copytruncate because the cron job and the service keep their log open.
mkdir -p /etc/logrotate.d
cat > /etc/logrotate.d/steam-backlog-triage <<EOF
$HOME_DIR/logs/dashboard.log $HOME_DIR/logs/backlog.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF
chmod 0644 /etc/logrotate.d/steam-backlog-triage

say "Scheduling the export ($EXPORT_SCHEDULE)"
# Replace only this project's block, so other jobs in the same crontab survive.
mkdir -p /etc/crontabs
TAB=/etc/crontabs/$APP_USER
touch "$TAB"
sed -i '/^# BEGIN steam-backlog-triage$/,/^# END steam-backlog-triage$/d' "$TAB"
cat >> "$TAB" <<EOF
# BEGIN steam-backlog-triage
$EXPORT_SCHEDULE  $PREFIX/run-export.sh >> $HOME_DIR/logs/backlog.log 2>&1
# END steam-backlog-triage
EOF
chmod 0600 "$TAB"
rc-update add crond default >/dev/null 2>&1 || true
rc-service crond restart >/dev/null 2>&1 || rc-service crond start

say "Installing the dashboard service (port $PORT)"
NAV_ARG=""
[ -n "$NAV" ] && NAV_ARG="--nav $NAV"
cat > /etc/init.d/backlog-triage <<EOF
#!/sbin/openrc-run
name="backlog-triage"
description="Steam Backlog Triage dashboard"
command="/usr/bin/python3"
command_args="$PREFIX/backlog_server.py --port $PORT --data $HOME_DIR/exports --ask-env $ASKF $NAV_ARG"
command_user="$APP_USER:$APP_USER"
supervisor="supervise-daemon"
respawn_delay=5
output_log="$HOME_DIR/logs/dashboard.log"
error_log="$HOME_DIR/logs/dashboard.log"

depend() {
  after net
}
EOF
chmod 0755 /etc/init.d/backlog-triage
touch "$HOME_DIR/logs/dashboard.log" && chown "$APP_USER:$APP_USER" "$HOME_DIR/logs/dashboard.log"
rc-update add backlog-triage default >/dev/null 2>&1 || true
rc-service backlog-triage restart >/dev/null 2>&1 || rc-service backlog-triage start

IP=$(ip -4 -o addr show 2>/dev/null | awk '$2 != "lo" {print $4; exit}' | cut -d/ -f1)
sleep 1
if wget -q -O /dev/null "http://127.0.0.1:$PORT/healthz" 2>/dev/null; then
  echo "Dashboard running: http://${IP:-<this-host>}:$PORT/"
else
  echo "WARNING: the dashboard didn't answer; check $HOME_DIR/logs/dashboard.log"
fi

cat > "$SAVED" <<EOF
# Written by install/alpine.sh; reused on re-runs. Pass a variable again to change it.
SAVED_PORT='$PORT'
SAVED_EXPORT_SCHEDULE='$EXPORT_SCHEDULE'
SAVED_NAV='$NAV'
EOF

say "Done"
echo "Steam settings: $ENVF"
echo "Ask settings: $ASKF"
echo "Run an export now: su $APP_USER -c $PREFIX/run-export.sh"
echo "Logs: $HOME_DIR/logs/"
