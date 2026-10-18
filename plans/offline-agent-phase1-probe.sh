#!/usr/bin/env bash
# Phase 1 go/no-go probe for plans/offline-agent.md. Throwaway spike, not for
# commit. Run ON THE MAC HOST (needs colima + docker):
#
#   bash plans/offline-agent-phase1-probe.sh
#
# Prereq for step 3: the claude-agent Colima VM must be running with
# claude-agent:latest built (start-agent.sh has been run at least once). The
# image is copied into the new VM with docker save | docker load.
#
# Output is tee'd to plans/offline-agent-phase1-results.txt so the results can
# be read back from inside the dev container. Written for macOS bash 3.2.
set -u

PROFILE=offline-agent
SRC_PROFILE=claude-agent
IMAGE=claude-agent:latest
WORK="$HOME/offline-agent-phase1-scratch"
REPO="$WORK/repo"
PROBE="$WORK/probe"
SESSION_DIR=/var/lib/offline-agent/sessions/phase1
VOL=offline-agent-phase1
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HERE/offline-agent-phase1-results.txt"
D="docker --context colima-$PROFILE"

exec > >(tee "$LOG") 2>&1

section() { printf '\n========== %s ==========\n' "$*"; }
vm_sh()   { printf '%s\n' "$1" | colima ssh -p "$PROFILE" -- bash; }
result()  { printf 'RESULT %-40s %s\n' "$1" "$2"; }

manifest() {
  # Content + name manifest of the host repo (incl. .git), mtime-insensitive.
  ( cd "$REPO" && find . | LC_ALL=C sort && find . -type f -exec shasum {} + | LC_ALL=C sort -k2 )
}

section "0. preflight"
date; sw_vers 2>/dev/null; colima version; docker version --format 'docker client {{.Client.Version}}'
command -v colima >/dev/null && command -v docker >/dev/null || { echo "need colima + docker"; exit 1; }

# ── 1. VM ─────────────────────────────────────────────────────────────────────
section "1. colima profile $PROFILE"
if colima status -p "$PROFILE" >/dev/null 2>&1; then
  echo "already running"
else
  t0=$(date +%s)
  colima start -p "$PROFILE" --vm-type vz --runtime docker --cpu 6 --memory 8 \
    --mount-type virtiofs --network-address || { echo "colima start failed"; exit 1; }
  echo "cold start took $(( $(date +%s) - t0 ))s"
fi
vm_sh 'uname -a; echo; echo "--- virtiofs / HOME mounts:"; mount | grep -E "virtiofs|'"$HOME"'" ; echo; echo "--- overlay in /proc/filesystems:"; grep overlay /proc/filesystems'
$D info --format 'docker server {{.ServerVersion}}, storage {{.Driver}}, rootdir {{.DockerRootDir}}'

# ── image copy (needed by steps 2 and 3: has git, node, opencode, pi) ──────────
section "image: copy $IMAGE from colima-$SRC_PROFILE"
if $D image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "already present in $PROFILE"
else
  t0=$(date +%s)
  docker --context "colima-$SRC_PROFILE" save "$IMAGE" | $D load \
    || { echo "image copy failed: start the $SRC_PROFILE VM (start-agent.sh) and re-run"; exit 1; }
  echo "copy took $(( $(date +%s) - t0 ))s"
fi

# ── 2. overlay over virtiofs ─────────────────────────────────────────────────
section "2a. scratch repo at $REPO"
rm -rf "$WORK"; mkdir -p "$REPO/sub/dir" "$PROBE"
( cd "$REPO" && git init -q && echo keep > keep.txt && echo del > delete-me.txt \
  && echo ren > rename-me.txt && echo deep > sub/dir/file.txt && git add -A \
  && git -c user.name=probe -c user.email=probe@x commit -qm base )
HOST_HEAD_BEFORE=$(git -C "$REPO" rev-parse HEAD)
manifest > "$PROBE/manifest-before.txt"
echo "host HEAD $HOST_HEAD_BEFORE, $(wc -l < "$PROBE/manifest-before.txt") manifest lines"
vm_sh "ls -la '$REPO'" && result "repo visible in VM at host path" yes || result "repo visible in VM at host path" NO

section "2b. do upper/work dirs need to pre-exist?"
$D volume rm -f "$VOL-nodirs" >/dev/null 2>&1
vm_sh "sudo rm -rf /var/lib/offline-agent/sessions/phase1-nodirs"
$D volume create --driver local --opt type=overlay --opt device=overlay \
  --opt "o=lowerdir=$REPO,upperdir=/var/lib/offline-agent/sessions/phase1-nodirs/upper,workdir=/var/lib/offline-agent/sessions/phase1-nodirs/work" \
  "$VOL-nodirs" >/dev/null
if $D run --rm -v "$VOL-nodirs:$REPO" "$IMAGE" ls "$REPO" >/dev/null 2>&1; then
  result "mount with missing upper/work" "WORKS (dirs auto-created?)"
else
  result "mount with missing upper/work" "fails (pre-create required)"
fi
$D volume rm -f "$VOL-nodirs" >/dev/null 2>&1

section "2c. overlay volume with pre-created dirs"
$D volume rm -f "$VOL" >/dev/null 2>&1
vm_sh "sudo rm -rf $SESSION_DIR && sudo mkdir -p $SESSION_DIR/upper $SESSION_DIR/work && echo '$REPO' | sudo tee $SESSION_DIR/path >/dev/null"
$D volume create --driver local --opt type=overlay --opt device=overlay \
  --opt "o=lowerdir=$REPO,upperdir=$SESSION_DIR/upper,workdir=$SESSION_DIR/work" "$VOL"

cat > "$PROBE/mutate.sh" <<'EOF'
set -eux
git config --global --add safe.directory '*'
cd "$1"
stat -c 'owner of repo root inside container: %u:%g' .
echo modified >> keep.txt
echo new > new.txt
rm delete-me.txt
git mv rename-me.txt renamed.txt
rm -rf sub
mkdir -p newdir && echo x > newdir/y
git add -A
git -c user.name=probe -c user.email=probe@x commit -qm overlay-commit
git log --oneline
git status --short
mkdir -p .git/hooks && printf '#!/bin/sh\necho HOOK RAN\n' > .git/hooks/post-checkout && chmod +x .git/hooks/post-checkout
EOF
cat > "$PROBE/check.sh" <<'EOF'
git config --global --add safe.directory '*'
cd "$1"
ok=1
[ -f new.txt ] || { echo "missing new.txt"; ok=0; }
[ ! -e delete-me.txt ] || { echo "delete-me.txt came back"; ok=0; }
[ -f renamed.txt ] && [ ! -e rename-me.txt ] || { echo "rename lost"; ok=0; }
[ ! -e sub ] || { echo "sub/ came back"; ok=0; }
grep -q modified keep.txt || { echo "keep.txt edit lost"; ok=0; }
git log --oneline | grep -q overlay-commit || { echo "overlay commit lost"; ok=0; }
[ -z "$(git status --porcelain)" ] || { echo "merged view dirty:"; git status --short; ok=0; }
git fsck --no-progress >/dev/null 2>&1 || { echo "git fsck failed"; ok=0; }
[ $ok = 1 ] && echo MERGED_VIEW_OK || echo MERGED_VIEW_BAD
EOF

if $D run --rm -v "$VOL:$REPO" -v "$PROBE:/probe" -w "$REPO" "$IMAGE" bash /probe/mutate.sh "$REPO"; then
  result "write/delete/rename/commit in overlay" ok
else
  result "write/delete/rename/commit in overlay" FAILED
  vm_sh 'sudo dmesg | tail -20'
fi

section "2d. host untouched?"
manifest > "$PROBE/manifest-after.txt"
if diff "$PROBE/manifest-before.txt" "$PROBE/manifest-after.txt"; then
  result "host repo byte-identical" yes
else
  result "host repo byte-identical" NO
fi
[ "$(git -C "$REPO" rev-parse HEAD)" = "$HOST_HEAD_BEFORE" ] && result "host HEAD unchanged" yes || result "host HEAD unchanged" NO
[ -z "$(git --no-optional-locks -C "$REPO" status --porcelain)" ] && result "host git status clean" yes || result "host git status clean" NO

section "2e. upper dir contents (c = whiteout char device)"
vm_sh "cd $SESSION_DIR/upper && sudo find . -not -path './.git/objects/*' -printf '%y %m %p\n' | sort -k3; echo; echo \".git/objects files in upper: \$(sudo find .git/objects -type f | wc -l)\"; echo; echo '--- overlay xattrs:'; sudo getfattr -R -d -m 'trusted.overlay' . 2>/dev/null | head -30 || echo '(getfattr unavailable)'"
vm_sh "mount | grep $SESSION_DIR || mount | grep overlay | grep -v docker/overlay2 | head"

section "2f. persistence across containers"
$D run --rm -v "$VOL:$REPO" -v "$PROBE:/probe" "$IMAGE" bash /probe/check.sh "$REPO" | tee "$PROBE/check1.txt"
grep -q MERGED_VIEW_OK "$PROBE/check1.txt" && result "persists across container restart" yes || result "persists across container restart" NO

section "2g. persistence across colima stop/start"
t0=$(date +%s)
colima stop -p "$PROFILE" && colima start -p "$PROFILE"
echo "stop+start took $(( $(date +%s) - t0 ))s"
vm_sh "sudo ls -la $SESSION_DIR $SESSION_DIR/upper; cat $SESSION_DIR/path"
$D run --rm -v "$VOL:$REPO" -v "$PROBE:/probe" "$IMAGE" bash /probe/check.sh "$REPO" | tee "$PROBE/check2.txt"
grep -q MERGED_VIEW_OK "$PROBE/check2.txt" && result "persists across colima stop/start" yes || result "persists across colima stop/start" NO
manifest > "$PROBE/manifest-after2.txt"
diff -q "$PROBE/manifest-before.txt" "$PROBE/manifest-after2.txt" >/dev/null && result "host still byte-identical" yes || result "host still byte-identical" NO

section "2h. host-side edit made between sessions: visible in merged view?"
echo host-edit > "$REPO/host-only.txt"
$D run --rm -v "$VOL:$REPO" "$IMAGE" sh -c "cat '$REPO/host-only.txt' 2>&1; ls '$REPO'"
rm -f "$REPO/host-only.txt"

# ── 3. offline harness startup ───────────────────────────────────────────────
section "3. opencode + pi with --network none"
mkdir -p "$PROBE/oc-config" "$PROBE/oc-data" "$PROBE/pi/agent"
cat > "$PROBE/oc-config/opencode.json" <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "share": "disabled",
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "local",
      "options": { "baseURL": "http://192.0.2.2:8000/v1", "apiKey": "omlx" },
      "models": { "test-model": { "name": "test-model" } }
    }
  },
  "model": "local/test-model",
  "permission": { "webfetch": "deny", "websearch": "deny" }
}
EOF
cat > "$PROBE/pi/agent/models.json" <<'EOF'
{ "providers": { "local": { "baseUrl": "http://192.0.2.2:8000/v1", "api": "openai-completions",
  "apiKey": "omlx", "models": [ { "id": "test-model" } ] } } }
EOF
cat > "$PROBE/pi/agent/settings.json" <<'EOF'
{ "defaultProvider": "local", "defaultModel": "test-model" }
EOF
# Logs the request line of every proxied request (CONNECT host:port / GET url)
# and refuses it, so we learn which hosts each harness tries to reach.
cat > "$PROBE/logproxy.js" <<'EOF'
const net = require('net'), fs = require('fs');
net.createServer(s => {
  s.on('error', () => {});
  s.once('data', d => {
    fs.appendFileSync(process.argv[2], d.toString().split('\r\n')[0] + '\n');
    s.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');
  });
}).listen(3128, '127.0.0.1');
EOF
cat > "$PROBE/harness.sh" <<'EOF'
# $1 = label; runs every harness command, timing each.
label=$1
now() { date +%s.%N; }
run() {
  name=$1; shift
  s=$(now); out=$(timeout 60 "$@" 2>&1 </dev/null); rc=$?
  el=$(awk "BEGIN{printf \"%.1f\", $(now)-$s}")
  printf -- '--- [%s] %s  rc=%s  %ss\n' "$label" "$name" "$rc" "$el"
  printf '%s\n' "$out" | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' | tail -15
}
tui() {
  # TUI under a pty for 20s: rc=124 means it was still running (did not crash).
  name=$1; shift
  s=$(now); script -qec "timeout 20 $*" /probe/tui-$label-$name.raw >/dev/null 2>&1 </dev/null; rc=$?
  el=$(awk "BEGIN{printf \"%.1f\", $(now)-$s}")
  printf -- '--- [%s] %s TUI  rc=%s (124 = alive after 20s)  %ss\n' "$label" "$name" "$rc" "$el"
  sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/\x1b[]P][^\x07]*\x07//g' /probe/tui-$label-$name.raw | tr -s ' \r' | grep -v '^\s*$' | tail -8
}
export TERM=xterm-256color
run opencode-version opencode --version
run opencode-models  opencode models
run opencode-run     opencode run "reply with OK"
tui opencode         opencode
run pi-version       pi --version
run pi-print         pi -p "reply with OK"
tui pi               pi
echo "--- [$label] files written under /root/.cache /root/.local/share/opencode:"
find /root/.cache /root/.local/share/opencode -maxdepth 3 2>/dev/null | head -30
EOF

oc_run() {
  # $1 label, rest: extra docker args
  label=$1; shift
  rm -rf "$PROBE/oc-data"/* "$PROBE/pi/agent/sessions"
  $D run --rm --network none \
    -v "$PROBE:/probe" \
    -v "$PROBE/oc-config:/root/.config/opencode" \
    -v "$PROBE/oc-data:/root/.local/share/opencode" \
    -v "$PROBE/pi:/root/.pi" \
    -w "$REPO" -v "$VOL:$REPO" \
    "$@" "$IMAGE" bash -c "$PROXY_PRELUDE bash /probe/harness.sh $label"
}

section "3a. plain --network none (failure modes + latency)"
PROXY_PRELUDE=""
oc_run plain

section "3b. --network none + logging proxy (which hosts do they try?)"
: > "$PROBE/egress-proxy.log"
PROXY_PRELUDE="node /probe/logproxy.js /probe/egress-proxy.log & sleep 0.5;"
oc_run proxied -e HTTPS_PROXY=http://127.0.0.1:3128 -e HTTP_PROXY=http://127.0.0.1:3128 -e NODE_USE_ENV_PROXY=1
echo "--- attempted destinations (count, request line):"
sort "$PROBE/egress-proxy.log" | uniq -c | sort -rn

section "3c. same, with OPENCODE_DISABLE_* env flags"
: > "$PROBE/egress-proxy-flags.log"
PROXY_PRELUDE="node /probe/logproxy.js /probe/egress-proxy-flags.log & sleep 0.5;"
oc_run flags -e HTTPS_PROXY=http://127.0.0.1:3128 -e HTTP_PROXY=http://127.0.0.1:3128 -e NODE_USE_ENV_PROXY=1 \
  -e OPENCODE_DISABLE_AUTOUPDATE=1 -e OPENCODE_DISABLE_MODELS_FETCH=1 -e OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
  -e OPENCODE_DISABLE_DEFAULT_PLUGINS=1
echo "--- attempted destinations with flags:"
sort "$PROBE/egress-proxy-flags.log" | uniq -c | sort -rn

section "summary"
grep '^RESULT' "$LOG"
echo
echo "Done. Scratch state left for inspection:"
echo "  host: $WORK    VM: $SESSION_DIR    volume: $VOL (context colima-$PROFILE)"
echo "Cleanup: rm -rf '$WORK'; $D volume rm $VOL; colima ssh -p $PROFILE -- sudo rm -rf $SESSION_DIR"
echo "The $PROFILE VM is left running for Phase 2."
