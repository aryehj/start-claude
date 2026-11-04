#!/usr/bin/env bash
# verify-sandbox-runtime.sh — live re-test of the start-claude.sh sandbox after a
# sandbox-runtime pin bump (ADR-047). Run on the macOS host:
#
#     tests/verify-sandbox-runtime.sh             rebuild if needed, then check
#     tests/verify-sandbox-runtime.sh --rebuild   always rebuild first
#
# It uses its own throwaway project (.temp/sandbox-probe) and container
# (sandbox-probe), so your project containers are left alone. When the probe
# container is missing, or holds a sandbox-runtime other than the pin in
# start-claude.sh, it runs `start-claude.sh --rebuild` for it. That rebuilds the
# shared claude-dev:latest image, and leaves you in the probe container's
# shell: type `exit` and the checks run on their own. Existing project
# containers keep their old image until you recreate them.
#
# Checks, each PASS/FAIL (INFO lines are recorded, not judged):
#   - the installed sandbox-runtime matches the pin
#   - CAP_SETFCAP (bit 31), CAP_SYS_ADMIN and CAP_NET_ADMIN are in the probe
#     container's bounding set; INFO: whether Apple Containers' default set,
#     with no --cap-add at all, has CAP_SETFCAP (plan Unknown 2)
#   - sandbox-runtime's own uid-0 preflight, `bwrap --unshare-user --dev-bind / / true`
#   - the same in-sandbox checks twice, under `srt` directly (the pinned
#     package) and under Claude Code (`claude -p`, haiku, one Bash call; its
#     bundled runtime plus the pinned apply-seccomp helper):
#     the command runs inside a user namespace; a write to the project dir
#     works; a write to /etc is denied; `mount -o remount,rw /` does not make
#     /usr writable; /proc/1/environ is unreadable; a non-allowlisted domain
#     is blocked, an allowlisted one is reachable, and a direct connection
#     that skips the proxy fails
#   - from outside the sandbox: the denied writes left no file behind
# The sandbox-runtime and Claude Code debug output (bwrap arguments, the
# apply-seccomp path, any CAP_SETFCAP warning) is excerpted into the log; the
# full files stay in .temp/sandbox-probe/.
#
# Claude Code uses the shared login in ~/.claude-containers/shared, as every
# start-claude.sh container does. Log: .verify/verify-sandbox-runtime.log
# Clean up afterwards with: container rm sandbox-probe

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
mkdir -p .verify
LOG="$REPO/.verify/verify-sandbox-runtime.log"
: > "$LOG"

NAME=sandbox-probe
IMAGE=claude-dev:latest
PROBE_DIR="$REPO/.temp/sandbox-probe"
FORCE_REBUILD=false
for a in "$@"; do
  case "$a" in
    --rebuild) FORCE_REBUILD=true ;;
    *) echo "usage: $0 [--rebuild]"; exit 2 ;;
  esac
done

PASS=0; FAIL=0; INFO=0; SANDBOX_TRIED=false
say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
pass() { say "PASS  $1"; PASS=$((PASS+1)); }
fail() { say "FAIL  $1${2:+ — $2}"; FAIL=$((FAIL+1)); }
info() { say "INFO  $1"; INFO=$((INFO+1)); }
summary() {
  say ""
  say "==> $PASS passed, $FAIL failed, $INFO info (log: $LOG)"
  if [[ $FAIL -gt 0 && $SANDBOX_TRIED == true ]]; then
    say "    On a sandbox failure: read the debug excerpts above, revert the pin"
    say "    (git revert the bump commit), and bisect 0.0.75 / 0.0.76 / 0.0.77, where"
    say "    weaker mode gained --cap-drop ALL and the CAP_SETFCAP refusal."
  fi
  [[ $FAIL -eq 0 ]]
  exit $?
}
die() { fail "$1" "$2"; summary; }

container_exists() {
  local out
  out="$(container inspect "$1" 2>/dev/null)" || return 1
  [[ -n "$out" && "$out" != "[]" ]]
}
is_running() {
  container inspect "$NAME" 2>/dev/null | grep -qE '"state"[[:space:]]*:[[:space:]]*"running"'
}
cexec() { container exec -w "$PROBE_DIR" "$NAME" "$@" </dev/null; }
# macOS has no timeout(1). Kill the client after $1 seconds.
with_timeout() {
  local secs=$1 pid watcher rc
  shift
  "$@" & pid=$!
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null ) & watcher=$!
  wait "$pid"; rc=$?
  kill "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null
  return $rc
}
cap_bit() {  # cap_bit HEX BIT -> exit 0 when set
  [[ "$1" =~ ^[0-9a-fA-F]+$ ]] || return 1
  (( (0x$1 >> $2) & 1 ))
}
excerpt() {  # excerpt FILE LABEL
  [[ -s "$1" ]] || { say "    ($2: no debug output)"; return; }
  say "    --- $2 (sandbox lines, first 40) ---"
  grep -iE 'sandbox linux|bwrap|seccomp|setfcap|cap-drop|unshare|uid_map|operation not permitted' "$1" \
    | head -n 40 | cut -c1-400 | sed 's/^/    | /' | tee -a "$LOG"
}

say "==> sandbox-runtime live check ($(date '+%Y-%m-%d %H:%M:%S')), git $(git rev-parse --short HEAD 2>/dev/null) on $(git branch --show-current 2>/dev/null)"

# ── preconditions ─────────────────────────────────────────────────────────────
command -v container >/dev/null || die "container CLI not on PATH" "Run this on the macOS host with Apple Containers."
PIN=$(sed -n 's/.*@anthropic-ai\/sandbox-runtime@\([0-9][0-9.]*\).*/\1/p' "$REPO/start-claude.sh" | head -n 1)
[[ -n "$PIN" ]] || die "no sandbox-runtime pin found in start-claude.sh" "Expected @anthropic-ai/sandbox-runtime@X.Y.Z"
say "    pin in start-claude.sh: $PIN"
container system start >/dev/null 2>&1 || true
mkdir -p "$PROBE_DIR"

installed_version() { cexec srt --version 2>/dev/null | tr -d '[:space:]'; }

# ── 1. rebuild when needed ────────────────────────────────────────────────────
need_rebuild=$FORCE_REBUILD
if ! $need_rebuild; then
  if container_exists "$NAME"; then
    container start "$NAME" >/dev/null 2>&1 || true
    have=$(installed_version)
    if [[ "$have" != "$PIN" ]]; then
      say "    probe container has sandbox-runtime '${have:-?}', not $PIN: rebuilding"
      need_rebuild=true
    else
      say "    probe container already has $PIN: skipping the rebuild (pass --rebuild to force)"
    fi
  else
    need_rebuild=true
  fi
fi
if $need_rebuild; then
  say ""
  say "==> start-claude.sh --rebuild $PROBE_DIR $NAME"
  echo "    This takes a few minutes. You will land in the probe container's shell:"
  echo "    type 'exit' there and the checks continue."
  "$REPO/start-claude.sh" --rebuild "$PROBE_DIR" "$NAME"
  say "    start-claude.sh exit $?"
  container_exists "$NAME" || die "start-claude.sh did not create '$NAME'" "See its output above."
fi

# ── 2. bring the probe container up ───────────────────────────────────────────
container start "$NAME" >/dev/null 2>&1 || true
for _ in $(seq 1 30); do is_running && break; sleep 1; done
is_running || die "container '$NAME' is not running" "Try: container start $NAME"

SANDBOX_TRIED=true
say ""
say "==> facts"
have=$(installed_version)
[[ "$have" == "$PIN" ]] && pass "installed sandbox-runtime is the pin ($PIN)" \
  || fail "installed sandbox-runtime" "got '${have:-none}', want $PIN"
say "    claude $(cexec claude --version 2>/dev/null | head -n 1)"
say "    $(cexec bwrap --version 2>/dev/null | head -n 1); kernel $(cexec uname -r 2>/dev/null); uid $(cexec id -u 2>/dev/null)"
say "    apply-seccomp: $(cexec bash -c 'ls "$(npm root -g)"/@anthropic-ai/sandbox-runtime/vendor/seccomp/*/apply-seccomp 2>/dev/null | tr "\n" " "')"

status=$(cexec cat /proc/self/status 2>/dev/null)
bnd=$(printf '%s\n' "$status" | awk '/^CapBnd:/ {print $2}')
say "    probe container $(printf '%s\n' "$status" | grep -E '^Cap(Inh|Prm|Eff|Bnd):' | tr -s '\t ' ' ' | tr '\n' ' ')"
cap_bit "$bnd" 31 && pass "CAP_SETFCAP (bit 31) in the probe container's bounding set" \
  || fail "CAP_SETFCAP (bit 31) in the bounding set" "CapBnd=$bnd; sandbox-runtime refuses to sandbox as uid 0 without it"
cap_bit "$bnd" 21 && pass "CAP_SYS_ADMIN (bit 21) in the bounding set" || fail "CAP_SYS_ADMIN (bit 21) in the bounding set" "CapBnd=$bnd"
cap_bit "$bnd" 12 && pass "CAP_NET_ADMIN (bit 12) in the bounding set" || fail "CAP_NET_ADMIN (bit 12) in the bounding set" "CapBnd=$bnd"

# Unknown 2: Apple Containers' default set, with no --cap-add at all.
dflt=$(container run --rm "$IMAGE" grep '^CapBnd:' /proc/self/status </dev/null 2>/dev/null | awk '{print $2}')
if [[ -z "$dflt" ]]; then
  info "default bounding set (no --cap-add): could not read it"
elif cap_bit "$dflt" 31; then
  info "default bounding set (no --cap-add) CapBnd=$dflt HAS CAP_SETFCAP: --cap-add SETFCAP is belt-and-braces"
else
  info "default bounding set (no --cap-add) CapBnd=$dflt LACKS CAP_SETFCAP: --cap-add SETFCAP is required"
fi

settings=$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])).get("sandbox",{}); print(s.get("enabled"), s.get("enableWeakerNestedSandbox"), s.get("failIfUnavailable"), s.get("allowUnsandboxedCommands"))' \
  "$PROBE_DIR/.claude/settings.local.json" 2>/dev/null)
[[ "$settings" == "True True True False" ]] && pass "probe settings.local.json: enabled, weaker mode, failIfUnavailable, no unsandboxed fallback" \
  || fail "probe settings.local.json sandbox block" "enabled/weaker/failIfUnavailable/allowUnsandboxed = '${settings:-unreadable}'"

probe_err=$(cexec bwrap --unshare-user --dev-bind / / true 2>&1)
[[ $? -eq 0 ]] && pass "uid-0 user namespace preflight (bwrap --unshare-user --dev-bind / / true)" \
  || fail "uid-0 user namespace preflight" "$(printf '%s' "$probe_err" | head -n 1)"

# ── 3. the in-sandbox checks ──────────────────────────────────────────────────
# Runs inside the sandbox; writes one "CHECK name STATUS detail" line per check.
cat > "$PROBE_DIR/sandbox-checks.sh" <<'CHECKS'
#!/bin/bash
out="$1"
dir=$(dirname "$out")
: > "$out" || exit 1
r() { printf 'CHECK %s %s %s\n' "$1" "$2" "$3" >> "$out"; }
r ran PASS "uid=$(id -u) shell pid=$$"
map=$(tr -s ' \t' ' ' < /proc/self/uid_map | sed 's/^ //' | tr '\n' ';')
case "$map" in
  *4294967295*) r in_userns FAIL "uid_map=[$map] is the initial namespace: the command is not sandboxed" ;;
  *) r in_userns PASS "uid_map=[$map]" ;;
esac
r caps INFO "$(grep -E '^Cap(Eff|Bnd):' /proc/self/status | tr -s '\t ' ' ' | tr '\n' ' ')"
if echo ok > "$dir/write-allowed.probe" 2>/dev/null; then r write_allowed PASS "$dir"; else r write_allowed FAIL "could not write $dir"; fi
if err=$(touch /etc/.sandbox-probe 2>&1); then
  rm -f /etc/.sandbox-probe; r write_denied FAIL "wrote /etc/.sandbox-probe"
else
  r write_denied PASS "$(echo "$err" | head -n 1)"
fi
merr=$(mount -o remount,rw / 2>&1); mrc=$?
if touch /usr/.sandbox-probe 2>/dev/null; then
  rm -f /usr/.sandbox-probe; r remount_denied FAIL "remount rc=$mrc, then wrote /usr/.sandbox-probe"
else
  r remount_denied PASS "remount rc=$mrc ($(echo "$merr" | head -n 1)); /usr still read-only"
fi
if head -c 1 /proc/1/environ >/dev/null 2>&1; then r environ_denied FAIL "/proc/1/environ readable"; else r environ_denied PASS "/proc/1/environ unreadable"; fi
if unshare -U -r true 2>/dev/null; then r nested_userns INFO "a nested user namespace can be created"; else r nested_userns INFO "a nested user namespace is refused"; fi
r proxy_env INFO "HTTPS_PROXY=${HTTPS_PROXY:-unset}"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 https://example.com/ 2>/dev/null); rc=$?
case "$code" in
  2*|3*) r net_denied FAIL "example.com answered HTTP $code" ;;
  *) r net_denied PASS "example.com blocked (curl rc=$rc, HTTP ${code:-none})" ;;
esac
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 https://registry.npmjs.org/ 2>/dev/null); rc=$?
[ "$code" = 200 ] && r net_allowed PASS "registry.npmjs.org HTTP 200" || r net_allowed FAIL "registry.npmjs.org curl rc=$rc, HTTP ${code:-none}"
if curl --noproxy '*' -s -o /dev/null -m 10 https://registry.npmjs.org/ 2>/dev/null; then
  r net_isolated FAIL "direct connection without the proxy succeeded"
else
  r net_isolated PASS "direct connection without the proxy failed"
fi
r done INFO "end"
CHECKS
EXPECTED="ran in_userns write_allowed write_denied remount_denied environ_denied net_denied net_allowed net_isolated"

judge() {  # judge LABEL RESULTS DEBUGFILE OUTPUTFILE
  local label=$1 results=$2 debug=$3 output=$4 name st detail
  if [[ ! -s "$results" ]]; then
    fail "$label: the sandboxed command did not run" "no results in $results"
    if [[ -s "$output" && "$output" != "$debug" ]]; then
      say "    --- $label output (last 30 lines) ---"
      tail -n 30 "$output" | cut -c1-400 | sed 's/^/    | /' | tee -a "$LOG"
    fi
    excerpt "$debug" "$label debug"
    if [[ -s "$debug" ]]; then
      say "    --- $label debug (last 60 lines) ---"
      tail -n 60 "$debug" | cut -c1-400 | sed 's/^/    | /' | tee -a "$LOG"
    fi
    return
  fi
  while read -r _ name st detail; do
    case "$st" in
      PASS) pass "$label: $name — $detail" ;;
      FAIL) fail "$label: $name" "$detail" ;;
      *) [[ "$name" == done ]] || info "$label: $name — $detail" ;;
    esac
  done < <(grep '^CHECK ' "$results")
  for name in $EXPECTED; do
    grep -q "^CHECK $name " "$results" || { fail "$label: $name" "missing from the results (the check script stopped early)"; }
  done
  excerpt "$debug" "$label debug"
}

clean_probe_files() {
  cexec rm -f /etc/.sandbox-probe /usr/.sandbox-probe >/dev/null 2>&1
  rm -f "$PROBE_DIR"/write-allowed.probe
}
left_behind() {  # left_behind LABEL
  if cexec test -e /etc/.sandbox-probe || cexec test -e /usr/.sandbox-probe; then
    fail "$1: from outside, a denied write left a file in /etc or /usr"
  else
    pass "$1: from outside, no file in /etc or /usr"
  fi
}

say ""
say "==> srt (the pinned package directly)"
cat > "$PROBE_DIR/srt-settings.json" <<EOF
{
  "network": { "allowedDomains": ["registry.npmjs.org"], "deniedDomains": [] },
  "filesystem": { "denyRead": [], "allowWrite": ["$PROBE_DIR"], "denyWrite": [] },
  "enableWeakerNestedSandbox": true
}
EOF
rm -f "$PROBE_DIR/srt-results.txt" "$PROBE_DIR/srt-debug.txt"
clean_probe_files
with_timeout 120 container exec -w "$PROBE_DIR" -e SRT_DEBUG=1 "$NAME" \
  srt --debug -s "$PROBE_DIR/srt-settings.json" \
  -c "bash $PROBE_DIR/sandbox-checks.sh $PROBE_DIR/srt-results.txt" \
  </dev/null > "$PROBE_DIR/srt-debug.txt" 2>&1
say "    srt exit $?"
judge srt "$PROBE_DIR/srt-results.txt" "$PROBE_DIR/srt-debug.txt" "$PROBE_DIR/srt-debug.txt"
left_behind srt

say ""
say "==> Claude Code (claude -p, one sandboxed Bash call)"
rm -f "$PROBE_DIR/claude-results.txt" "$PROBE_DIR/claude-debug.txt" "$PROBE_DIR/claude-output.txt"
clean_probe_files
PROMPT="Use the Bash tool to run exactly this command, once, unchanged, and run nothing else: bash $PROBE_DIR/sandbox-checks.sh $PROBE_DIR/claude-results.txt
Then reply with the single word DONE."
run_claude() {
  rm -f "$PROBE_DIR/claude-results.txt" "$PROBE_DIR/claude-debug.txt" "$PROBE_DIR/claude-output.txt"
  with_timeout 300 container exec -w "$PROBE_DIR" "$NAME" \
    claude -p --model haiku --max-turns 4 --allowedTools Bash \
    --debug-file "$PROBE_DIR/claude-debug.txt" "$PROMPT" \
    </dev/null > "$PROBE_DIR/claude-output.txt" 2>&1
  say "    claude exit $?; reply: $(head -c 200 "$PROBE_DIR/claude-output.txt" | tr '\n' ' ')"
}
auth_failed() {
  grep -qiE 'not logged in|/login|auth login|failed to authenticate|invalid api key' "$PROBE_DIR/claude-output.txt" 2>/dev/null
}
run_claude
if auth_failed && [[ -t 0 ]]; then
  # The shared login (~/.claude-containers/shared) has expired. Log in inside
  # the probe container, which renews it for every start-claude.sh container,
  # then retry once.
  say "    the shared Claude login has expired: running 'claude auth login' in $NAME"
  echo "    Finish the login in your browser; the check retries afterwards."
  container exec -it -w "$PROBE_DIR" "$NAME" claude auth login
  say "    claude auth login exit $?"
  clean_probe_files
  run_claude
fi
if auth_failed; then
  fail "Claude Code is not logged in" "the sandbox was not exercised; log in (claude auth login in any start-claude.sh container), then rerun"
else
  judge claude "$PROBE_DIR/claude-results.txt" "$PROBE_DIR/claude-debug.txt" "$PROBE_DIR/claude-output.txt"
fi
left_behind claude

container stop "$NAME" >/dev/null 2>&1 || true
summary
