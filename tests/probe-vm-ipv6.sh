#!/usr/bin/env bash
# probe-vm-ipv6.sh — can tinyproxy reach anything over IPv6 from the Colima VM?
#
# CLAUDE_AGENT_OUT6 (ADR-050) should refuse it at once, with "Permission denied".
# Link-local (fe80::) is not probed: a DNS answer cannot carry the scope id a
# link-local connect needs. Containers are not probed: on a non-IPv6 Docker
# network, IPv6 is disabled on their eth0.
#
# FAIL means tinyproxy has a path: a Mac address on a shared prefix that connects
# or actively refuses (a refusal is the Mac answering), or a default route, which
# is reported even though the rule covers it, since it is new information.
# WARN means a timeout: the rule answers at once, so it did not fire.
#
# Run from the macOS host, with the claude-agent VM running.
#   tests/probe-vm-ipv6.sh

set -u

PROFILE=claude-agent
PORTS="8000 11434"
PASS=0; FAIL=0; WARN=0

pass() { printf 'PASS  %s\n' "$1"; PASS=$((PASS+1)); }
fail() { printf 'FAIL  %s — %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
warn() { printf 'WARN  %s — %s\n' "$1" "$2"; WARN=$((WARN+1)); }

# The command goes over stdin, as in start-agent.sh, so that no quoting has to
# survive colima ssh. -g stops curl globbing the brackets in the IPv6 literal.
probe() { # probe <ipv6> <port>
  printf "curl -g -v --noproxy '*' --connect-timeout 5 -o /dev/null 'http://[%s]:%s/' 2>&1\n" "$1" "$2" \
    | colima ssh -p "$PROFILE" -- sudo -u tinyproxy sh 2>&1
}

if ! colima list -p "$PROFILE" --json 2>/dev/null | grep -q '"Running"'; then
  echo "error: Colima VM '$PROFILE' is not running; start it with start-agent.sh" >&2
  exit 1
fi

routes=$(colima ssh -p "$PROFILE" -- ip -6 route </dev/null 2>/dev/null | tr -d '\r')

if grep -q '^default' <<<"$routes"; then
  fail "no IPv6 default route" "$(grep '^default' <<<"$routes" | head -n 1)"
else
  pass "no IPv6 default route"
fi

# Only the /64 form "aaaa:bbbb:cccc:dddd::/64" is matched, which is what router
# advertisements install. Anything else is reported rather than silently passed.
prefixes=$(awk '$1 !~ /^fe80:/ && $1 != "default" {print $1}' <<<"$routes")
mac_addrs=$(ifconfig | awk '$1 == "inet6" {sub(/%.*/, "", $2); print $2}')

if [[ -z "$prefixes" ]]; then
  pass "no routable IPv6 prefix in the VM"
fi

for prefix in $prefixes; do
  echo "==> prefix $prefix"
  if [[ "$prefix" != *::/64 ]]; then
    warn "prefix $prefix" "not a /64, so not checked against the Mac's addresses"
    continue
  fi
  stem=${prefix%:/64}
  matches=$(grep "^$stem" <<<"$mac_addrs" || true)
  if [[ -z "$matches" ]]; then
    pass "the Mac has no address in $prefix"
    continue
  fi
  for addr in $matches; do
    for port in $PORTS; do
      out=$(probe "$addr" "$port")
      label="tinyproxy user to [$addr]:$port"
      if [[ "$out" == *"Connected to"* ]]; then
        fail "$label" "CONNECTED: something on the Mac answers on this port over IPv6"
      elif [[ "$out" == *"refused"* ]]; then
        fail "$label" "refused by the Mac, so the path is open, with nothing listening on this port"
      elif [[ "$out" == *"Permission denied"* ]]; then
        pass "$label: refused by CLAUDE_AGENT_OUT6"
      elif [[ "$out" == *"No route to host"* || "$out" == *"unreachable"* ]]; then
        pass "$label: no path"
      else
        warn "$label" "$(printf '%s' "$out" | grep -v '^time=' | tail -n 1)"
      fi
    done
  done
done

echo "==> IPv6 listeners on the Mac, for context:"
for port in $PORTS; do
  lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 && $5 == "IPv6" {print "    " $1, $9}'
done

printf '==> %d passed, %d failed, %d warnings\n' "$PASS" "$FAIL" "$WARN"
(( FAIL == 0 ))
