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
  rm -f "$LROTHER"
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
# git: answers `rev-parse --short HEAD` with $FAKE_GIT_HASH, or fails like a directory that is not a checkout.
cat > "$STUBS/git" <<'EOF'
#!/bin/sh
[ -n "${FAKE_GIT_HASH:-}" ] && { echo "$FAKE_GIT_HASH"; exit 0; }
exit 128
EOF
chmod +x "$STUBS/git"
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
LR=/etc/logrotate.d/steam-backlog-triage
LROTHER=/etc/logrotate.d/other-app-for-installer-check
ENVF=$HOME_DIR/.steam-env
ASKF=$HOME_DIR/.config/backlog-triage/ask.env
# Start from a machine that has never had the installer (matters when this is re-run in the same VM).
rm -f "$ENVF" "$SAVED" "$INIT" "$ASKF" "$LR"
mkdir -p /etc/crontabs /etc/init.d /etc/logrotate.d "$HOME_DIR"

# Another application's logrotate file: the installer must leave it alone.
printf '/var/log/other-app.log {\n    daily\n    rotate 3\n}\n' > "$LROTHER"
cksum < "$LROTHER" > "$STUBS/lrother.sum"
others() { (cd /etc/logrotate.d && for f in *; do [ "$f" = steam-backlog-triage ] || cksum < "$f"; done); }  # every file but ours
others > "$STUBS/lrothers.sum"

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
run first FAKE_GIT_HASH=abc1234 STEAM_API_KEY=FAKEKEY0123456789 STEAM_PROFILE=7656119XXXXXXXXXX PORT=8585 \
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
check "installed the Ask modules, mode 644" sh -c "for m in backlog_query access_jwt ask; do [ \"\$(stat -c %a '$PREFIX'/\$m.py)\" = 644 ] || exit 1; done"
check "the installed server and its modules import (rc-service is stubbed, so nothing started it)" \
  sh -c "cd '$PREFIX' && python3 -c 'import backlog_server, ask, backlog_query, access_jwt'"
check "creates ask.env, mode 600" test "$(stat -c %a "$ASKF")" = 600
check "ask.env settings are all empty" sh -c "grep -qE '^[A-Z_]+=' '$ASKF' && ! grep -E '^[A-Z_]+=' '$ASKF' | grep -qvE '^[A-Z_]+=\$'"
check "ask.env lists the seven settings" sh -c "for k in ANTHROPIC_SEARCH_KEY ANTHROPIC_SEARCH_MODEL ASK_DAILY_LIMIT ASK_ACCESS_TEAM ASK_ACCESS_AUD ASK_EMAILS ASK_LAN; do grep -q \"^\$k=\" '$ASKF' || exit 1; done"
check "init script passes --ask-env" grep -q -- "--ask-env $ASKF" "$INIT"

# ---- log rotation: this app's two files, in a file of its own
check "installs logrotate" grep -q '^apk add .*logrotate' "$CALLS"
check "writes the app's logrotate file, mode 644" test "$(stat -c %a "$LR")" = 644
check "it covers the dashboard log and the export log" grep -qx "$HOME_DIR/logs/dashboard.log $HOME_DIR/logs/backlog.log {" "$LR"
check "it has no wildcard and names no other path" sh -c "! grep -q '[*]' '$LR' && test \"\$(grep -c '/' '$LR')\" = 1"
for d in weekly "rotate 8" compress missingok notifempty copytruncate; do
  check "logrotate: $d" grep -qx "    $d" "$LR"
done
check "logrotate: the block is closed" grep -qx '}' "$LR"
check "another application's logrotate file is untouched" sh -c "cksum < '$LROTHER' | cmp -s - '$STUBS/lrother.sum'"
cp "$LR" "$STUBS/lr.first"

# ---- build stamp: the installed server is stamped from git; the checkout is not
check "the installed server is stamped with the short commit" grep -qx 'BUILD_STAMP = "abc1234"' "$PREFIX/backlog_server.py"
# shellcheck disable=SC2016  # the $ is literal: it is the placeholder's own
check "the checkout's server still has the placeholder" grep -q '^BUILD_STAMP = "\$Format:%h\$"$' "$SRC/backlog_server.py"
check "the installed server reports that build" sh -c "cd '$PREFIX' && test \"\$(python3 -c 'import backlog_server as b; print(b.build_id())')\" = abc1234"

cp "$TAB" "$STUBS/tab.first"
# The user fills in a key; later runs must never touch ask.env.
echo "ANTHROPIC_SEARCH_KEY=test-not-a-key" >> "$ASKF"
cksum < "$ASKF" > "$STUBS/ask.sum"

# ================================================================== re-run: nothing given
# The user edits the env file between runs; it must survive untouched.
echo "STEAM_API_KEY='EDITED-BY-USER'" > "$ENVF"
chmod 600 "$ENVF"
cp "$ENVF" "$STUBS/env.before"
run second

check "keeps the env file byte for byte" cmp -s "$ENVF" "$STUBS/env.before"
check "keeps the env file mode 600" test "$(stat -c %a "$ENVF")" = 600
check "keeps ask.env byte for byte" sh -c "cksum < '$ASKF' | cmp -s - '$STUBS/ask.sum'"
check "keeps ask.env mode 600" test "$(stat -c %a "$ASKF")" = 600
check "remembers PORT" grep -q -- "--port 8585 " "$INIT"
check "remembers NAV" grep -q -- "--nav Deals=http://deals.example.com:8485/" "$INIT"
check "remembers EXPORT_SCHEDULE" grep -q '^30 5 \* \* 2  ' "$TAB"
check "saved options file is unchanged in content" grep -q "SAVED_EXPORT_SCHEDULE='30 5 \* \* 2'" "$SAVED"
check "still exactly one BEGIN" test "$(count '^# BEGIN steam-backlog-triage$' "$TAB")" = 1
check "still exactly one END" test "$(count '^# END steam-backlog-triage$' "$TAB")" = 1
check "crontab is identical to after the first run" cmp -s "$TAB" "$STUBS/tab.first"
check "the build is unknown when git can't tell, and the stamp is gone from the installed copy" \
  sh -c "grep -q '^BUILD_STAMP = \"\\\$Format:%h\\\$\"\$' '$PREFIX/backlog_server.py' && cd '$PREFIX' && test \"\$(python3 -c 'import backlog_server as b; print(b.build_id())')\" = unknown"
check "logrotate file rewritten identically" cmp -s "$LR" "$STUBS/lr.first"
check "other logrotate file still untouched after the second run" sh -c "cksum < '$LROTHER' | cmp -s - '$STUBS/lrother.sum'"
check "unrelated cron lines still there" sh -c "grep -qx '0 3 \* \* \* /usr/local/bin/other-job.sh' '$TAB' && grep -qx '30 6 \* \* \* /usr/local/bin/another-job.sh' '$TAB'"

# ================================================================== re-run: change one option, clear another
# The app's logrotate file is edited by hand; a re-run puts it back.
echo "# local edit" >> "$LR"
chmod 600 "$LR"
run third FAKE_GIT_HASH=def5678 PORT=8686 NAV=

check "a re-run restores the logrotate file" cmp -s "$LR" "$STUBS/lr.first"
check "a re-run restores its mode" test "$(stat -c %a "$LR")" = 644
check "the third run stamps the new commit" grep -qx 'BUILD_STAMP = "def5678"' "$PREFIX/backlog_server.py"
others > "$STUBS/lrothers.after"
check "no other logrotate file was created, removed or changed" cmp -s "$STUBS/lrothers.sum" "$STUBS/lrothers.after"

check "a given option replaces the saved one" grep -q -- "--port 8686 " "$INIT"
check "an empty NAV removes the link" sh -c "! grep -q -- '--nav' '$INIT'"
check "the other saved option is kept" grep -q '^30 5 \* \* 2  ' "$TAB"
check "still exactly one block after the third run" test "$(count '^# BEGIN steam-backlog-triage$' "$TAB")" = 1
check "the env file still untouched" cmp -s "$ENVF" "$STUBS/env.before"
check "ask.env still untouched after the third run" sh -c "cksum < '$ASKF' | cmp -s - '$STUBS/ask.sum'"
check "ask.env still mode 600 after the third run" test "$(stat -c %a "$ASKF")" = 600

# ================================================================== refusals
printf '\n--- refusals\n'
if env 'NAV=bad nav' sh "$SRC/install/alpine.sh" >/dev/null 2>&1; then bad "NAV with a space is refused"; else ok "NAV with a space is refused"; fi

echo
if [ "$fail" = 0 ]; then echo "installer checks passed"; else echo "installer checks FAILED" >&2; fi
exit "$fail"
