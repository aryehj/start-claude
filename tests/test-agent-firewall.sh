#!/usr/bin/env bash
# test-agent-firewall.sh — validate the start-agent.sh egress allowlist from
# inside the running claude-agent container.
#
# Covers every smoke test documented in README.md except allowlist hot-reload,
# which requires host-side actions and is not included here.
#
# Exit 0 if every test either passes or is legitimately skipped (inference
# backend not running, search disabled, no vision model named). Exit 1 on any
# failure.
#
# Run in a container created after the inference proxy was added. Optional env:
#   INFERENCE_TEST_MODEL    model for chat requests (default: first in /v1/models)
#   INFERENCE_VISION_MODEL  omlx vision model; the vision tests skip without it,
#                           because /v1/models does not say which models take images
#   INFERENCE_TEST_TIMEOUT  seconds per model request (default: 180; a request may
#                           have to load the model first)

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

echo "==> start-agent firewall smoke tests"
echo

if [[ -z "${HTTPS_PROXY:-}" ]]; then
  printf '%swarning%s: HTTPS_PROXY is not set — are you running this inside the claude-agent container?\n\n' \
    "$C_WARN" "$C_RST"
fi

tcp_open() { timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

# ── Inference proxy discovery ────────────────────────────────────────────────
# Both proxies listen on the docker0 gateway, so $BRIDGE_IP comes from
# HTTPS_PROXY. The backend comes from whichever of OMLX_HOST and OLLAMA_HOST is
# set. A reattach with another --backend can leave both set; the firewall opens
# only the active port, so the one that connects is the active one.
BRIDGE_IP="${HTTPS_PROXY:-}"
BRIDGE_IP="${BRIDGE_IP#http://}"
BRIDGE_IP="${BRIDGE_IP%/}"
BRIDGE_IP="${BRIDGE_IP%:*}"
BACKEND=""
INFERENCE_URL=""
INFERENCE_PORT=""
INFERENCE_MODEL=""
MODEL_TIMEOUT="${INFERENCE_TEST_TIMEOUT:-180}"

detect_inference() {
  local backend var url addr connects
  for backend in omlx ollama; do
    var="${backend^^}_HOST"
    url="${!var:-}"
    url="${url%/}"
    [[ -n "$url" ]] || continue
    addr="${url#http://}"
    connects=false
    [[ "${addr%:*}" == "$BRIDGE_IP" ]] && tcp_open "$BRIDGE_IP" "${addr##*:}" && connects=true
    if [[ -z "$INFERENCE_URL" ]] || $connects; then
      BACKEND="$backend"
      INFERENCE_URL="$url"
      INFERENCE_PORT="${addr##*:}"
    fi
    $connects && return
  done
}
detect_inference

require_inference() {
  [[ -n "$INFERENCE_URL" ]] && return 0
  skip "$1" "neither OMLX_HOST nor OLLAMA_HOST is set (test_env_wiring reports this)"
  return 1
}

# Sends one request straight to the inference proxy; sets RESP_STATUS and RESP_BODY.
# --path-as-is: otherwise curl resolves ../ itself and nginx never sees a traversal.
inference_request() {
  local method=$1 path=$2 out rc
  shift 2
  out=$(curl --noproxy '*' --path-as-is -sS --max-time "$MODEL_TIMEOUT" -X "$method" \
    -w '\n%{http_code}' "$@" "$INFERENCE_URL$path" 2>&1)
  rc=$?
  RESP_STATUS="${out##*$'\n'}"
  RESP_BODY="${out%$'\n'*}"
  if (( rc != 0 )); then
    RESP_STATUS="curl exit $rc"
    RESP_BODY="$out"
  fi
}

# nginx answers 502/504 from the proxy itself when the host's server is down.
backend_down() { [[ "$RESP_STATUS" == 502 || "$RESP_STATUS" == 504 ]]; }

# POST body for a chat request. $1 model, $2 stream (true|false).
chat_body() {
  jq -nc --arg m "$1" --argjson s "$2" \
    '{model: $m, stream: $s, max_tokens: 8, messages: [{role: "user", content: "Reply with OK."}]}'
}

# ── 1. Bridge default-deny ───────────────────────────────────────────────────
# Direct egress (bypassing the proxy) must be rejected by the DOCKER-USER
# REJECT rule. curl --noproxy '*' ignores HTTPS_PROXY for all hosts.
test_default_deny() {
  local out rc
  out=$(curl --noproxy '*' -sS --max-time 5 https://example.com 2>&1)
  rc=$?
  if (( rc != 0 )); then
    pass "default-deny: direct egress blocked (curl exit $rc)"
  else
    fail "default-deny" "direct request to example.com succeeded without the proxy"
  fi
}

# ── 2. Allowlisted host via proxy ────────────────────────────────────────────
# A host that should be in the default allowlist must reach the internet
# through tinyproxy.
test_allowed_via_proxy() {
  local out rc
  out=$(curl -sS --max-time 10 https://api.github.com/zen 2>&1)
  rc=$?
  if (( rc == 0 )) && [[ -n "$out" ]]; then
    pass "allowed-via-proxy: api.github.com/zen → \"${out}\""
  else
    fail "allowed-via-proxy" "curl exit $rc, response=${out:0:120}"
  fi
}

# ── 3. Denied host via proxy ─────────────────────────────────────────────────
# A host that is NOT in the allowlist must be rejected by tinyproxy's filter.
# curl exit 56 = CONNECT tunnel failed (the canonical "proxy said no" error
# for HTTPS); any other nonzero is still treated as denied.
test_denied_via_proxy() {
  local out rc
  out=$(curl -sS --max-time 10 https://example.com 2>&1)
  rc=$?
  if (( rc == 56 )); then
    pass "denied-via-proxy: tinyproxy rejected CONNECT (curl exit 56)"
  elif (( rc != 0 )); then
    pass "denied-via-proxy: blocked (curl exit $rc)"
  else
    fail "denied-via-proxy" "example.com reached the internet — the proxy filter is not enforcing"
  fi
}

# ── 4. Allowed inference routes ──────────────────────────────────────────────
# Every request here carries no credentials, as the agent has none. Injection
# can only be shown where the server enforces a key: an omlx with no key set
# answers these too, and Ollama has no auth at all.
expect_allowed() {
  local method=$1 path=$2
  shift 2
  inference_request "$method" "$path" "$@"
  if [[ "$RESP_STATUS" == 200 ]]; then
    pass "inference-allowed: $method $path → 200"
  elif backend_down; then
    skip "inference-allowed: $method $path" "backend down (proxy answered $RESP_STATUS)"
  else
    fail "inference-allowed: $method $path" "got $RESP_STATUS, body=${RESP_BODY:0:160}"
  fi
}

test_inference_allowed_routes() {
  require_inference "inference-allowed" || return
  inference_request GET /v1/models
  local models_body="$RESP_BODY"
  if backend_down; then
    skip "inference-allowed" "$BACKEND backend down (proxy answered $RESP_STATUS); proxy refusals still run below"
    return
  elif [[ "$RESP_STATUS" != 200 ]]; then
    fail "inference-allowed: GET /v1/models" "got $RESP_STATUS with no credentials; is the host's OMLX_API_KEY right? body=${RESP_BODY:0:160}"
    return
  fi
  if [[ "$BACKEND" == omlx ]]; then
    pass "inference-allowed: GET /v1/models → 200 without credentials (proxy injects the key if the server enforces one)"
  else
    pass "inference-allowed: GET /v1/models → 200 without credentials (Ollama has no auth, so nothing to inject)"
  fi

  # A guessed key must be overwritten, not forwarded. Ollama would ignore it anyway.
  if [[ "$BACKEND" == omlx ]]; then
    inference_request GET /v1/models -H "Authorization: Bearer agent-guess" -H "x-api-key: agent-guess"
    if [[ "$RESP_STATUS" == 200 ]]; then
      pass "inference-allowed: GET /v1/models → 200 with wrong client credentials (proxy overwrites them if the server enforces a key)"
    else
      fail "inference-allowed: wrong client credentials" "got $RESP_STATUS; the proxy may be forwarding them"
    fi
  fi

  [[ "$BACKEND" == ollama ]] && expect_allowed GET /api/tags

  INFERENCE_MODEL="${INFERENCE_TEST_MODEL:-$(jq -r '.data[0].id // empty' <<<"$models_body" 2>/dev/null)}"
  if [[ -z "$INFERENCE_MODEL" ]]; then
    skip "inference-allowed: model routes" "no model listed and INFERENCE_TEST_MODEL unset"
    return
  fi

  local json=(-H "Content-Type: application/json")
  local body
  body=$(chat_body "$INFERENCE_MODEL" false)
  expect_allowed POST /v1/chat/completions "${json[@]}" -d "$body"
  if [[ "$BACKEND" == omlx ]]; then
    local anthropic=("${json[@]}" -H "anthropic-version: 2023-06-01")
    body=$(jq -c 'del(.stream)' <<<"$body")
    expect_allowed POST /v1/messages "${anthropic[@]}" -d "$body"
    # Anthropic clients send ?beta=true, so query strings must keep working.
    expect_allowed POST '/v1/messages/count_tokens?beta=true' "${anthropic[@]}" \
      -d "$(jq -c 'del(.max_tokens)' <<<"$body")"
  fi
}

test_inference_streaming() {
  require_inference "inference-streaming" || return
  if [[ -z "$INFERENCE_MODEL" ]]; then
    skip "inference-streaming" "no model available (see inference-allowed)"
    return
  fi
  inference_request POST /v1/chat/completions -N -H "Content-Type: application/json" \
    -d "$(chat_body "$INFERENCE_MODEL" true)"
  if backend_down; then
    skip "inference-streaming" "backend down (proxy answered $RESP_STATUS)"
  elif [[ "$RESP_STATUS" == 200 && "$RESP_BODY" == *"data: "* && "$RESP_BODY" == *"[DONE]"* ]]; then
    pass "inference-streaming: SSE chunks through to [DONE]"
  else
    fail "inference-streaming" "got $RESP_STATUS, body=${RESP_BODY:0:160}"
  fi
}

# ── 5. Proxy refusals ────────────────────────────────────────────────────────
# Answered by the proxy itself, so these need no backend. The proxy hides the
# upstream's Server header, so that cannot tell who said 403; nginx's own error
# page can. A 403 from anything else fails rather than passing for the wrong reason.
assert_proxy_refuses() {
  local method=$1 path=$2
  shift 2
  inference_request "$method" "$path" --max-time 10 "$@"
  if [[ "$RESP_STATUS" == 403 && "$RESP_BODY" == *"<center>nginx"* ]]; then
    pass "proxy-refuses: $method $path${1:+ (with client credentials)} → 403 from nginx"
  elif [[ "$RESP_STATUS" == 403 ]]; then
    fail "proxy-refuses: $method $path" "403, but not nginx's own page; did this reach the backend? body=${RESP_BODY:0:160}"
  else
    fail "proxy-refuses: $method $path" "expected 403 from nginx, got $RESP_STATUS, body=${RESP_BODY:0:160}"
  fi
}

test_proxy_refusals() {
  require_inference "proxy-refuses" || return
  local routes=(
    "POST /v1/web/search"
    "POST /v1/web/fetch"
    "POST /v1/mcp/execute"
    "GET /admin/api/global-settings"
    "POST /v1/responses"
    "POST /v1/models/x/load"
    "GET /v1/models/status"
    "POST /api/pull"
    # Wrong method on an allowed path.
    "GET /v1/chat/completions"
    "POST /v1/models"
    "DELETE /v1/models"
    # Traversal from an allowed path.
    "POST /v1/chat/completions/../../web/search"
    "GET /v1/models/../../admin/api/global-settings"
    "POST /v1/models/x/..%2F..%2Fweb/search"
    # Percent-encoded.
    "POST /v1/web%2Fsearch"
    "POST /v1/%77eb/search"
    "POST /v1/models%2Fx%2Fload"
    "GET /%61dmin/api/global-settings"
    "GET /v1/%2e%2e/admin/api/global-settings"
  )
  case "$BACKEND" in
    omlx)
      routes+=("GET /api/tags" "GET /v1/messages")
      ;;
    ollama)
      routes+=("POST /v1/messages" "POST /v1/messages/count_tokens" "POST /api/tags")
      ;;
  esac
  local route
  for route in "${routes[@]}"; do
    assert_proxy_refuses "${route%% *}" "${route#* }"
  done
  # Refused whatever credentials the agent presents.
  assert_proxy_refuses POST /v1/web/search -H "Authorization: Bearer agent-guess" -H "x-api-key: agent-guess"
}

# ── 6. No path to the host's inference port ──────────────────────────────────
# Connects with /dev/tcp, not curl: curl 7.88 reports "Couldn't connect to
# server" for both errors, and the difference matters. The firewall's REJECT
# surfaces as "No route to host". "Connection refused" came from the host, so the
# packet got through and only the closed port stopped it.
assert_connect_rejected() {
  local label=$1 host=$2 port=$3 out rc
  out=$(timeout 5 bash -c "exec 3<>/dev/tcp/$host/$port" 2>&1)
  rc=$?
  if (( rc == 0 )); then
    fail "$label" "connected to $host:$port; the direct path is open"
  elif [[ "$out" == *"No route to host"* ]]; then
    pass "$label: $host:$port rejected by the firewall"
  elif [[ "$out" == *"refused"* ]]; then
    fail "$label" "$host:$port answered ECONNREFUSED: the packet reached the host, so no firewall rule stopped it"
  else
    fail "$label" "expected an immediate REJECT from $host:$port, got exit $rc: ${out:0:120}"
  fi
}

# The container has no HOST_IP. --add-host maps host.docker.internal to it.
container_host_ip() { getent hosts host.docker.internal 2>/dev/null | awk '{print $1; exit}'; }

test_no_direct_host_path() {
  require_inference "no-direct-host-path" || return
  local host_ip
  host_ip=$(container_host_ip)
  if [[ -z "$host_ip" ]]; then
    skip "no-direct-host-path" "host.docker.internal does not resolve, so the host IP is unknown"
    return
  elif [[ "$host_ip" == "$BRIDGE_IP" ]]; then
    skip "no-direct-host-path" "host.docker.internal maps to the proxy address $BRIDGE_IP, not the host; check from the VM"
    return
  fi
  assert_connect_rejected "no-direct-host-path" "$host_ip" "$INFERENCE_PORT"
  assert_connect_rejected "no-direct-host-path" host.docker.internal "$INFERENCE_PORT"
}

# Through tinyproxy. curl honours NO_PROXY even with -x, so a host listed there
# would go direct and measure the wrong path: hence env -u and --noproxy ''.
#
# This shows tinyproxy's hostname filter refusing, and nothing more: it answers
# "403 Filtered" for an IP or an unlisted name before opening any connection, so
# CLAUDE_AGENT_OUT is never exercised. Only a tinyproxy "403 Filtered" passes; any
# other answer fails, including "500 Unable to connect", which means the filter
# admitted the name.
#
# NOT PROVED HERE, left for live verification: the owner-match rule and its
# private ranges. That needs an allowlisted name resolving to a private address.
# tinyproxy resolves names in the VM and the allowlist is read-only in here, so
# this container cannot set one up, and the default allowlist has no
# wildcard-DNS name. Run tests/probe-tinyproxy-owner-rule.sh from the macOS host
# instead.
assert_tinyproxy_filters() {
  local url=$1 out rc connect
  out=$(env -u NO_PROXY -u no_proxy curl --noproxy '' -x "${HTTPS_PROXY:-}" -sS -i --max-time 10 "$url" 2>&1)
  rc=$?
  if (( rc == 0 )) && [[ "$out" =~ ^HTTP/[0-9.]+\ 403\ Filtered && "$out" == *"Server: tinyproxy"* ]]; then
    pass "tinyproxy-filters: $url → 403 Filtered (hostname filter only; owner-match rule not exercised)"
  else
    fail "tinyproxy-filters: $url" "expected tinyproxy's 403 Filtered, got exit $rc: ${out:0:160}"
  fi

  # curl reports only the status for a refused CONNECT.
  connect=$(env -u NO_PROXY -u no_proxy curl --noproxy '' -x "${HTTPS_PROXY:-}" -p -sS -o /dev/null \
    -w '%{http_connect}' --max-time 10 "$url" 2>/dev/null)
  rc=$?
  if (( rc != 0 )) && [[ "$connect" == 403 ]]; then
    pass "tinyproxy-filters: CONNECT for $url → 403 (tinyproxy refused; owner-match rule not exercised)"
  else
    fail "tinyproxy-filters: CONNECT for $url" "expected 403, got exit $rc, CONNECT status ${connect:-none}"
  fi
}

test_tinyproxy_refuses_host() {
  require_inference "tinyproxy-filters" || return
  local host_ip
  host_ip=$(container_host_ip)
  if [[ -n "$host_ip" ]]; then
    assert_tinyproxy_filters "http://$host_ip:$INFERENCE_PORT/v1/models"
  else
    skip "tinyproxy-filters: host IP" "host.docker.internal does not resolve"
  fi
  assert_tinyproxy_filters "http://host.docker.internal:$INFERENCE_PORT/v1/models"
}

# ── 7. The real key is not in the container ──────────────────────────────────
# The container does not know the real key, so it cannot search for it. What it
# can check: every secret-named field in the client configs holds the placeholder.
# Keep this in step with INFERENCE_PLACEHOLDER_KEY in start-agent.sh;
# tests/test_agent_sh.py asserts that they match.
INFERENCE_PLACEHOLDER_KEY="not-a-real-key-inference-proxy-injects-credentials"
OPENCODE_JSON=/root/.config/opencode/opencode.json
PI_MODELS_JSON=/root/.pi/agent/models.json

# Prints the path of each secret-named field not holding the placeholder. Never
# prints a value. `{env:NAME}` is an OpenCode reference, not a key.
nonplaceholder_secret_paths() {
  jq -r --arg ph "$INFERENCE_PLACEHOLDER_KEY" '
    paths(scalars) as $p
    | select(($p[-1] | type) == "string"
             and ($p[-1] | test("^((x-)?api[-_]?key|authorization)$"; "i")))
    | select(getpath($p) != $ph and ((getpath($p) | tostring) | startswith("{env:") | not))
    | $p | map(tostring) | join(".")' "$1"
}

# $1 config file; $2 jq path to the active entry's apiKey; $3 regex of local entries.
check_client_config() {
  local file=$1 active_path=$2 local_re=$3 paths path name
  name=$(basename "$file")
  if [[ ! -f "$file" ]]; then
    skip "no-real-key: $name" "$file does not exist"
    return
  fi
  if ! paths=$(nonplaceholder_secret_paths "$file" 2>/dev/null); then
    fail "no-real-key: $name" "not valid JSON"
    return
  fi
  # The active entry must hold the placeholder, so this cannot pass on an empty
  # file or a renamed field.
  if [[ "$(jq -r "$active_path // empty" "$file")" == "$INFERENCE_PLACEHOLDER_KEY" ]]; then
    pass "no-real-key: $name $active_path holds the placeholder"
  else
    fail "no-real-key: $name" "$active_path does not hold the placeholder"
  fi
  local leaks=() others=()
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    if [[ "$path" =~ $local_re ]]; then
      leaks+=("$path")
    else
      others+=("$path")
    fi
  done <<<"$paths"
  if (( ${#leaks[@]} > 0 )); then
    fail "no-real-key: $name" "local provider fields without the placeholder: ${leaks[*]}"
  else
    pass "no-real-key: $name local provider entries hold only the placeholder"
  fi
  # A user's own cloud key is legitimate here, and the container cannot compare
  # these with the real key. The injectors scrub copies of it on the host.
  if (( ${#others[@]} > 0 )); then
    skip "no-real-key: $name other entries" "cannot verify from here, confirm these are not the omlx key: ${others[*]}"
  fi
}

test_real_key_absent() {
  # By name only: a leak must not be printed into the test log.
  if [[ -n "${OMLX_API_KEY:-}" ]]; then
    fail "no-real-key: env" "OMLX_API_KEY is set in this shell; recreate with start-agent.sh --reset-container"
  else
    pass "no-real-key: env has no OMLX_API_KEY"
  fi
  # A shell can have had it unset while the container's create-time env keeps it.
  if tr '\0' '\n' </proc/1/environ 2>/dev/null | grep -q '^OMLX_API_KEY=.'; then
    fail "no-real-key: create-time env" "PID 1 holds OMLX_API_KEY; recreate with start-agent.sh --reset-container"
  else
    pass "no-real-key: create-time env (PID 1) has no OMLX_API_KEY"
  fi
  local backend="${BACKEND:-omlx}"
  check_client_config "$OPENCODE_JSON" ".provider.$backend.options.apiKey" '^provider\.(omlx|ollama)\.'
  check_client_config "$PI_MODELS_JSON" ".providers.local.apiKey" '^providers\.local\.'
}

# ── 8. omlx vision ───────────────────────────────────────────────────────────
# Catches regressions on an omlx upgrade. Remote image URLs must stay refused,
# since the proxy does not inspect bodies and such a URL would be server-side
# Internet access. Message checked as well as status: a 400 for any other reason
# would prove nothing.
# 16x16 solid red PNG.
TEST_IMAGE_B64="iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFklEQVR42mP4z8BAEmIY1TCqYfhqAACQ+f8B8u7oVwAAAABJRU5ErkJggg=="

vision_chat_body() {
  jq -nc --arg m "$1" --arg url "$2" '{model: $m, max_tokens: 32, messages: [{role: "user", content: [
    {type: "text", text: "What colour is this image?"},
    {type: "image_url", image_url: {url: $url}}]}]}'
}

assert_remote_image_rejected() {
  local label=$1 path=$2 body=$3
  inference_request POST "$path" -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" -d "$body"
  if backend_down; then
    skip "vision: $label" "backend down (proxy answered $RESP_STATUS)"
  elif [[ "$RESP_STATUS" == 400 && "${RESP_BODY,,}" == *"remote url"* ]]; then
    pass "vision: $label rejected with 400"
  else
    fail "vision: $label" "expected 400 naming remote URLs, got $RESP_STATUS, body=${RESP_BODY:0:160}"
  fi
}

test_omlx_vision() {
  require_inference "vision" || return
  if [[ "$BACKEND" != omlx ]]; then
    skip "vision" "omlx only; backend is $BACKEND"
    return
  fi
  local model="${INFERENCE_VISION_MODEL:-}"
  if [[ -z "$model" ]]; then
    skip "vision" "INFERENCE_VISION_MODEL unset; name an omlx vision model to run these"
    return
  fi
  inference_request POST /v1/chat/completions -H "Content-Type: application/json" \
    -d "$(vision_chat_body "$model" "data:image/png;base64,$TEST_IMAGE_B64")"
  if backend_down; then
    skip "vision: base64 image" "backend down (proxy answered $RESP_STATUS)"
  elif [[ "$RESP_STATUS" == 200 && -n "$(jq -r '.choices[0].message.content // empty' <<<"$RESP_BODY" 2>/dev/null)" ]]; then
    pass "vision: base64 image answered by $model"
  else
    fail "vision: base64 image" "got $RESP_STATUS, body=${RESP_BODY:0:160}"
  fi

  assert_remote_image_rejected "http:// image URL" /v1/chat/completions \
    "$(vision_chat_body "$model" "http://example.com/x.png")"
  assert_remote_image_rejected "file:// image URL" /v1/chat/completions \
    "$(vision_chat_body "$model" "file:///etc/passwd")"
  assert_remote_image_rejected "Anthropic url image source" /v1/messages \
    "$(jq -nc --arg m "$model" '{model: $m, max_tokens: 32, messages: [{role: "user", content: [
      {type: "image", source: {type: "url", url: "http://example.com/x.png"}},
      {type: "text", text: "What colour is this image?"}]}]}')"
}

# ── 9. Runtime env wiring ────────────────────────────────────────────────────
# The proxy vars and the active backend's URL must be present from process
# birth, and that URL must be the in-VM proxy, not the host.
test_env_wiring() {
  local missing=()
  for var in HTTP_PROXY HTTPS_PROXY NO_PROXY; do
    if [[ -z "${!var:-}" ]]; then
      missing+=("$var")
    fi
  done
  [[ -n "$INFERENCE_URL" ]] || missing+=("OMLX_HOST or OLLAMA_HOST")
  if (( ${#missing[@]} > 0 )); then
    fail "env-wiring" "missing: ${missing[*]}"
  elif [[ "$INFERENCE_URL" != "http://$BRIDGE_IP:$INFERENCE_PORT" ]]; then
    fail "env-wiring" "$INFERENCE_URL is not the proxy at $BRIDGE_IP; container predates it? start-agent.sh --reset-container"
  else
    pass "env-wiring: HTTP_PROXY, HTTPS_PROXY, NO_PROXY set; $BACKEND at proxy $INFERENCE_URL"
  fi
}

# ── 10. Inter-container port isolation ────────────────────────────────────────
# The iptables CLAUDE_AGENT chain allows inter-container traffic within
# claude-agent-net only on port 8080 (SearXNG MCP path). All other ports must
# be rejected. Tests both sides: 8080 reachable, 9000 blocked.
# Skip if searxng is not running (--disable-search).
test_searxng_port_isolation() {
  if ! getent hosts searxng >/dev/null 2>&1; then
    skip "inter-container-port-isolation" "searxng not in DNS (--disable-search?)"
    return
  fi

  # Port 8080: must be reachable. curl 0 (ok), 22 (HTTP error ≥400), or 52
  # (empty reply) all confirm the TCP connection was allowed through.
  local rc
  curl --noproxy searxng -sS --max-time 5 http://searxng:8080/ >/dev/null 2>&1
  rc=$?
  if (( rc == 0 || rc == 22 || rc == 52 )); then
    pass "inter-container-port-isolation: searxng:8080 reachable (curl exit $rc)"
  else
    fail "inter-container-port-isolation" "searxng:8080 blocked (curl exit $rc); iptables AGENT_NET:8080 RETURN rule may be missing"
  fi

  # Port 9000: must be blocked. With --reject-with icmp-admin-prohibited, curl
  # sees the ICMP response immediately and exits non-zero (typically exit 7).
  curl --noproxy searxng -sS --max-time 3 http://searxng:9000/ >/dev/null 2>&1
  rc=$?
  if (( rc != 0 )); then
    pass "inter-container-port-isolation: searxng:9000 blocked (curl exit $rc)"
  else
    fail "inter-container-port-isolation" "searxng:9000 reachable — iptables CLAUDE_AGENT REJECT not firing on inter-container traffic"
  fi
}

# ── 11. External DNS blackholed ───────────────────────────────────────────────
# The agent container resolves through 192.0.2.1 (never routed), so external
# names must not resolve. It should fail fast (REJECT), not after a resolver
# timeout; a slow failure means queries are not leaving from the container's
# namespace.
#
# The kernel rate-limits the ICMP errors REJECT sends, per destination, and the
# rejected connects earlier in this script drain that allowance. A lookup whose
# error is suppressed waits out Docker's 4s resolver timeout, so one slow failure
# proves nothing. Pause first, and pass on any fast failure in three attempts.
test_external_dns_blackholed() {
  local attempt start end ms
  for attempt in 1 2 3; do
    sleep 4
    start=$(date +%s%N)
    if getent hosts example.com >/dev/null 2>&1; then
      fail "external-dns-blackholed" "example.com resolved — DNS is a channel past the allowlist"
      return
    fi
    end=$(date +%s%N)
    ms=$(( (end - start) / 1000000 ))
    if (( ms < 2000 )); then
      pass "external-dns-blackholed: example.com did not resolve (${ms} ms, attempt $attempt)"
      return
    fi
  done
  fail "external-dns-blackholed" "blocked, but took ${ms} ms on all 3 attempts (resolver timeout, not a REJECT)"
}

# ── 12. Container-name DNS ────────────────────────────────────────────────────
# Docker's embedded DNS must still resolve searxng on claude-agent-net.
# Skip when search is disabled (the script strips the searxng MCP block).
test_container_dns() {
  if ! grep -q '"searxng"' /root/.config/opencode/opencode.json 2>/dev/null; then
    skip "container-dns" "search disabled (no searxng MCP in opencode.json)"
    return
  fi
  if getent hosts searxng >/dev/null 2>&1; then
    pass "container-dns: searxng resolves"
  else
    fail "container-dns" "searxng does not resolve; embedded DNS is broken"
  fi
}

# ── 13. VM-local services closed ──────────────────────────────────────────────
# Traffic to the VM's own addresses goes through INPUT (CLAUDE_AGENT_IN): the
# proxy port must connect, the VM's sshd must not.
test_vm_local_services_closed() {
  local proxy="${HTTPS_PROXY#http://}"
  proxy="${proxy%/}"
  local bridge_ip="${proxy%:*}" proxy_port="${proxy##*:}"
  local gw_hex gw
  gw_hex=$(awk '$2 == "00000000" { print $3; exit }' /proc/net/route)
  if [[ -z "$bridge_ip" || -z "$gw_hex" ]]; then
    fail "vm-local-services-closed" "could not determine proxy address or default gateway"
    return
  fi
  gw=$(printf '%d.%d.%d.%d' "0x${gw_hex:6:2}" "0x${gw_hex:4:2}" "0x${gw_hex:2:2}" "0x${gw_hex:0:2}")

  if tcp_open "$bridge_ip" "$proxy_port"; then
    pass "vm-local-services-closed: proxy $bridge_ip:$proxy_port connects"
  else
    fail "vm-local-services-closed" "proxy $bridge_ip:$proxy_port refused; CLAUDE_AGENT_IN RETURN rule missing?"
  fi

  local addr
  for addr in $(printf '%s\n' "$gw" "$bridge_ip" | sort -u); do
    if tcp_open "$addr" 22; then
      fail "vm-local-services-closed" "VM sshd reachable at $addr:22; CLAUDE_AGENT_IN not applied"
    else
      pass "vm-local-services-closed: $addr:22 refused"
    fi
  done
}

# ── run ──────────────────────────────────────────────────────────────────────
test_default_deny
test_allowed_via_proxy
test_denied_via_proxy
test_inference_allowed_routes
test_inference_streaming
test_proxy_refusals
test_no_direct_host_path
test_tinyproxy_refuses_host
test_real_key_absent
test_omlx_vision
test_env_wiring
test_searxng_port_isolation
test_external_dns_blackholed
test_container_dns
test_vm_local_services_closed

echo
printf '==> %s%d passed%s, %s%d failed%s, %s%d skipped%s\n' \
  "$C_OK" "$PASS" "$C_RST" \
  "$C_ERR" "$FAIL" "$C_RST" \
  "$C_WARN" "$SKIP" "$C_RST"

(( FAIL == 0 ))
