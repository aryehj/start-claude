#!/usr/bin/env bash
# test-offline-agent-limits.sh — validate the offline-agent.py storage and
# resource bounds from inside a running offline-agent session container.
#
# It fills the session image, so run it in a scratch project whose session
# was created with a small cap, so the fill is quick:
#     mkdir -p ~/scratch/oa-limits && cd ~/scratch/oa-limits
#     OFFLINE_AGENT_SESSION_DISK=256M /path/to/start-claude/offline-agent.py
# then, from the macOS host in the same directory while that session is open:
#     docker --context colima-offline-agent exec -i \
#       "offline-agent-$(printf %s "$PWD" | shasum -a 256 | cut -c1-12)" \
#       bash -s < /path/to/start-claude/tests/test-offline-agent-limits.sh
# (the cap applies only when the session is created; an existing session
# keeps its size)
#
# Override the expected container caps if offline-agent.py's constants change:
#   EXPECT_PIDS (default 512), EXPECT_MEMORY_BYTES (default 1536 MiB),
#   EXPECT_TMP_KIB (default 512 MiB).
#
# Every fill file is deleted on exit. Exit 0 if every test passes, 1 otherwise.

set -u

PASS=0
FAIL=0

if [[ -t 1 ]]; then
  C_OK=$'\e[32m'
  C_ERR=$'\e[31m'
  C_RST=$'\e[0m'
else
  C_OK=""; C_ERR=""; C_RST=""
fi

pass() { printf '%sPASS%s  %s\n' "$C_OK" "$C_RST" "$1"; PASS=$((PASS+1)); }
fail() { printf '%sFAIL%s  %s%s\n' "$C_ERR" "$C_RST" "$1" "${2:+ — $2}"; FAIL=$((FAIL+1)); }

EXPECT_PIDS="${EXPECT_PIDS:-512}"
EXPECT_MEMORY_BYTES="${EXPECT_MEMORY_BYTES:-$((1536 * 1024 * 1024))}"
EXPECT_TMP_KIB="${EXPECT_TMP_KIB:-$((512 * 1024))}"
HARNESS_DIR=/root/.local/share/opencode
# Upper bound on how long a fill may take; a 256M cap fills in seconds.
FILL_TIMEOUT=300

PROJECT_FILL="$PWD/.oa-limits-fill"
HARNESS_FILL="$HARNESS_DIR/.oa-limits-fill"
TMP_FILL=/tmp/.oa-limits-fill
cleanup() { rm -f "$PROJECT_FILL" "$HARNESS_FILL" "$TMP_FILL"; }
trap cleanup EXIT

echo "==> offline-agent limits smoke tests"
echo

# Size of the filesystem holding $1, in KiB.
fs_size_kib() { df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $2}'; }

# fill NAME FILE CAP_KIB: write zeros until the filesystem refuses, then check
# the refusal is ENOSPC and that no more than the cap was written.
fill() {
  local name=$1 file=$2 cap_kib=$3 err written_kib
  err=$(timeout "$FILL_TIMEOUT" dd if=/dev/zero of="$file" bs=1M status=none 2>&1)
  local rc=$?
  written_kib=$(( $(stat -c %s "$file" 2>/dev/null || echo 0) / 1024 ))
  rm -f "$file"
  if (( rc == 124 )); then
    fail "$name" "no ENOSPC within ${FILL_TIMEOUT}s (wrote ${written_kib} KiB) — is the session cap small?"
  elif [[ "$err" != *"No space left on device"* ]]; then
    fail "$name" "dd exit $rc without ENOSPC: ${err:0:120}"
  elif (( written_kib > cap_kib )); then
    fail "$name" "wrote ${written_kib} KiB, more than the ${cap_kib} KiB cap"
  else
    pass "$name: ENOSPC after ${written_kib} KiB (cap ${cap_kib} KiB)"
  fi
}

# ── 1. Read-only root ────────────────────────────────────────────────────────
test_root_read_only() {
  local err
  if err=$(touch /.oa-limits-probe 2>&1); then
    rm -f /.oa-limits-probe
    fail "root-read-only" "wrote to /"
  elif [[ "$err" == *"Read-only file system"* ]]; then
    pass "root-read-only: / refuses writes"
  else
    fail "root-read-only" "write failed, but not with EROFS: $err"
  fi
}

# ── 2. Project dir and harness state share the session image ─────────────────
# The overlay upper layer and the harness state dir are both on the session's
# loop-mounted ext4 image, so both report its size.
test_harness_on_session_image() {
  local fstype source
  fstype=$(findmnt -n -o FSTYPE -T "$HARNESS_DIR" 2>/dev/null)
  source=$(findmnt -n -o SOURCE -T "$HARNESS_DIR" 2>/dev/null)
  if [[ "$fstype" != "ext4" || "$source" != /dev/loop* ]]; then
    fail "harness-on-session-image" "$HARNESS_DIR is on '$fstype' from '$source', not the session's ext4 loop image"
  elif ! touch "$HARNESS_DIR/.oa-limits-probe" 2>/dev/null; then
    fail "harness-on-session-image" "$HARNESS_DIR is not writable"
  else
    rm -f "$HARNESS_DIR/.oa-limits-probe"
    pass "harness-on-session-image: $HARNESS_DIR writable on $source (ext4)"
  fi
}

test_project_fill() {
  local fstype
  fstype=$(findmnt -n -o FSTYPE -T "$PWD" 2>/dev/null)
  if [[ "$fstype" != "overlay" ]]; then
    fail "project-fill" "$PWD is on '${fstype:-unknown}', not overlay — run from the session's project dir"
    return
  fi
  fill "project-fill" "$PROJECT_FILL" "$(fs_size_kib "$HARNESS_DIR")"
}

test_harness_fill() {
  fill "harness-fill" "$HARNESS_FILL" "$(fs_size_kib "$HARNESS_DIR")"
}

# ── 3. /tmp is a sized tmpfs ─────────────────────────────────────────────────
test_tmp_fill() {
  local fstype size
  fstype=$(findmnt -n -o FSTYPE -T /tmp 2>/dev/null)
  size=$(fs_size_kib /tmp)
  if [[ "$fstype" != "tmpfs" ]]; then
    fail "tmp-fill" "/tmp is on '${fstype:-unknown}', not tmpfs"
    return
  fi
  if (( size > EXPECT_TMP_KIB )); then
    fail "tmp-fill" "/tmp tmpfs is ${size} KiB, expected at most ${EXPECT_TMP_KIB} KiB"
    return
  fi
  fill "tmp-fill" "$TMP_FILL" "$size"
}

# ── 4. Nothing writable is backed by the Mac ─────────────────────────────────
# Mac paths reach the VM over virtiofs; the project's virtiofs path is only
# the overlay's lower layer, which never appears as a mount of its own.
test_no_writable_mac_mount() {
  local bad
  bad=$(findmnt -rn -o TARGET,FSTYPE,OPTIONS | awk '$2 == "virtiofs" && $3 ~ /(^|,)rw(,|$)/ {print $1}')
  if [[ -z "$bad" ]]; then
    pass "no-writable-mac-mount: every virtiofs mount is read-only"
  else
    fail "no-writable-mac-mount" "read-write virtiofs mounts: $(echo $bad)"
  fi
}

# ── 5. cgroup caps ───────────────────────────────────────────────────────────
# Read, not fork-bombed or OOMed.
test_cgroup_caps() {
  local pids mem
  pids=$(cat /sys/fs/cgroup/pids.max 2>/dev/null)
  mem=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)
  if [[ "$pids" == "$EXPECT_PIDS" ]]; then
    pass "pids-max: $pids"
  else
    fail "pids-max" "pids.max is '${pids:-unreadable}', expected $EXPECT_PIDS"
  fi
  if [[ "$mem" == "$EXPECT_MEMORY_BYTES" ]]; then
    pass "memory-max: $mem bytes"
  else
    fail "memory-max" "memory.max is '${mem:-unreadable}', expected $EXPECT_MEMORY_BYTES"
  fi
}

test_root_read_only
test_harness_on_session_image
test_project_fill
test_harness_fill
test_tmp_fill
test_no_writable_mac_mount
test_cgroup_caps

echo
printf '==> %s%d passed%s, %s%d failed%s\n' \
  "$C_OK" "$PASS" "$C_RST" \
  "$C_ERR" "$FAIL" "$C_RST"

(( FAIL == 0 ))
