#!/bin/sh
# tests/installer/check.sh - run install/alpine.sh twice (three times, really) with the system commands
# stubbed, and check what a re-run must keep and must not duplicate.
#
# It changes /etc, /opt and /home, so it only runs where that is safe: inside the python:3.12-alpine CI
# container, or anywhere else when INSTALLER_TEST_OK=1 (a throwaway VM or container). On a machine that
# isn't Alpine, set FAKE_USERS=1 as well: adduser and chown are stubbed, since there is no BusyBox adduser.
#
#   docker run --rm -v "$PWD":/src:ro python:3.12-alpine sh /src/tests/installer/check.sh
set -eu

SRC=$(cd "$(dirname "$0")/../.." && pwd)
if [ "${INSTALLER_TEST_OK:-}" != 1 ] && [ ! -f /.dockerenv ] && [ ! -f /etc/alpine-release ]; then
  echo "Refusing to run: this changes /etc, /opt and /home. Use a container, or set INSTALLER_TEST_OK=1." >&2
  exit 2
fi

STUBS=$(mktemp -d)
CALLS=$STUBS/calls.log
: > "$CALLS"
made_alpine_release=0
fail=0

# shellcheck disable=SC2329  # runs from the EXIT trap
cleanup() {
  [ "$made_alpine_release" = 1 ] && rm -f /etc/alpine-release
  rm -rf "$STUBS"
}
trap cleanup EXIT

ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1" >&2; fail=1; }
check() { # check "description" command...
  desc=$1; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}
count() { grep -c "$1" "$2" || true; }

# ---- stubs: record the call, do nothing
for cmd in apk rc-service rc-update wget ip; do
  cat > "$STUBS/$cmd" <<EOF
#!/bin/sh
echo "$cmd \$*" >> "$CALLS"
exit 0
EOF
  chmod +x "$STUBS/$cmd"
done
if [ "${FAKE_USERS:-}" = 1 ]; then
  for cmd in adduser chown; do
    printf '#!/bin/sh\necho "%s $*" >> "%s"\nexit 0\n' "$cmd" "$CALLS" > "$STUBS/$cmd"
    chmod +x "$STUBS/$cmd"
  done
  # shellcheck disable=SC2016  # the stub's own $1 and $@ must stay literal
  printf '#!/bin/sh\n[ "${1:-}" = steam ] && exit 1\nexec /usr/bin/id "$@"\n' > "$STUBS/id"
  chmod +x "$STUBS/id"
fi
PATH="$STUBS:$PATH"
export PATH

[ -f /etc/alpine-release ] || { echo "3.20.0" > /etc/alpine-release; made_alpine_release=1; }

# ---- a venv that is already there, so the installer doesn't try to reach PyPI
PREFIX=/opt/steam-backlog-triage
mkdir -p "$PREFIX/venv/bin"
PYBIN=$(command -v python3)
ln -sf "$PYBIN" "$PREFIX/venv/bin/python"
printf '#!/bin/sh\necho "pip $*" >> "%s"\nexit 0\n' "$CALLS" > "$PREFIX/venv/bin/pip"
chmod +x "$PREFIX/venv/bin/pip"

HOME_DIR=/home/steam
TAB=/etc/crontabs/steam
SAVED=/etc/steam-backlog-triage.conf
INIT=/etc/init.d/backlog-triage
ENVF=$HOME_DIR/.steam-env
# Start from a machine that has never had the installer (matters when this is re-run in the same VM).
rm -f "$ENVF" "$SAVED" "$INIT"
mkdir -p /etc/crontabs /etc/init.d "$HOME_DIR"

# An unrelated job, and a block from an older install that must be replaced rather than duplicated.
cat > "$TAB" <<'EOF'
0 3 * * * /usr/local/bin/other-job.sh
# BEGIN steam-backlog-triage
1 1 * * 1  /old/path/run-export.sh >> /old.log 2>&1
# END steam-backlog-triage
30 6 * * * /usr/local/bin/another-job.sh
EOF

run() { # run label [VAR=value ...]
  label=$1; shift
  printf '\n--- %s\n' "$label"
  env "$@" sh "$SRC/install/alpine.sh" > "$STUBS/out.$label" 2>&1 || { cat "$STUBS/out.$label" >&2; bad "$label: installer exited non-zero"; return 1; }
  ok "$label: installer exited 0"
}

# ================================================================== first install
run first STEAM_API_KEY=FAKEKEY0123456789 STEAM_PROFILE=7656119XXXXXXXXXX PORT=8585 \
  'EXPORT_SCHEDULE=30 5 * * 2' NAV=Deals=http://deals.example.com:8485/

check "creates the env file from the given settings" grep -q "STEAM_API_KEY='FAKEKEY0123456789'" "$ENVF"
check "the env file is mode 600" test "$(stat -c %a "$ENVF")" = 600
check "saves the options" grep -q "SAVED_PORT='8585'" "$SAVED"
check "init script uses the port and the nav link" grep -q -- "--port 8585 .*--nav Deals=http://deals.example.com:8485/" "$INIT"
check "unrelated cron line before the block is kept" grep -qx '0 3 \* \* \* /usr/local/bin/other-job.sh' "$TAB"
check "unrelated cron line after the block is kept" grep -qx '30 6 \* \* \* /usr/local/bin/another-job.sh' "$TAB"
check "the old block's schedule is gone" sh -c "! grep -q '/old/path' '$TAB'"
check "the new schedule is in the block" grep -q '^30 5 \* \* 2  /opt/steam-backlog-triage/run-export.sh' "$TAB"
check "exactly one BEGIN" test "$(count '^# BEGIN steam-backlog-triage$' "$TAB")" = 1
check "exactly one END" test "$(count '^# END steam-backlog-triage$' "$TAB")" = 1
check "installed the files" test -x "$PREFIX/backlog_server.py" -a -x "$PREFIX/run-export.sh" -a -f "$PREFIX/backlog.html"
check "enabled and restarted the service" grep -q '^rc-service backlog-triage restart' "$CALLS"

cp "$TAB" "$STUBS/tab.first"

# ================================================================== re-run: nothing given
# The user edits the env file between runs; it must survive untouched.
echo "STEAM_API_KEY='EDITED-BY-USER'" > "$ENVF"
chmod 600 "$ENVF"
cp "$ENVF" "$STUBS/env.before"
run second

check "keeps the env file byte for byte" cmp -s "$ENVF" "$STUBS/env.before"
check "keeps the env file mode 600" test "$(stat -c %a "$ENVF")" = 600
check "remembers PORT" grep -q -- "--port 8585 " "$INIT"
check "remembers NAV" grep -q -- "--nav Deals=http://deals.example.com:8485/" "$INIT"
check "remembers EXPORT_SCHEDULE" grep -q '^30 5 \* \* 2  ' "$TAB"
check "saved options file is unchanged in content" grep -q "SAVED_EXPORT_SCHEDULE='30 5 \* \* 2'" "$SAVED"
check "still exactly one BEGIN" test "$(count '^# BEGIN steam-backlog-triage$' "$TAB")" = 1
check "still exactly one END" test "$(count '^# END steam-backlog-triage$' "$TAB")" = 1
check "crontab is identical to after the first run" cmp -s "$TAB" "$STUBS/tab.first"
check "unrelated cron lines still there" sh -c "grep -qx '0 3 \* \* \* /usr/local/bin/other-job.sh' '$TAB' && grep -qx '30 6 \* \* \* /usr/local/bin/another-job.sh' '$TAB'"

# ================================================================== re-run: change one option, clear another
run third PORT=8686 NAV=

check "a given option replaces the saved one" grep -q -- "--port 8686 " "$INIT"
check "an empty NAV removes the link" sh -c "! grep -q -- '--nav' '$INIT'"
check "the other saved option is kept" grep -q '^30 5 \* \* 2  ' "$TAB"
check "still exactly one block after the third run" test "$(count '^# BEGIN steam-backlog-triage$' "$TAB")" = 1
check "the env file still untouched" cmp -s "$ENVF" "$STUBS/env.before"

# ================================================================== refusals
printf '\n--- refusals\n'
if env 'NAV=bad nav' sh "$SRC/install/alpine.sh" >/dev/null 2>&1; then bad "NAV with a space is refused"; else ok "NAV with a space is refused"; fi

echo
if [ "$fail" = 0 ]; then echo "installer checks passed"; else echo "installer checks FAILED" >&2; fi
exit "$fail"
