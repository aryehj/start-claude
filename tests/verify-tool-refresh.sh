#!/usr/bin/env bash
# verify-tool-refresh.sh — acceptance checks for a SearXNG bump or an
# OpenCode/Pi refresh (plans/safe-tool-version-updates.md, ADR-054).
#
# Run on the macOS host while the claude-agent container is running (e.g. in a
# second terminal after `start-agent.sh --backend=omlx`):
#     tests/verify-tool-refresh.sh
# The end-to-end check needs omlx serving gemma-4-e4b-it-6bit (the only model
# live tests use; larger ones strain the host's RAM).
#
# In the agent container (via `docker exec`):
#   1. SearXNG answers JSON with results from at least two engines, and no
#      engine fails for a reason other than upstream throttling (an engine that
#      needs a host missing from the allowlist shows up here).
#   2. OpenCode loads the injected opencode.json (`opencode debug config`) and
#      the searxng MCP server connects (`opencode mcp list`).
#   3. `opencode run` on omlx/gemma-4-e4b-it-6bit calls the searxng tool, the
#      tool returns results, and the answer is not empty. `pi --version` runs.
# On the host (DOCKER_CONTEXT=colima-claude-agent, the client start-agent.sh uses):
#   4. After --refresh-tools: claude-agent:previous and :latest share every
#      layer above the npm install and differ from it onward.
#   5. Whether the build cache survives `docker image rm`, i.e. whether
#      --rebuild alone reuses the npm layer: builds a two-line probe image
#      twice with an image rm between. INFO only.
#   6. The sandbox's .sandbox_config/tool-versions matches the image.
#
# Also runs inside the agent container itself: checks 1-3 run directly and the
# host checks are skipped. Full output: .verify/verify-tool-refresh-<time>.log

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$REPO/tests/$(basename "$0")"
cd "$REPO" || exit 1
mkdir -p .verify
LOG="$REPO/.verify/verify-tool-refresh-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee "$LOG") 2>&1

PROFILE=claude-agent
CONTAINER=claude-agent
MODEL="${VERIFY_MODEL:-gemma-4-e4b-it-6bit}"
PASS=0; FAIL=0; SKIP=0
pass() { echo "PASS  $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL  $1${2:+ — $2}"; FAIL=$((FAIL+1)); }
skip() { echo "SKIP  $1${2:+ — $2}"; SKIP=$((SKIP+1)); }
# Count result lines produced elsewhere (a container, a python helper); tally also prints them.
tally() { printf '%s\n' "$1"; count "$1"; }
count() {
  PASS=$((PASS + $(grep -c '^PASS ' <<<"$1")))
  FAIL=$((FAIL + $(grep -c '^FAIL ' <<<"$1")))
  SKIP=$((SKIP + $(grep -c '^SKIP ' <<<"$1")))
}
summary() {
  echo
  echo "==> $PASS passed, $FAIL failed, $SKIP skipped (log: $LOG)"
  [[ $FAIL -eq 0 ]]
  exit $?
}
die() { echo "error: $1"; echo "    $2"; FAIL=$((FAIL+1)); summary; }

echo "==> tool refresh acceptance checks ($(date '+%Y-%m-%d %H:%M:%S'))"
echo

# The in-container checks live after the `exit` at the end of this file. They
# are passed to `bash -c` as an argument and run with stdin closed, so nothing
# they start (opencode, curl) can read the rest of the script, and nothing waits
# for an EOF on a piped stdin. Host runs that piped the checks into
# `docker exec -i` stalled with no output past "mode: host"; a stdin EOF lost
# on the Colima socket forward is the likely cause.
AGENT_SCRIPT="$(printf 'MODEL=%q\n' "$MODEL"; sed -n '/^#__AGENT_CHECKS__$/,$p' "$SELF")"

if command -v colima >/dev/null 2>&1; then
  MODE=host
  colima list -p "$PROFILE" --json 2>/dev/null | grep -q '"Running"' \
    || die "Colima VM '$PROFILE' is not running" "Start it with: start-agent.sh --backend=omlx"
  if docker context inspect "colima-$PROFILE" >/dev/null 2>&1; then
    export DOCKER_CONTEXT="colima-$PROFILE"
  fi
  [[ "$(docker container inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == true ]] \
    || die "container '$CONTAINER' is not running" "Start it in another terminal with: start-agent.sh --backend=omlx"
  in_agent() { docker exec "$CONTAINER" bash -c "$AGENT_SCRIPT" </dev/null; }
elif [[ -f /etc/claude-agent/allowlist.txt ]]; then
  MODE=container
  in_agent() { bash -c "$AGENT_SCRIPT" </dev/null; }
else
  die "neither colima nor the claude-agent container found" "Run this on the macOS host (or inside the claude-agent container)."
fi
echo "INFO  mode: $MODE"

# ── 1-3: inside the agent container ──────────────────────────────────────────
# Stream the output as it comes (the end-to-end run can take minutes), then count it.
agent_log=$(mktemp)
in_agent 2>&1 | tee "$agent_log"
AGENT_OUT=$(cat "$agent_log"); rm -f "$agent_log"
count "$AGENT_OUT"
grep -qE '^(PASS|FAIL|SKIP) ' <<<"$AGENT_OUT" || fail "in-container checks ran" "no result lines; see the output above"

# ── 4-6: host-side image checks ──────────────────────────────────────────────
echo
if [[ "$MODE" != host ]]; then
  skip "image layer, build cache and tool-versions checks" "need the macOS host"
  summary
fi

latest_id=$(docker image inspect -f '{{.Id}}' claude-agent:latest 2>/dev/null)
previous_id=$(docker image inspect -f '{{.Id}}' claude-agent:previous 2>/dev/null)
echo "INFO  claude-agent:latest   ${latest_id:-missing}"
echo "INFO  claude-agent:previous ${previous_id:-missing}"

# 4. The refresh rebuilt only from the npm layer onward.
if [[ -z "$previous_id" ]]; then
  skip "refresh kept the layers above npm" "no claude-agent:previous yet (run start-agent.sh --refresh-tools)"
elif [[ "$previous_id" == "$latest_id" ]]; then
  skip "refresh kept the layers above npm" "previous and latest are the same image (restored, or not refreshed since)"
else
  # Layers above the npm RUN: the base image's, plus one per RUN/COPY/ADD before
  # it. Both read from the Dockerfile, so the count follows its layout (the npm
  # RUN is the last layer; tests/test_dockerfile.py enforces that).
  above=$(awk '/^(RUN|COPY|ADD) /{n++} /opencode-ai@latest/{print n-1; exit}' dockerfiles/claude-agent.Dockerfile)
  base_image=$(awk '/^FROM /{print $2; exit}' dockerfiles/claude-agent.Dockerfile)
  base=$(docker image inspect -f '{{len .RootFS.Layers}}' "$base_image" 2>/dev/null || echo 1)
  tally "$(python3 - "$((above + base))" \
    "$(docker image inspect -f '{{json .RootFS.Layers}}' claude-agent:previous)" \
    "$(docker image inspect -f '{{json .RootFS.Layers}}' claude-agent:latest)" <<'PYEOF'
import json, sys
keep, a, b = int(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[3])
shared = 0
while shared < min(len(a), len(b)) and a[shared] == b[shared]:
    shared += 1
print(f"INFO  {shared} shared layers; {keep} expected above the npm install")
print(f"PASS  refresh kept the {keep} layers above npm" if shared >= keep else
      f"FAIL  refresh kept the layers above npm — only {shared} of {keep} shared (OS/Node/Claude Code were rebuilt)")
print("PASS  refresh rebuilt the npm layer" if shared <= keep else
      f"FAIL  refresh rebuilt the npm layer — {shared} layers shared, so the npm layer was reused")
PYEOF
)"
fi

# 5. Does the build cache survive `docker image rm`?
probe_tag=claude-agent-cache-probe:latest
probe_build() {
  printf 'FROM claude-agent:latest\nRUN date +%%s%%N > /cache-probe\n' \
    | docker build -q -t "$probe_tag" - >/dev/null 2>&1 \
    && docker run --rm --network none --entrypoint cat "$probe_tag" /cache-probe 2>/dev/null
}
first=$(probe_build); docker image rm "$probe_tag" >/dev/null 2>&1
second=$(probe_build); docker image rm "$probe_tag" >/dev/null 2>&1
if [[ -z "$first" || -z "$second" ]]; then
  skip "build cache probe" "the probe image did not build"
elif [[ "$first" == "$second" ]]; then
  echo "INFO  build cache SURVIVES docker image rm: --rebuild alone reuses the cached npm layer"
else
  echo "INFO  build cache does NOT survive docker image rm: --rebuild reruns every layer, npm included"
fi
echo "INFO  docker client $(docker version -f '{{.Client.Version}}' 2>/dev/null), server $(docker version -f '{{.Server.Version}}' 2>/dev/null); buildx: $(docker buildx version 2>/dev/null | head -n 1 || echo none)${DOCKER_BUILDKIT:+; DOCKER_BUILDKIT=$DOCKER_BUILDKIT}"

# 6. The tool-version record matches the image.
sandbox=""; dir="$REPO"
while [[ "$dir" != / ]]; do
  if [[ -d "$dir/.sandbox_config" ]]; then sandbox="$dir"; break; fi
  dir=$(dirname "$dir")
done
record="$sandbox/.sandbox_config/tool-versions"
if [[ -z "$sandbox" ]]; then
  skip "tool-versions record matches the image" "this repo is not inside a sandbox"
elif [[ ! -f "$record" ]]; then
  fail "tool-versions record matches the image" "$record missing (start-agent.sh writes it after a build or on a fresh container)"
elif grep -qxF "image_id=$latest_id" "$record"; then
  pass "tool-versions record matches the image ($(grep -E '^(opencode|pi)=' "$record" | tr '\n' ' '))"
else
  fail "tool-versions record matches the image" "$record describes $(sed -n 's/^image_id=//p' "$record")"
fi

summary
exit

#__AGENT_CHECKS__
# Runs inside the claude-agent container. MODEL is set by the line above it.
work=$(mktemp -d)
cd "$work" || exit 1

# 1. SearXNG JSON through tinyproxy.
for q in debian+linux+distribution python+programming+language; do
  curl --noproxy searxng -s --max-time 30 "http://searxng:8080/search?q=$q&format=json" > "sx-$q.json" 2>/dev/null
done
python3 - sx-*.json <<'PYEOF'
import json, sys
throttle = ("captcha", "too many requests", "suspended")
engines, odd, total, bad = set(), [], 0, []
for path in sys.argv[1:]:
    try:
        d = json.load(open(path))
    except Exception as e:
        bad.append(f"{path}: not JSON ({e})")
        continue
    r = d.get("results", [])
    total += len(r)
    found = sorted({e for x in r for e in (x.get("engines") or [x.get("engine")]) if e})
    engines.update(found)
    un = d.get("unresponsive_engines", [])
    print(f"INFO  searxng {path[3:-5]}: {len(r)} results from {found or 'no engine'}; unresponsive: {un or 'none'}")
    for name, reason in un:
        if any(t in reason.lower() for t in throttle):
            print(f"INFO  {name}: upstream throttling ({reason})")
        else:
            odd.append(f"{name} ({reason})")
if bad:
    print(f"FAIL  searxng answers JSON — {'; '.join(bad)}")
elif len(engines) >= 2:
    print(f"PASS  searxng: {total} results from {len(engines)} engines ({', '.join(sorted(engines))})")
else:
    print(f"FAIL  searxng: results from at least two engines — got {sorted(engines) or 'none'}")
if odd:
    print(f"FAIL  searxng: no engine fails for a non-throttling reason — {', '.join(sorted(set(odd)))}")
elif not bad:
    print("PASS  searxng: no engine fails for a non-throttling reason")
PYEOF
ver=$(curl --noproxy searxng -s --max-time 10 http://searxng:8080/config 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null)
echo "INFO  searxng version: ${ver:-unknown}"

# 2. OpenCode config and MCP.
strip() { sed 's/\x1b\[[0-9;]*m//g'; }
if out=$(timeout 90 opencode debug config 2>&1 >/dev/null); then
  echo "PASS  opencode debug config loads the injected opencode.json"
else
  echo "FAIL  opencode debug config loads the injected opencode.json — $(printf '%s' "$out" | strip | head -n 3 | tr '\n' ' ')"
fi
mcp=$(timeout 90 opencode mcp list 2>&1 | strip)
if grep -q 'searxng.*connected' <<<"$mcp"; then
  echo "PASS  opencode mcp list: searxng connected"
else
  echo "FAIL  opencode mcp list: searxng connected — $(grep -m1 searxng <<<"$mcp" || echo 'searxng not listed')"
fi

# 3. CLI versions, then end to end on omlx.
echo "INFO  opencode $(opencode --version 2>/dev/null | head -n 1), pi $(timeout 30 pi --version 2>/dev/null | head -n 1)"
if timeout 30 pi --version >/dev/null 2>&1; then echo "PASS  pi --version runs"; else echo "FAIL  pi --version runs"; fi
if ! python3 - "$MODEL" <<'PYEOF'
import json, sys
try:
    d = json.load(open("/root/.config/opencode/opencode.json"))
except Exception:
    sys.exit(1)
sys.exit(0 if sys.argv[1] in d.get("provider", {}).get("omlx", {}).get("models", {}) else 1)
PYEOF
then
  echo "SKIP  opencode run end to end — opencode.json has no omlx provider with $MODEL (start with --backend=omlx while omlx serves it)"
else
  echo "INFO  opencode run on omlx/$MODEL (may take minutes while the model loads)"
  timeout 600 opencode run --format json -m "omlx/$MODEL" --dir "$work" \
    "Use the searxng websearch tool to search for 'Debian Linux distribution'. Then reply with one sentence about what you found." \
    > run.jsonl 2> run.err
  rc=$?
  python3 - run.jsonl "$rc" <<'PYEOF'
import json, sys
calls, texts = [], []
for line in open(sys.argv[1]):
    try:
        e = json.loads(line)
    except Exception:
        continue
    p = e.get("part") or {}
    if e.get("type") == "tool_use" and str(p.get("tool", "")).startswith("searxng"):
        calls.append(p.get("state") or {})
    if e.get("type") == "text" and (p.get("text") or "").strip():
        texts.append(p["text"].strip())
print(f"INFO  opencode run exit {sys.argv[2]}; {len(calls)} searxng tool call(s)")
done = [c for c in calls if c.get("status") == "completed"]
print("PASS  opencode run calls the searxng tool" if done else
      "FAIL  opencode run calls the searxng tool — no completed searxng_* tool_use event")

def n_results(c):
    try:
        out = json.loads(c.get("output") or "")
    except Exception:
        return 1 if len(c.get("output") or "") > 20 else 0
    if isinstance(out, dict):
        out = out.get("result", out.get("results", []))
    return len(out) if isinstance(out, list) else 0

if done:
    hits = max(n_results(c) for c in done)
    print(f"PASS  the searxng tool returned results ({hits})" if hits else
          "FAIL  the searxng tool returned results — it answered with an empty list")
print(f"PASS  opencode run gives a non-empty answer: {texts[-1][:120]!r}" if texts else
      "FAIL  opencode run gives a non-empty answer — no text event")
PYEOF
  if [[ -s run.err ]]; then strip < run.err | head -n 5 | sed 's/^/INFO  opencode stderr: /'; fi
fi
cd / && rm -rf "$work"
