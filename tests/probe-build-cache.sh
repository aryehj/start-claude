#!/usr/bin/env bash
# probe-build-cache.sh — what does the claude-agent VM's docker build cache
# keep? (ADR-054, fourth amendment.)
#
# A second --refresh-tools on the single-Dockerfile layout rebuilt 12 of 13
# layers. The suspected (unconfirmed) cause: with the legacy builder, a layer's
# cache entry hangs off the image whose build first created it, so removing that
# image (drop_untagged_image removes the old :previous after each refresh) loses
# the cache hit for later images that only reused the layer, though they still
# hold it. start-agent.sh no longer depends on the cache (the base is a tagged
# image), so this probe only explains what happened.
#
# With tiny throwaway images (tags cache-probe-*, removed at the end) built on
# the base image the Dockerfile uses, it:
#   1. records the client/server versions, buildx, and the image store (graph
#      driver or containerd snapshotter);
#   2. builds A (step 1, step 2a) and B (same step 1, step 2b): did B reuse A's
#      step 1? Step 1 writes a timestamp, so equal timestamps mean a cache hit;
#   3. removes A, keeps B, builds C (same step 1, step 2c): is step 1 still
#      cached while B still uses the layer? A miss here supports the hypothesis;
#   4. removes B too and builds D (same step 1): is step 1 cached with no image
#      holding it (the "cache does not survive docker image rm" observation)?
# then prints a one-line conclusion.
#
# Run from the macOS host with the claude-agent VM running:
#     tests/probe-build-cache.sh
# Uses the same docker client and context as start-agent.sh. Log:
# .verify/probe-build-cache.log

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
mkdir -p .verify
LOG="$REPO/.verify/probe-build-cache.log"
exec > >(tee "$LOG") 2>&1

PROFILE=claude-agent
TAGS=(cache-probe-a:latest cache-probe-b:latest cache-probe-c:latest cache-probe-d:latest)

command -v colima >/dev/null 2>&1 \
  || { echo "error: colima not on PATH. Run this on the macOS host."; exit 1; }
colima list -p "$PROFILE" --json 2>/dev/null | grep -q '"Running"' \
  || { echo "error: the $PROFILE VM is not running. Start it once with: start-agent.sh"; exit 1; }
if docker context inspect "colima-$PROFILE" >/dev/null 2>&1; then
  export DOCKER_CONTEXT="colima-$PROFILE"
fi

cleanup() {
  local t
  for t in "${TAGS[@]}"; do
    docker image rm "$t" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

echo "==> build cache probe ($(date '+%Y-%m-%d %H:%M:%S')), context ${DOCKER_CONTEXT:-default}"

# ── 1. environment ───────────────────────────────────────────────────────────
echo "INFO  docker client $(docker version -f '{{.Client.Version}}' 2>/dev/null), server $(docker version -f '{{.Server.Version}}' 2>/dev/null)"
buildx=$(docker buildx version 2>/dev/null | head -n 1)
echo "INFO  buildx: ${buildx:-none (docker build uses the legacy builder)}"
echo "INFO  DOCKER_BUILDKIT=${DOCKER_BUILDKIT:-unset}"
driver=$(docker info -f '{{.Driver}}' 2>/dev/null)
driver_status=$(docker info -f '{{json .DriverStatus}}' 2>/dev/null)
echo "INFO  storage driver: ${driver:-unknown}"
echo "INFO  driver status: ${driver_status:-unknown}"
if grep -q 'io.containerd.snapshotter' <<<"$driver_status$driver"; then
  store="containerd snapshotter"
else
  store="graph driver"
fi
echo "INFO  image store: $store"

base=$(awk '/^FROM /{print $2; exit}' dockerfiles/claude-agent.Dockerfile)
if ! docker image inspect "$base" >/dev/null 2>&1; then
  echo "error: $base (the FROM of dockerfiles/claude-agent.Dockerfile) is not in the VM; build claude-agent once first."
  exit 1
fi
echo "INFO  probe base image: $base"

# A fresh nonce per run, so step 1 never matches a previous run's cache.
nonce="$(date +%s)-$$"
step1="RUN echo cache-probe-$nonce >/dev/null && date +%s%N > /step1"

# build <tag> <step-2 marker>: echoes the image's /step1, empty on failure.
build() {
  local tag=$1 marker=$2 out
  echo "--- docker build $tag" >&2
  out=$(printf 'FROM %s\n%s\nRUN echo %s > /step2\n' "$base" "$step1" "$marker" \
    | docker build --network=host -t "$tag" - 2>&1)
  local rc=$?
  printf '%s\n' "$out" | grep -E 'Using cache|CACHED|Step |^#[0-9]+ ' | sed 's/^/      /' >&2
  [[ $rc -eq 0 ]] || { printf '%s\n' "$out" | tail -n 5 | sed 's/^/      /' >&2; return 0; }
  echo "      parent: $(docker image inspect -f '{{.Parent}}' "$tag" 2>/dev/null || true)" >&2
  docker run --rm --network none --entrypoint cat "$tag" /step1 2>/dev/null
}
verdict() { [[ -n "$1" && "$1" == "$2" ]] && echo hit || echo miss; }

# ── 2. A, then B with the same step 1 ────────────────────────────────────────
echo
a=$(build cache-probe-a:latest a)
b=$(build cache-probe-b:latest b)
if [[ -z "$a" || -z "$b" ]]; then
  echo "FAIL  probe images built — see the build output above"
  exit 1
fi
ab=$(verdict "$a" "$b")
echo "INFO  A step 1 = $a; B step 1 = $b → B's step 1: cache $ab"

# ── 3. remove A (B still holds the layer), build C ───────────────────────────
echo
echo "--- docker image rm cache-probe-a:latest"
docker image rm cache-probe-a:latest 2>&1 | sed 's/^/      /'
c=$(build cache-probe-c:latest c)
bc=$(verdict "$b" "$c")
echo "INFO  C step 1 = ${c:-?} → with A removed and B present, C's step 1: cache $bc"

# ── 4. remove B and C too (no image holds the layer), build D ────────────────
echo
echo "--- docker image rm cache-probe-b:latest cache-probe-c:latest"
docker image rm cache-probe-b:latest cache-probe-c:latest 2>&1 | sed 's/^/      /'
d=$(build cache-probe-d:latest d)
# Against C's step 1: on a miss in step 3, C built the layer anew.
if [[ -n "$c" ]]; then cd=$(verdict "$c" "$d"); else cd=$(verdict "$b" "$d"); fi
echo "INFO  D step 1 = ${d:-?} → with no probe image left, D's step 1: cache $cd"

# ── conclusion ───────────────────────────────────────────────────────────────
echo
summary="$store, buildx $([[ -n "$buildx" ]] && echo yes || echo no)"
if [[ "$ab" == miss ]]; then
  echo "RESULT  no cache reuse even between two back-to-back builds ($summary): the builder is not caching at all here"
elif [[ "$bc" == miss ]]; then
  echo "RESULT  hypothesis SUPPORTED ($summary): removing the image that first built a layer dropped its cache although another image still uses the layer"
else
  echo "RESULT  hypothesis NOT supported ($summary): the layer stayed cached after its first image was removed while another image used it; with no image left: cache $cd"
fi
echo "(log: $LOG)"
