#!/usr/bin/env bash
# bump-searxng.sh — move start-agent.sh's SearXNG pin to the current upstream
# image (plans/safe-tool-version-updates.md; ADR-054).
#
# Run on the macOS host with the claude-agent Colima VM up:
#     scripts/bump-searxng.sh
# It pulls docker.io/searxng/searxng:latest inside the VM, reads the digest
# from RepoDigests, and rewrites SEARXNG_IMAGE and its "known good on" comment
# in start-agent.sh. It does not commit. Next:
#   1. start-agent.sh (any launch replaces a searxng container on another pin)
#   2. tests/verify-tool-refresh.sh
#   3. commit start-agent.sh if it passes; otherwise `git checkout start-agent.sh`
#      and launch again to go back to the old image.
#
# Options (mainly for tests):
#   --digest sha256:HEX   use this digest instead of pulling in the VM
#   --version VERSION     SearXNG version to note in the comment
# Env: START_AGENT overrides the file to rewrite.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
START_AGENT="${START_AGENT:-$REPO/start-agent.sh}"
PROFILE=claude-agent
UPSTREAM=docker.io/searxng/searxng:latest
DIGEST=""
VERSION=""

# colima ssh re-quotes argv, so arguments with spaces (the Go templates below)
# arrive mangled. Like start-agent.sh's vm_ssh, %q-join them and pipe the line
# in on stdin.
vm() {
  local cmd="" a
  for a in "$@"; do cmd+=" $(printf '%q' "$a")"; done
  printf '%s\n' "$cmd" | colima ssh -p "$PROFILE" -- bash
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --digest)  DIGEST="${2:?--digest needs a value}"; shift ;;
    --version) VERSION="${2:?--version needs a value}"; shift ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "error: unknown argument '$1'" >&2; exit 2 ;;
  esac
  shift
done

[[ -f "$START_AGENT" ]] || { echo "error: $START_AGENT not found" >&2; exit 1; }

if [[ -z "$DIGEST" ]]; then
  command -v colima >/dev/null || { echo "error: colima not on PATH. Run this on the macOS host." >&2; exit 1; }
  # Capture, then match: `colima list | grep -q` under pipefail can fail when
  # grep exits on the first match and colima takes SIGPIPE.
  vm_state=$(colima list -p "$PROFILE" --json 2>/dev/null || true)
  [[ "$vm_state" == *'"Running"'* ]] \
    || { echo "error: Colima VM '$PROFILE' is not running. Start it with start-agent.sh first." >&2; exit 1; }
  echo "==> Pulling $UPSTREAM in the $PROFILE VM"
  vm docker pull -q "$UPSTREAM" >/dev/null
  # The image may carry digests from other repos too (same image ID); take
  # the searxng/searxng one, never just the first.
  repo_digests=$(vm docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$UPSTREAM" | tr -d '\r')
  repo_digest=""
  while IFS= read -r line; do
    case "$line" in
      searxng/searxng@sha256:*|docker.io/searxng/searxng@sha256:*) repo_digest="$line"; break ;;
    esac
  done <<<"$repo_digests"
  if [[ -z "$repo_digest" ]]; then
    echo "error: no searxng/searxng entry in the image's RepoDigests:" >&2
    printf '%s\n' "${repo_digests:-(none)}" >&2
    exit 1
  fi
  DIGEST="${repo_digest##*@}"
  if [[ -z "$VERSION" ]]; then
    VERSION=$(vm docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.version"}}' \
      "$UPSTREAM" 2>/dev/null | tr -d '\r' || true)
    [[ "$VERSION" == "<no value>" ]] && VERSION=""
  fi
fi

if [[ ! "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "error: '$DIGEST' is not a sha256 digest" >&2
  exit 1
fi

# Rewrite the comment block right above SEARXNG_IMAGE and the pin itself.
# Exit 3 from the rewriter means the pin is already current.
rc=0
python3 - "$START_AGENT" "$DIGEST" "$VERSION" "$(date +%Y-%m-%d)" <<'PYEOF' || rc=$?
import re, sys
path, digest, version, today = sys.argv[1:5]
lines = open(path).read().split("\n")
pins = [i for i, l in enumerate(lines) if re.fullmatch(r'SEARXNG_IMAGE="docker\.io/searxng/searxng@sha256:[0-9a-f]{64}"', l)]
if len(pins) != 1:
    sys.exit(f"error: expected one pinned SEARXNG_IMAGE line in {path}, found {len(pins)}")
pin = pins[0]
old = lines[pin].split("@", 1)[1].rstrip('"')
if old == digest:
    print(f"==> SEARXNG_IMAGE is already at {digest}; nothing to do")
    sys.exit(3)
start = pin
while start > 0 and lines[start - 1].startswith("#"):
    start -= 1
what = f"SearXNG {version}, " if version else ""
comment = [
    f"# Pinned by digest ({what}upstream latest and known good on {today}). Bump it",
    "# with scripts/bump-searxng.sh, verify with tests/verify-tool-refresh.sh, then",
    "# commit. Any launch replaces a searxng container on another pin (ADR-054).",
]
lines[start:pin + 1] = comment + [f'SEARXNG_IMAGE="docker.io/searxng/searxng@{digest}"']
open(path, "w").write("\n".join(lines))
print(f"==> SEARXNG_IMAGE: {old[:19]}… → {digest[:19]}…{f' ({version})' if version else ''}")
PYEOF
[[ $rc -eq 3 ]] && exit 0
[[ $rc -eq 0 ]] || exit "$rc"

echo
echo "Not committed. Next:"
echo "  1. start-agent.sh                  (replaces the searxng container with the new pin)"
echo "  2. tests/verify-tool-refresh.sh    (in another terminal, while the container runs)"
echo "  3. commit start-agent.sh if it passes; otherwise: git checkout start-agent.sh, then launch again"
