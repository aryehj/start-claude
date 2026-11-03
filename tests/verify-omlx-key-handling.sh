#!/usr/bin/env bash
# verify-omlx-key-handling.sh — live check for plans/fix-omlx-key-handling.md.
#
# Run from the macOS host, with omlx serving gemma-4-e4b-it-6bit and the
# claude-agent container already started once with the key:
#     OMLX_API_KEY=<key> start-agent.sh --backend=omlx
# This script never needs the key itself. It checks:
#   1. Precondition: the inference proxy serves omlx (port 8000) and holds a key.
#   2. The container gets 200 from omlx through the proxy.
#   3. A keyless `start-agent.sh --reload-allowlist` keeps omlx as the backend
#      and keeps the key (ADR-053), says so, and prints no key material.
#   4. Afterwards the VM config still holds the key, and the container still
#      gets 200 (not 401).
#   5. Phase 2: the startup probe's exact keyless curl, run from the VM, counts
#      omlx as reachable. Records the HTTP code omlx gives an unauthenticated
#      request (the plan's open Unknown).
#   6. The nginx-backed unit tests, if nginx and uv are on the Mac.
# Full output goes to .verify/verify-omlx-key-handling.log.

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
mkdir -p .verify
LOG="$REPO/.verify/verify-omlx-key-handling.log"
exec > >(tee "$LOG") 2>&1

PROFILE=claude-agent
CONTAINER=claude-agent
START_AGENT="${START_AGENT:-$REPO/start-agent.sh}"  # overridable for dry runs
CONF=/etc/nginx/conf.d/claude-agent-inference.conf
PASS=0; FAIL=0; SKIP=0
pass() { echo "PASS  $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL  $1${2:+ — $2}"; FAIL=$((FAIL+1)); }
skip() { echo "SKIP  $1${2:+ — $2}"; SKIP=$((SKIP+1)); }
summary() {
  echo
  echo "==> $PASS passed, $FAIL failed, $SKIP skipped (log: $LOG)"
  [[ $FAIL -eq 0 ]]
  exit $?
}
die() { echo "error: $1"; echo "    $2"; FAIL=$((FAIL+1)); summary; }

# colima ssh re-quotes argv, so arguments with spaces or quotes arrive mangled.
# Like start-agent.sh's vm_ssh, %q-join them and pipe the line in on stdin.
vm() {
  local cmd="" a
  for a in "$@"; do cmd+=" $(printf '%q' "$a")"; done
  vm_sh "$cmd"
}
vm_sh() { printf '%s\n' "$1" | colima ssh -p "$PROFILE" -- bash; }
key_count() { vm sudo grep -c '^  proxy_set_header Authorization "Bearer ' "$CONF" 2>/dev/null | tr -d '[:space:]'; }
proxy_port() { vm sudo sed -n 's/^  listen [^ ]*:\([0-9][0-9]*\) default_server;$/\1/p' "$CONF" 2>/dev/null | head -n 1 | tr -d '[:space:]'; }
container_code() {
  vm docker exec "$CONTAINER" curl --noproxy '*' -s -o /dev/null -w '%{http_code}' \
    --max-time 10 "http://$BRIDGE_IP:8000/v1/models" 2>/dev/null
}

echo "==> omlx key handling live check ($(date '+%Y-%m-%d %H:%M:%S'))"
echo

# ── preconditions ─────────────────────────────────────────────────────────────
command -v colima >/dev/null || die "colima not on PATH" "Run this on the macOS host."
colima list -p "$PROFILE" --json 2>/dev/null | grep -q '"Running"' \
  || die "Colima VM '$PROFILE' is not running" "Start it once with: OMLX_API_KEY=<key> start-agent.sh --backend=omlx"
[[ "$(vm docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | tr -d '[:space:]')" == true ]] \
  || die "container '$CONTAINER' is not running" "Start it once with: OMLX_API_KEY=<key> start-agent.sh --backend=omlx"
BRIDGE_IP=$(vm docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null | tr -d '[:space:]')
HOST_IP=$(vm ip route show default 2>/dev/null | awk '/^default/ {print $3; exit}')
[[ -n "$BRIDGE_IP" && -n "$HOST_IP" ]] || die "could not read the VM's bridge or host IP" "Check: colima ssh -p $PROFILE -- ip route"
[[ "$(proxy_port)" == 8000 && "$(key_count)" == 1 ]] \
  || die "the inference proxy is not serving omlx with a key (port '$(proxy_port)', key lines '$(key_count)')" \
         "Run once: OMLX_API_KEY=<key> start-agent.sh --backend=omlx  — then rerun this script."
pass "precondition: proxy serves omlx on 8000 and holds a key"

# ── 2. baseline through the proxy ─────────────────────────────────────────────
code=$(container_code)
[[ "$code" == 200 ]] && pass "container → proxy → omlx before reload: 200" \
  || fail "container → proxy → omlx before reload" "got HTTP '$code', want 200"

# ── 3. keyless --reload-allowlist ─────────────────────────────────────────────
echo
echo "==> start-agent.sh --reload-allowlist with OMLX_API_KEY unset"
RELOAD_OUT=$(env -u OMLX_API_KEY "$START_AGENT" --reload-allowlist 2>&1); rc=$?
printf '%s\n' "$RELOAD_OUT" | sed 's/^/    | /'
echo
[[ $rc -eq 0 ]] && pass "reload exits 0" || fail "reload exits 0" "exit $rc"
grep -qF '==> Inference backend: omlx (the one the inference proxy already serves; --backend switches it)' <<<"$RELOAD_OUT" \
  && pass "reload keeps omlx as the backend without --backend" \
  || fail "reload keeps omlx as the backend" "expected 'Inference backend: omlx (the one …)' line"
grep -qF '==> OMLX_API_KEY not set; keeping the key the inference proxy already holds' <<<"$RELOAD_OUT" \
  && pass "reload says it keeps the proxy's key" \
  || fail "reload says it keeps the proxy's key" "expected 'keeping the key' line"
grep -qF 'warning: OMLX_API_KEY not set' <<<"$RELOAD_OUT" \
  && fail "reload prints no missing-key warning" "warned that requests will fail" \
  || pass "reload prints no missing-key warning"
grep -q 'Bearer' <<<"$RELOAD_OUT" \
  && fail "reload output carries no key material" "found 'Bearer' in the output" \
  || pass "reload output carries no key material"

# ── 4. state after reload ─────────────────────────────────────────────────────
[[ "$(key_count)" == 1 ]] && pass "VM proxy config still holds the key" \
  || fail "VM proxy config still holds the key" "key lines: '$(key_count)'"
[[ "$(proxy_port)" == 8000 ]] && pass "proxy still listens on 8000 (omlx)" \
  || fail "proxy still listens on 8000" "port: '$(proxy_port)'"
code=$(container_code)
[[ "$code" == 200 ]] && pass "container → proxy → omlx after reload: 200" \
  || fail "container → proxy → omlx after reload" "got HTTP '$code', want 200 (401 means the key was dropped)"

# ── 5. phase 2: the startup probe's keyless curl ─────────────────────────────
if vm curl -s -o /dev/null --max-time 3 "http://$HOST_IP:8000/v1/models" >/dev/null 2>&1; then
  pass "startup probe's keyless curl reaches omlx from the VM"
else
  fail "startup probe's keyless curl reaches omlx from the VM" "curl failed; start-agent.sh would warn omlx is unreachable"
fi
unauth=$(vm curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://$HOST_IP:8000/v1/models" 2>/dev/null)
echo "INFO  omlx answers an unauthenticated GET /v1/models with HTTP $unauth"

# ── 6. nginx-backed unit tests ────────────────────────────────────────────────
echo
if NGINX=$(command -v nginx) && command -v uv >/dev/null; then
  if NGINX_BIN="$NGINX" uv run --with pytest pytest -q tests/test_agent_sh.py; then
    pass "tests/test_agent_sh.py with NGINX_BIN=$NGINX"
  else
    fail "tests/test_agent_sh.py with NGINX_BIN=$NGINX"
  fi
else
  skip "nginx-backed unit tests" "needs nginx and uv on the Mac (brew install nginx)"
fi

summary
