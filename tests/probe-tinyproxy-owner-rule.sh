#!/usr/bin/env bash
# probe-tinyproxy-owner-rule.sh — live check of CLAUDE_AGENT_OUT's owner-match
# rule, which test-agent-firewall.sh cannot reach from inside the container (see
# the comment above assert_tinyproxy_filters there).
#
# As the tinyproxy user, connections to the macOS host and to each private range
# must fail with "No route to host". As the VM's normal user, the host must not,
# which shows the refusal comes from the owner match and not from routing.
#
# Run from the macOS host. Requires colima, with the claude-agent VM running.
# The port only has to be one the host answers on for the control: 11434 for
# Ollama (the default), 8000 for omlx.
#   tests/probe-tinyproxy-owner-rule.sh [port]

set -u

PROFILE=claude-agent
PORT=${1:-11434}
PASS=0; FAIL=0

pass() { printf 'PASS  %s\n' "$1"; PASS=$((PASS+1)); }
fail() { printf 'FAIL  %s — %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }

# The command goes over stdin, as in start-agent.sh, so that no quoting has to
# survive colima ssh. -v because curl 8 reports only "Couldn't connect to server"
# on its error line; the errno appears only in the verbose trace.
probe() { # probe <ip> [sudo prefix...]
  local ip=$1; shift
  printf "curl -v --noproxy '*' --connect-timeout 5 -o /dev/null 'http://%s:%s/' 2>&1\n" "$ip" "$PORT" \
    | colima ssh -p "$PROFILE" -- "$@" sh 2>&1
}

if ! colima list -p "$PROFILE" --json 2>/dev/null | grep -q '"Running"'; then
  echo "error: Colima VM '$PROFILE' is not running; start it with start-agent.sh" >&2
  exit 1
fi

HOST_IP=$(colima ssh -p "$PROFILE" -- ip route show default </dev/null 2>/dev/null \
  | tr -d '\r' | awk '/^default/ {print $3; exit}')
if [[ -z "$HOST_IP" ]]; then
  echo "error: could not read the macOS host IP from the VM's default route" >&2
  exit 1
fi
echo "==> host=$HOST_IP port=$PORT"

for ip in "$HOST_IP" 10.0.0.1 172.16.0.1 192.168.0.1 169.254.0.1 100.64.0.1; do
  out=$(probe "$ip" sudo -u tinyproxy)
  if [[ "$out" == *"No route to host"* ]]; then
    pass "tinyproxy user refused: $ip"
  else
    # A timeout lands here too: the REJECT answers at once, so it did not fire.
    fail "tinyproxy user refused: $ip" "expected 'No route to host', got: $(printf '%s' "$out" | tail -n 2 | tr '\n' ' ')"
  fi
done

out=$(probe "$HOST_IP")
if [[ "$out" == *"No route to host"* ]]; then
  fail "control: normal user reaches $HOST_IP" "also 'No route to host', so the refusals above do not prove the owner match"
elif [[ "$out" == *"Connected to"* || "$out" == *"Connection refused"* ]]; then
  pass "control: normal user reaches $HOST_IP"
else
  fail "control: normal user reaches $HOST_IP" "inconclusive: $(printf '%s' "$out" | tail -n 2 | tr '\n' ' ')"
fi

printf '==> %d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
