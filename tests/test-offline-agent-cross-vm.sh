#!/usr/bin/env bash
# test-offline-agent-cross-vm.sh — assert that an offline-agent session and the
# claude-agent / research VMs run side by side without either firewall
# affecting the other, and that the offline-agent container cannot reach them.
#
# Run from the macOS host with an offline-agent session open in another
# terminal (cd ~/some/project && offline-agent.py). Skips (exit 0) whatever is
# not running; exit 1 on any FAIL.
#
# Covers:
#   - From the offline-agent container: the claude-agent and research VM
#     addresses (sshd, tinyproxy, Squid) and research-vane's host bind
#     (host.docker.internal:3000) are unreachable.
#   - Positive, offline-agent: the inference carve-out still works.
#   - Positive, claude-agent: its proxy egress still works with the
#     offline-agent firewall active (an allowlisted host via tinyproxy).

set -u

PASS=0; FAIL=0; SKIP=0

if [[ -t 1 ]]; then
  C_OK=$'\e[32m'; C_ERR=$'\e[31m'; C_WARN=$'\e[33m'; C_RST=$'\e[0m'
else
  C_OK=""; C_ERR=""; C_WARN=""; C_RST=""
fi

pass() { printf '%sPASS%s  %s\n' "$C_OK" "$C_RST" "$1"; PASS=$((PASS+1)); }
fail() { printf '%sFAIL%s  %s%s\n' "$C_ERR" "$C_RST" "$1" "${2:+ — $2}"; FAIL=$((FAIL+1)); }
skip() { printf '%sSKIP%s  %s%s\n' "$C_WARN" "$C_RST" "$1" "${2:+ — $2}"; SKIP=$((SKIP+1)); }

summary() {
  echo
  printf '==> %s%d passed%s, %s%d failed%s, %s%d skipped%s\n' \
    "$C_OK" "$PASS" "$C_RST" "$C_ERR" "$FAIL" "$C_RST" "$C_WARN" "$SKIP" "$C_RST"
  (( FAIL == 0 ))
}

echo "==> cross-VM tests (offline-agent ↔ claude-agent, research)"
echo

# One field of `colima list --json` for a profile (empty if absent). Not
# `colima status`: it reports through its logger, not as parseable stdout.
vm_field() {
  colima list -p "$1" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.loads(sys.stdin.readline()).get(sys.argv[1],""))' "$2" 2>/dev/null
}
vm_running() { [[ "$(vm_field "$1" status)" == "Running" ]]; }
# The VM's --network-address IP.
vm_address() { vm_field "$1" address; }

OA="docker --context colima-offline-agent"

if ! vm_running offline-agent; then
  skip "all" "offline-agent VM not running — start a session with offline-agent.py"
  summary; exit $?
fi
OA_CONTAINER=$($OA ps --filter 'name=^offline-agent-' --format '{{.Names}}' 2>/dev/null | head -n1)
if [[ -z "$OA_CONTAINER" ]]; then
  skip "all" "no offline-agent session container running — open one with offline-agent.py"
  summary; exit $?
fi
echo "    session container: $OA_CONTAINER"
echo

# curl exit 0 = connected = ISOLATION FAILURE; any other exit is blocked.
oa_cannot_reach() {
  local label=$1 url=$2
  local out rc
  out=$($OA exec "$OA_CONTAINER" curl -sS --connect-timeout 5 --max-time 8 "$url" 2>&1)
  rc=$?
  if (( rc == 0 )); then
    fail "$label" "ISOLATION BREACH: offline-agent reached $url — got: ${out:0:120}"
  else
    pass "$label: blocked (curl exit $rc)"
  fi
}

# ── From offline-agent: sibling VMs unreachable ──────────────────────────────
for vm in claude-agent research; do
  if ! vm_running "$vm"; then
    skip "offline→$vm" "$vm VM not running"
    continue
  fi
  addr=$(vm_address "$vm")
  if [[ -z "$addr" ]]; then
    skip "offline→$vm" "could not read $vm VM address from colima list"
    continue
  fi
  oa_cannot_reach "offline→$vm:22 ($addr)" "http://$addr:22/"
  if [[ "$vm" == claude-agent ]]; then
    oa_cannot_reach "offline→$vm tinyproxy ($addr:8888)" "http://$addr:8888/"
  else
    oa_cannot_reach "offline→$vm squid ($addr:8888)" "http://$addr:8888/"
  fi
done

if vm_running research; then
  oa_cannot_reach "offline→vane-host-bind:3000" "http://host.docker.internal:3000/"
fi

# ── Positive: offline-agent inference carve-out ──────────────────────────────
inference_url=$($OA exec "$OA_CONTAINER" bash -c 'echo "${OMLX_HOST:-${OLLAMA_HOST:-}}"' 2>/dev/null | tr -d '\r')
if [[ -z "$inference_url" ]]; then
  fail "offline-inference-carveout" "neither OMLX_HOST nor OLLAMA_HOST set in $OA_CONTAINER"
else
  out=$($OA exec "$OA_CONTAINER" curl -sS --max-time 5 "$inference_url/" 2>&1)
  rc=$?
  # Any HTTP answer (0, 22) or a refusal from the host (7) proves the packet
  # got through; an iptables reject or timeout does not.
  if (( rc == 0 || rc == 22 )); then
    pass "offline-inference-carveout: $inference_url answered"
  elif (( rc == 7 )) && [[ "$out" == *"Couldn't connect"* ]]; then
    skip "offline-inference-carveout" "server not running (ECONNREFUSED from host: carve-out works)"
  else
    fail "offline-inference-carveout" "curl exit $rc for $inference_url; out=${out:0:120}"
  fi
fi

# ── Positive: claude-agent egress unaffected ─────────────────────────────────
if vm_running claude-agent; then
  out=$(colima ssh -p claude-agent -- docker exec claude-agent \
    curl -sS --max-time 10 https://api.github.com/zen 2>&1)
  rc=$?
  if (( rc == 0 )) && [[ -n "$out" ]]; then
    pass "claude-agent-proxy-egress: api.github.com/zen via tinyproxy"
  else
    fail "claude-agent-proxy-egress" "curl exit $rc, out=${out:0:120} (is the claude-agent container running?)"
  fi
else
  skip "claude-agent-proxy-egress" "claude-agent VM not running"
fi

summary
