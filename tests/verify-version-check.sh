#!/usr/bin/env bash
# verify-version-check.sh — live check of check_versions.py against the real
# npm registry and Docker Hub (plans/safe-tool-version-updates.md, Phase 3;
# ADR-055). Run on the macOS host, which has direct egress:
#
#     tests/verify-version-check.sh
#
# Checks, each PASS/FAIL (INFO lines are recorded, not judged):
#   - Docker Hub's tags API answers for searxng/searxng:latest with a
#     last_updated date and a digest (plan Unknown 4); the raw response is
#     saved next to the log
#   - INFO: whether the tags API's digest for the pinned version's tag equals
#     the RepoDigests digest pinned in SEARXNG_IMAGE (the helper compares
#     dates only, so either answer is fine)
#   - the helper, run as the launchers configure it, fetches all four
#     sources into a scratch state file and exits 0; its real output is logged
#   - a second run the same day does not fetch again
#   - with every in-use value forced old and --lag-days 0, the cached report
#     prints one line per component, each naming its update command
#   - the cached report (what runs before every attach) takes under a second
# It never touches the launchers' own state files.
# Log: .verify/verify-version-check.log

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
mkdir -p .verify
LOG="$REPO/.verify/verify-version-check.log"
RAW="$REPO/.verify/version-check-dockerhub.json"
: > "$LOG"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
STATE="$WORK/version-check.json"

PASS=0; FAIL=0
say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
pass() { say "PASS  $1"; PASS=$((PASS+1)); }
fail() { say "FAIL  $1${2:+ — $2}"; FAIL=$((FAIL+1)); }
info() { say "INFO  $1"; }

for tool in python3 curl; do
  command -v "$tool" >/dev/null || { say "error: $tool not found on PATH"; exit 1; }
done
say "verify-version-check $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(uname -s), $(python3 --version 2>&1)"

# ── what the launchers pass ──────────────────────────────────────────────────
PIN_LINE=$(grep -m1 '^SEARXNG_IMAGE=' start-agent.sh)
PIN_DIGEST=$(sed -n 's/^SEARXNG_IMAGE=".*@\(sha256:[0-9a-f]\{64\}\)"$/\1/p' <<<"$PIN_LINE")
PIN_ON=$(sed -n 's/^# Pinned by digest (.*known good on \([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\)).*/\1/p' start-agent.sh | head -n 1)
PIN_VERSION=$(sed -n 's/^# Pinned by digest (SearXNG \([^,]*\),.*/\1/p' start-agent.sh | head -n 1)
SR_PIN=$(sed -n 's/.*@anthropic-ai\/sandbox-runtime@\([0-9][0-9.]*\).*/\1/p' start-claude.sh | head -n 1)
info "SearXNG pin: ${PIN_VERSION:-?} ${PIN_DIGEST:-?}, known good on ${PIN_ON:-?}"
info "sandbox-runtime pin: ${SR_PIN:-?}"
[[ -n "$PIN_DIGEST" && -n "$PIN_ON" && -n "$SR_PIN" ]] \
  && pass "pins and the SearXNG pin date parse from the launchers" \
  || fail "pins and the SearXNG pin date parse from the launchers"

# ── Docker Hub tags API (Unknown 4) ──────────────────────────────────────────
hub_tag() {  # $1 tag, $2 output file; prints the HTTP status
  curl -s -o "$2" -w '%{http_code}' --max-time 15 \
    "https://hub.docker.com/v2/repositories/searxng/searxng/tags/$1" || true
}
fields() {  # $1 file: one-line summary of the fields the helper reads
  python3 - "$1" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
imgs = [(i.get("architecture"), (i.get("digest") or "")[:19]) for i in d.get("images") or []]
print("name={} last_updated={} tag_last_pushed={} digest={} images={}".format(
    d.get("name"), d.get("last_updated"), d.get("tag_last_pushed"), d.get("digest"), imgs))
PYEOF
}
code=$(hub_tag latest "$RAW")
if [[ "$code" == 200 ]] && summary=$(fields "$RAW" 2>&1); then
  info "tags/latest: $summary"
  if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("last_updated") and str(d.get("digest","")).startswith("sha256:") else 1)' "$RAW"; then
    pass "Docker Hub tags/latest has last_updated and digest (raw: $RAW)"
  else
    fail "Docker Hub tags/latest has last_updated and digest" "see $RAW"
  fi
else
  fail "Docker Hub tags API answers for searxng/searxng:latest" "HTTP $code"
fi

if [[ -n "$PIN_VERSION" ]]; then
  pinned_json="$WORK/pinned-tag.json"
  code=$(hub_tag "$PIN_VERSION" "$pinned_json")
  if [[ "$code" == 200 ]]; then
    info "tags/$PIN_VERSION: $(fields "$pinned_json" 2>&1)"
    match=$(python3 - "$pinned_json" "$PIN_DIGEST" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1])); pin = sys.argv[2]
if d.get("digest") == pin:
    print("yes: the tags API digest is the RepoDigests (manifest-list) digest")
elif any(i.get("digest") == pin for i in d.get("images") or []):
    print("no: the pin matches a per-platform image digest, not the tag digest")
else:
    print("no: the pin matches neither the tag digest nor an image digest")
PYEOF
)
    info "Unknown 4, digest equality for the pinned tag: $match"
  else
    info "tags/$PIN_VERSION: HTTP $code (no tag for the pinned version; digest equality not checked)"
  fi
else
  info "no SearXNG version in the pin comment; digest equality not checked"
fi

# ── the helper, live ─────────────────────────────────────────────────────────
real_specs=("searxng=$PIN_ON" "sandbox-runtime=$SR_PIN" opencode pi)
start=$(python3 -c 'import time; print(time.time())')
out=$(python3 check_versions.py --state "$STATE" "${real_specs[@]}" 2>&1); rc=$?
took=$(python3 -c "import time; print('%.1f' % (time.time() - $start))")
info "helper fetch + report with the real pins, default 30-day lag (${took}s):"
if [[ -n "$out" ]]; then printf '%s\n' "$out" | sed 's/^/        /' | tee -a "$LOG"; else say "        (no warnings)"; fi
summary=$(python3 - "$STATE" <<'PYEOF' 2>&1
import json, sys
s = json.load(open(sys.argv[1]))["sources"]
for key in sorted(s):
    e = s[key]
    print("{}: latest={} updated={} digest={} times={}".format(
        key, e.get("latest"), e.get("updated"), (e.get("digest") or "")[:19], len(e.get("times") or {})))
want = {"npm:@opencode/cli", "npm:@earendil-works/pi-coding-agent",
        "npm:@anthropic-ai/sandbox-runtime", "dockerhub:searxng/searxng:latest"}
bad = [k for k in want if not (s.get(k, {}).get("latest") or s.get(k, {}).get("updated"))]
sys.exit(1 if bad else 0)
PYEOF
); src_rc=$?
printf '%s\n' "$summary" | sed 's/^/        /' | tee -a "$LOG"
[[ $rc -eq 0 && $src_rc -eq 0 ]] \
  && pass "helper fetched all four sources (npm x3, Docker Hub) and exited 0" \
  || fail "helper fetched all four sources (npm x3, Docker Hub) and exited 0" "rc=$rc; see the source lines above"

before=$(python3 -c 'import json,sys; print(sorted((k, v.get("checked")) for k, v in json.load(open(sys.argv[1]))["sources"].items()))' "$STATE" 2>&1)
python3 check_versions.py --state "$STATE" "${real_specs[@]}" >/dev/null 2>&1
after=$(python3 -c 'import json,sys; print(sorted((k, v.get("checked")) for k, v in json.load(open(sys.argv[1]))["sources"].items()))' "$STATE" 2>&1)
[[ "$before" == "$after" ]] && pass "a second run the same day does not fetch again" \
  || fail "a second run the same day does not fetch again"

old=$(python3 - "$STATE" <<'PYEOF' 2>/dev/null
import json, re, sys
s = json.load(open(sys.argv[1]))["sources"]
def oldest(key):
    times = s.get(key, {}).get("times") or {}
    rel = sorted(times, key=lambda v: times[v])
    return rel[0] if rel else ""
print("opencode=" + oldest("npm:@opencode/cli"))
print("pi=" + oldest("npm:@earendil-works/pi-coding-agent"))
print("sandbox-runtime=" + oldest("npm:@anthropic-ai/sandbox-runtime"))
PYEOF
)
forced=(searxng=2000-01-01)
while IFS= read -r spec; do [[ -n "$spec" ]] && forced+=("$spec"); done <<<"$old"
info "forced in-use values: ${forced[*]}"
forced_out=$(python3 check_versions.py --cached --state "$STATE" --lag-days 0 "${forced[@]}" 2>&1)
printf '%s\n' "$forced_out" | sed 's/^/        /' | tee -a "$LOG"
ok=true
for want in "SearXNG.*scripts/bump-searxng.sh" "OpenCode.*start-agent.sh --refresh-tools" \
            "Pi .*start-agent.sh --refresh-tools" "sandbox-runtime.*start-claude.sh --rebuild"; do
  grep -q "$want" <<<"$forced_out" || ok=false
done
[[ $(grep -c '^warning: ' <<<"$forced_out") -eq 4 ]] || ok=false
$ok && pass "forced-old report: one line per component, each naming its update command" \
  || fail "forced-old report: one line per component, each naming its update command"

start=$(python3 -c 'import time; print(time.time())')
python3 check_versions.py --cached --state "$STATE" "${real_specs[@]}" >/dev/null 2>&1
took=$(python3 -c "import time; print('%.3f' % (time.time() - $start))")
python3 -c "import sys; sys.exit(0 if float('$took') < 1.0 else 1)" \
  && pass "cached report takes ${took}s (< 1s)" || fail "cached report takes ${took}s (< 1s)"

say ""
say "Summary: $PASS passed, $FAIL failed. Log: $LOG"
[[ $FAIL -eq 0 ]]
