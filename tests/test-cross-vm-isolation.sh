#!/usr/bin/env bash
# test-cross-vm-isolation.sh — assert that claude-agent and research Colima
# VMs cannot reach each other's containers.
#
# Security-relevant invariant: the two-VM architecture provides isolation.
# Exit 1 on any FAIL so a regression in isolation is immediately visible.
# Skips (non-fail, exit 0) if either VM is not running.
#
# Covers:
#   - From claude-agent: research-searxng and research-vane unreachable by
#     container name and via host.docker.internal:3000 (Vane's host bind).
#   - From research-searxng: claude-agent and searxng (agent's) unreachable.
#   - Positive: claude-agent reaches the in-VM inference proxy, and the research
#     VM reaches the macOS host inference port directly, confirming the negative
#     tests aren't over-blocking. claude-agent has no direct path to the host;
#     test-agent-firewall.sh asserts that from inside the container.
#
# Run from the macOS host. Requires colima and docker on PATH.

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

echo "==> cross-VM isolation tests (claude-agent ↔ research)"
echo

# ── VM availability ───────────────────────────────────────────────────────────
vm_running() { colima status -p "$1" 2>/dev/null | grep -q "Running"; }

AGENT_UP=false; RESEARCH_UP=false
vm_running claude-agent && AGENT_UP=true
vm_running research     && RESEARCH_UP=true

if ! $AGENT_UP && ! $RESEARCH_UP; then
  skip "all" "neither claude-agent nor research VM is running — start both to run these tests"
  echo
  printf '==> %s%d passed%s, %s%d failed%s, %s%d skipped%s\n' \
    "$C_OK" "$PASS" "$C_RST" "$C_ERR" "$FAIL" "$C_RST" "$C_WARN" "$SKIP" "$C_RST"
  exit 0
fi

# ── From claude-agent: cannot reach research containers ──────────────────────
# curl exit 6  = Could not resolve host (DNS isolated — good)
# curl exit 7  = Connection refused (connected but refused — good)
# curl exit 0  = Connection succeeded — ISOLATION FAILURE
# All other non-zero exits are also treated as isolated.
agent_cannot_reach() {
  local label=$1 url=$2 note=${3:-}
  local out rc
  out=$(colima ssh -p claude-agent -- docker exec claude-agent \
    curl --noproxy '*' -sS --connect-timeout 5 "$url" 2>&1)
  rc=$?
  if (( rc == 0 )); then
    fail "$label" "ISOLATION BREACH: claude-agent reached $url${note:+ ($note)} — got: ${out:0:120}"
  else
    pass "$label: blocked (curl exit $rc)"
  fi
}

if $AGENT_UP && $RESEARCH_UP; then
  # Container name DNS and port reachability within research-net
  agent_cannot_reach "agent→research-searxng:8080" \
    "http://research-searxng:8080/" "SearXNG HTTP"
  agent_cannot_reach "agent→research-searxng:8888" \
    "http://research-searxng:8888/" "Squid proxy port"
  agent_cannot_reach "agent→research-vane:3000" \
    "http://research-vane:3000/" "Vane internal port"
  # Vane publishes port 3000 to the macOS host (-p 3000:3000). From inside
  # claude-agent, host.docker.internal resolves to the macOS host. CLAUDE_AGENT
  # has no rule opening any port there, so the REJECT rule fires.
  agent_cannot_reach "agent→vane-host-bind:3000" \
    "http://host.docker.internal:3000/" "research-vane host port bind"
elif $AGENT_UP && ! $RESEARCH_UP; then
  skip "agent→research-containers" \
    "research VM not running — start both VMs to test isolation"
elif ! $AGENT_UP; then
  skip "agent→research-containers" "claude-agent VM not running"
fi

# ── From research-searxng: cannot reach claude-agent containers ───────────────
# SearXNG runs Alpine+Python. Use Python3 socket for connectivity probes;
# curl is not guaranteed on the Alpine image.
#
# Exit semantics for the Python probe:
#   0 = connected (ISOLATION FAILURE)
#   1 = DNS failure or blocked by iptables (expected)
research_cannot_reach() {
  local label=$1 host=$2 port=$3
  local out rc
  # Single-quoted to avoid local shell expansion; host/port are safe literals.
  # The heredoc-style script is piped via stdin (-i flag on docker exec).
  out=$(colima ssh -p research -- docker exec -i research-searxng python3 - "$host" "$port" <<'PYEOF' 2>&1
import socket, sys
h, p = sys.argv[1], int(sys.argv[2])
try:
    s = socket.create_connection((h, p), timeout=5)
    s.close()
    print(f"connected to {h}:{p}")
    sys.exit(0)
except socket.gaierror as e:
    print(f"dns_failure {h}: {e}")
    sys.exit(1)
except OSError as e:
    print(f"blocked {h}:{p}: {e}")
    sys.exit(1)
PYEOF
)
  rc=$?
  if (( rc == 0 )); then
    fail "$label" "ISOLATION BREACH: research-searxng reached $host:$port — got: ${out:0:120}"
  else
    pass "$label: blocked (${out%%$'\n'*})"
  fi
}

if $AGENT_UP && $RESEARCH_UP; then
  research_cannot_reach "research→claude-agent:8080"  "claude-agent" 8080
  research_cannot_reach "research→agent-searxng:8080" "searxng"      8080
elif $RESEARCH_UP && ! $AGENT_UP; then
  skip "research→agent-containers" \
    "claude-agent VM not running — start both VMs to test isolation"
elif ! $RESEARCH_UP; then
  skip "research→agent-containers" "research VM not running"
fi

# ── Positive: inference endpoints still work ─────────────────────────────────
# claude-agent reaches inference only through the nginx proxy in its VM. A
# refused connection is no longer evidence of anything here: nginx answers 502
# when the backend is down, and the container has no direct path to probe. So the
# proxy is proved by a route it refuses itself, with nginx's own error page, which
# needs no backend. An allowed route then shows the proxy reaches the host.
agent_inference_get() {  # URL; prints the body then the 3-digit status
  colima ssh -p claude-agent -- docker exec claude-agent \
    curl --noproxy '*' -sS --max-time 10 -w '%{http_code}' "$1" 2>&1
}

test_agent_inference_proxy() {
  local url proxy out rc status
  # Container env, as a fresh one sets exactly one of these.
  url=$(colima ssh -p claude-agent -- \
    docker exec claude-agent bash -c 'echo "${OMLX_HOST:-${OLLAMA_HOST:-}}"' 2>/dev/null)
  proxy=$(colima ssh -p claude-agent -- \
    docker exec claude-agent bash -c 'echo "${HTTPS_PROXY:-}"' 2>/dev/null)
  url="${url%/}"
  proxy="${proxy#http://}"
  proxy="${proxy%:*}"

  if [[ -z "$url" ]]; then
    skip "agent-inference-proxy" "neither OMLX_HOST nor OLLAMA_HOST set in claude-agent container"
    return
  fi
  local addr="${url#http://}"
  if [[ -z "$proxy" || "${addr%:*}" != "$proxy" ]]; then
    fail "agent-inference-proxy" \
      "$url is not on the proxy's address (${proxy:-unknown}); container predates it? start-agent.sh --reset-container"
    return
  fi

  out=$(agent_inference_get "$url/admin/api/global-settings")
  rc=$?
  status="${out: -3}"
  if (( rc == 0 )) && [[ "$status" == 403 && "$out" == *"<center>nginx"* ]]; then
    pass "agent-inference-proxy: $addr answered by nginx (denied route → 403)"
  else
    fail "agent-inference-proxy" \
      "expected 403 from nginx at $addr, got curl exit $rc; out=${out:0:120}"
    return
  fi

  out=$(agent_inference_get "$url/v1/models")
  rc=$?
  status="${out: -3}"
  if (( rc == 0 )) && [[ "$status" == 200 ]]; then
    pass "agent-inference-proxy: $addr reaches the host backend (/v1/models → 200)"
  elif (( rc == 0 )) && [[ "$status" == 502 || "$status" == 504 ]]; then
    skip "agent-inference-proxy: backend" "inference backend not running (proxy answered $status)"
  else
    fail "agent-inference-proxy" \
      "unexpected result for $addr/v1/models: curl exit $rc; out=${out:0:120}"
  fi
}

# research.py still has a direct iptables carve-out, so ECONNREFUSED proves it.
test_research_inference_carveout() {
  # research-searxng is Alpine+Python, no curl. Use Python socket.
  # Discover the macOS host IP from the research VM's default route.
  local host_ip rc out
  host_ip=$(colima ssh -p research -- ip route show default 2>/dev/null \
    | awk '/^default/{print $3; exit}')

  if [[ -z "$host_ip" ]]; then
    skip "research-inference-carveout" "could not discover host IP from research VM"
    return
  fi

  # Probe Ollama port (11434). Exit 2 = ECONNREFUSED = carve-out works.
  out=$(colima ssh -p research -- docker exec -i research-searxng python3 - "$host_ip" <<'PYEOF' 2>&1
import socket, sys
host_ip = sys.argv[1]
try:
    s = socket.create_connection((host_ip, 11434), timeout=5)
    s.close()
    print(f"connected to {host_ip}:11434")
    sys.exit(0)
except ConnectionRefusedError:
    print(f"connection_refused {host_ip}:11434 (Ollama not running)")
    sys.exit(2)
except OSError as e:
    print(f"blocked {host_ip}:11434: {e}")
    sys.exit(1)
PYEOF
)
  rc=$?

  if (( rc == 0 )); then
    pass "research-inference-carveout: $host_ip:11434 reachable (Ollama running)"
  elif (( rc == 2 )); then
    skip "research-inference-carveout: Ollama not running (iptables carve-out works: ECONNREFUSED from host)"
  else
    fail "research-inference-carveout" \
      "unexpected result for $host_ip:11434 — carve-out may be broken; out=${out:0:120}"
  fi
}

if $AGENT_UP;   then test_agent_inference_proxy;       fi
if $RESEARCH_UP; then test_research_inference_carveout; fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
printf '==> %s%d passed%s, %s%d failed%s, %s%d skipped%s\n' \
  "$C_OK" "$PASS" "$C_RST" "$C_ERR" "$FAIL" "$C_RST" "$C_WARN" "$SKIP" "$C_RST"

(( FAIL == 0 ))
