#!/usr/bin/env bash
# test-offline-agent-firewall.sh — validate the offline-agent.py no-egress
# firewall from inside a running offline-agent session container.
#
# Run it from the session's project directory, either in the container shell
# (when the project is this repo):
#     bash tests/test-offline-agent-firewall.sh
# or from the macOS host, in the session's project directory, while the
# session is open in another terminal:
#     docker --context colima-offline-agent exec -i \
#       "offline-agent-$(printf %s "$PWD" | shasum -a 256 | cut -c1-12)" \
#       bash -s < /path/to/start-claude/tests/test-offline-agent-firewall.sh
# (the container name is offline-agent- plus the first 12 hex chars of the
# SHA-256 of the project path; list running ones with
#  docker --context colima-offline-agent ps --filter 'name=^offline-agent-')
#
# Exit 0 if every test either passes or is legitimately skipped (inference
# server not running). Exit 1 on any failure.

set -u

PASS=0
FAIL=0
SKIP=0

if [[ -t 1 ]]; then
  C_OK=$'\e[32m'
  C_ERR=$'\e[31m'
  C_WARN=$'\e[33m'
  C_RST=$'\e[0m'
else
  C_OK=""; C_ERR=""; C_WARN=""; C_RST=""
fi

pass() { printf '%sPASS%s  %s\n' "$C_OK" "$C_RST" "$1"; PASS=$((PASS+1)); }
fail() { printf '%sFAIL%s  %s%s\n' "$C_ERR" "$C_RST" "$1" "${2:+ — $2}"; FAIL=$((FAIL+1)); }
skip() { printf '%sSKIP%s  %s%s\n' "$C_WARN" "$C_RST" "$1" "${2:+ — $2}"; SKIP=$((SKIP+1)); }

echo "==> offline-agent firewall smoke tests"
echo

INFERENCE_URL="${OMLX_HOST:-${OLLAMA_HOST:-}}"
if [[ -z "$INFERENCE_URL" ]]; then
  printf '%swarning%s: neither OMLX_HOST nor OLLAMA_HOST is set — are you inside an offline-agent container?\n\n' \
    "$C_WARN" "$C_RST"
fi

tcp_open() { timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

default_gateway() {
  local gw_hex
  gw_hex=$(awk '$2 == "00000000" { print $3; exit }' /proc/net/route)
  [[ -n "$gw_hex" ]] || return 1
  printf '%d.%d.%d.%d' "0x${gw_hex:6:2}" "0x${gw_hex:4:2}" "0x${gw_hex:2:2}" "0x${gw_hex:0:2}"
}

# ── 1. Inference carve-out ───────────────────────────────────────────────────
# The one permitted destination. ECONNREFUSED (curl exit 7) means the packet
# reached the host and nothing listened: the carve-out works, the server is
# just down, so SKIP rather than FAIL.
test_inference_reachable() {
  if [[ -z "$INFERENCE_URL" ]]; then
    skip "inference-carveout" "no OMLX_HOST/OLLAMA_HOST"
    return
  fi
  local path=/v1/models
  [[ -n "${OLLAMA_HOST:-}" ]] && path=/api/tags
  local auth=()
  [[ -n "${OMLX_API_KEY:-}" ]] && auth=(-H "Authorization: Bearer $OMLX_API_KEY")
  local out rc
  out=$(curl -sS --max-time 5 "${auth[@]}" "$INFERENCE_URL$path" 2>&1)
  rc=$?
  if (( rc == 0 )); then
    pass "inference-carveout: $INFERENCE_URL reachable"
  elif (( rc == 7 )) && [[ "$out" == *"Couldn't connect"* ]]; then
    skip "inference-carveout" "server not running on host (ECONNREFUSED from the host, not an iptables reject)"
  else
    fail "inference-carveout" "curl exit $rc, out=${out:0:120}"
  fi
}

# ── 2. Other ports on the inference host ─────────────────────────────────────
# The carve-out is one port, not the whole host: the Mac's sshd must be refused.
test_inference_host_other_port_blocked() {
  if [[ -z "$INFERENCE_URL" ]]; then
    skip "inference-host-other-port" "no OMLX_HOST/OLLAMA_HOST"
    return
  fi
  local host="${INFERENCE_URL#http://}"
  host="${host%%:*}"
  if tcp_open "$host" 22; then
    fail "inference-host-other-port" "$host:22 reachable; carve-out is wider than the inference port"
  else
    pass "inference-host-other-port: $host:22 refused"
  fi
}

# ── 3. Direct egress ─────────────────────────────────────────────────────────
# By IP, so a pass cannot be explained by DNS alone.
test_egress_by_ip_blocked() {
  local rc
  curl --noproxy '*' -sS --max-time 5 http://1.1.1.1/ >/dev/null 2>&1
  rc=$?
  if (( rc != 0 )); then
    pass "egress-by-ip: 1.1.1.1 blocked (curl exit $rc)"
  else
    fail "egress-by-ip" "reached 1.1.1.1 — OFFLINE_AGENT REJECT not applied"
  fi
}

test_egress_by_name_blocked() {
  local rc
  curl --noproxy '*' -sS --max-time 5 https://example.com >/dev/null 2>&1
  rc=$?
  if (( rc != 0 )); then
    pass "egress-by-name: example.com blocked (curl exit $rc)"
  else
    fail "egress-by-name" "reached https://example.com"
  fi
}

# ── 4. DNS blackholed, failing fast ──────────────────────────────────────────
# A name must never resolve, and the failure should come from the REJECT, not
# a silent drop. The REJECT's ICMP reply is rate-limited by the kernel (~1/s
# per destination), so right after the egress tests a lookup can still wait
# out Docker's embedded-DNS 4 s upstream timeout. One fast failure proves the
# REJECT is in the path (a silent drop is slow every time), so retry after a
# pause before calling it slow.
test_external_dns_blackholed() {
  local attempt start end ms slowest=0
  for attempt in 1 2 3; do
    sleep 3
    start=$(date +%s%N)
    if getent hosts example.com >/dev/null 2>&1; then
      fail "external-dns-blackholed" "example.com resolved — DNS is an exfil channel"
      return
    fi
    end=$(date +%s%N)
    ms=$(( (end - start) / 1000000 ))
    if (( ms < 2000 )); then
      pass "external-dns-blackholed: example.com did not resolve (${ms} ms, attempt $attempt)"
      return
    fi
    (( ms > slowest )) && slowest=$ms
  done
  fail "external-dns-blackholed" "never resolved, but every attempt took ≥2 s (slowest ${slowest} ms): queries look silently dropped, not REJECTed"
}

# ── 5. VM-local services closed ──────────────────────────────────────────────
# No proxy to carve out, so every VM address is closed (OFFLINE_AGENT_IN):
# sshd on the network gateway and docker0, dnsmasq-style DNS over TCP.
test_vm_local_services_closed() {
  local gw
  if ! gw=$(default_gateway); then
    fail "vm-local-services-closed" "could not determine the default gateway"
    return
  fi
  local target
  for target in "$gw:22" "$gw:53" "172.17.0.1:22" "192.168.5.1:53"; do
    if tcp_open "${target%:*}" "${target##*:}"; then
      fail "vm-local-services-closed" "$target reachable; OFFLINE_AGENT_IN not applied"
    else
      pass "vm-local-services-closed: $target refused"
    fi
  done
}

# ── 6. No proxy environment ──────────────────────────────────────────────────
test_no_proxy_env() {
  local set=()
  for var in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy; do
    [[ -n "${!var:-}" ]] && set+=("$var")
  done
  if (( ${#set[@]} == 0 )); then
    pass "no-proxy-env: no proxy variables set"
  else
    fail "no-proxy-env" "set: ${set[*]}"
  fi
}

# ── 7. Project dir is the overlay ────────────────────────────────────────────
# Writes in the working directory must land in the overlay, never the host.
test_project_is_overlay() {
  local fstype
  fstype=$(findmnt -n -o FSTYPE -T "$PWD" 2>/dev/null)
  if [[ "$fstype" == "overlay" ]]; then
    pass "project-is-overlay: $PWD is on overlay"
  else
    fail "project-is-overlay" "$PWD is on '${fstype:-unknown}', not overlay — run from the session's project dir"
  fi
}

test_inference_reachable
test_inference_host_other_port_blocked
test_egress_by_ip_blocked
test_egress_by_name_blocked
test_external_dns_blackholed
test_vm_local_services_closed
test_no_proxy_env
test_project_is_overlay

echo
printf '==> %s%d passed%s, %s%d failed%s, %s%d skipped%s\n' \
  "$C_OK" "$PASS" "$C_RST" \
  "$C_ERR" "$FAIL" "$C_RST" \
  "$C_WARN" "$SKIP" "$C_RST"

(( FAIL == 0 ))
