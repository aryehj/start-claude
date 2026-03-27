#!/usr/bin/env bash
# start-claude.sh — spin up a Claude Code dev container for a project
#
# Usage:
#   start-claude.sh [--rebuild] [project-dir] [container-name]
#
# Defaults:
#   project-dir    = current directory
#   container-name = basename of project-dir

set -euo pipefail

# ── args ──────────────────────────────────────────────────────────────────────
REBUILD=false
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=true ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done

PROJECT_DIR="${POSITIONAL[0]:-$(pwd)}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"          # absolute, resolved
CONTAINER_NAME="${POSITIONAL[1]:-$(basename "$PROJECT_DIR")}"
BASE_IMAGE="${CLAUDE_CONTAINER_IMAGE:-debian:bookworm-slim}"
CONTAINER_MEMORY="${CLAUDE_CONTAINER_MEMORY:-4G}"
CONTAINER_CPUS="${CLAUDE_CONTAINER_CPUS:-4}"
CLAUDE_DIR="$HOME/.claude"
IMAGE_STAMP="$HOME/.claude-dev-image-built"
TERM_ARGS=(-e "TERM=$TERM" -e "COLORTERM=${COLORTERM:-}" -e "TERM_PROGRAM=${TERM_PROGRAM:-}")

# ── pre-flight ─────────────────────────────────────────────────────────────────
if ! command -v container &>/dev/null; then
  echo "error: 'container' CLI not found. Install Apple Containers first." >&2
  exit 1
fi

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "error: project dir '$PROJECT_DIR' does not exist." >&2
  exit 1
fi

# ── ensure container service is running ───────────────────────────────────────
echo "==> Starting container service (no-op if already running)"
container system start

# ── rebuild: remove existing container and image ──────────────────────────────
IMAGE_TAG="claude-dev:latest"

if $REBUILD; then
  if [[ "$(container inspect "$CONTAINER_NAME" 2>/dev/null)" != "[]" ]]; then
    echo "==> --rebuild requested — removing existing container '$CONTAINER_NAME'."
    container rm "$CONTAINER_NAME"
  fi
  if container image inspect "$IMAGE_TAG" &>/dev/null; then
    echo "==> --rebuild requested — removing existing image."
    container image rm "$IMAGE_TAG"
  fi
fi

# ── check for existing container ──────────────────────────────────────────────
if [[ "$(container inspect "$CONTAINER_NAME" 2>/dev/null)" != "[]" ]]; then
  echo "Container '$CONTAINER_NAME' already exists — attaching."
  container start "$CONTAINER_NAME" 2>/dev/null || true
  container exec -it "${TERM_ARGS[@]}" "$CONTAINER_NAME" /bin/bash
  exit 0
fi

if container image inspect "$IMAGE_TAG" &>/dev/null; then
  echo "==> Image $IMAGE_TAG already exists — skipping setup."
  if [[ -f "$IMAGE_STAMP" ]]; then
    BUILD_TIME=$(cat "$IMAGE_STAMP")
    NOW=$(date +%s)
    AGE_DAYS=$(( (NOW - BUILD_TIME) / 86400 ))
    if (( AGE_DAYS >= 30 )); then
      echo "==> Warning: dev image is ${AGE_DAYS} days old. Run with --rebuild to refresh."
    fi
  fi
else
  echo "==> Setting up dev image from ${BASE_IMAGE}"

  # Apple Containers does not auto-pull images on `run`
  if ! container image inspect "$BASE_IMAGE" &>/dev/null; then
    echo "==> Pulling $BASE_IMAGE"
    container image pull "$BASE_IMAGE"
  fi

  SETUP_NAME="claude-dev-setup-$$"
  trap 'container rm "$SETUP_NAME" 2>/dev/null || true' EXIT

  container run --name "$SETUP_NAME" "$BASE_IMAGE" bash -c '
    set -euo pipefail

    # ── system packages ──────────────────────────────────────────────────────
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      bash curl wget git ca-certificates gnupg \
      build-essential python3 python3-pip \
      jq ripgrep fd-find unzip \
      bubblewrap socat libseccomp2 libseccomp-dev
    apt-get upgrade -y

    # Record apt upgrade time for staleness check
    touch /var/lib/apt/last-upgrade

    # ── apt staleness warning (fires on every shell attach) ───────────────
    cat >> /etc/bash.bashrc << '"'"'BASHRC'"'"'
if [[ -f /var/lib/apt/last-upgrade ]]; then
  _apt_age=$(( ($(date +%s) - $(date -r /var/lib/apt/last-upgrade +%s)) / 86400 ))
  if (( _apt_age >= 7 )); then
    echo "Warning: apt packages are ${_apt_age} days old — run: apt-get update && apt-get upgrade"
  fi
  unset _apt_age
fi
BASHRC

    # ── Node.js (LTS) ────────────────────────────────────────────────────────
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
    apt-get install -y nodejs
    rm -rf /var/lib/apt/lists/*

    npm install -g npm@latest @anthropic-ai/sandbox-runtime

    # ── uv ───────────────────────────────────────────────────────────────────
    # UV_INSTALL_DIR puts the binaries directly into /usr/local/bin, so no
    # PATH fixup is needed and the installer does not print the "add to PATH"
    # warning.
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

    # ── Claude Code CLI ──────────────────────────────────────────────────────
    # The installer puts the binary in ~/.local/bin, which is not in the default
    # PATH. Symlink into /usr/local/bin for invocability; also add ~/.local/bin
    # to PATH in .bashrc so the claude binary itself doesn't warn at startup.
    export PATH="/root/.local/bin:$PATH"
    curl -fsSL https://claude.ai/install.sh | bash
    ln -sf /root/.local/bin/claude /usr/local/bin/claude
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> /root/.bashrc
  '

  echo "==> Exporting $IMAGE_TAG"
  container export --image "$IMAGE_TAG" "$SETUP_NAME"

  until container inspect "$SETUP_NAME" 2>/dev/null | grep -q '"status":"stopped"'; do
    sleep 0.1
  done
  container rm "$SETUP_NAME"
  trap - EXIT

  # Record image build time for age check
  date +%s > "$IMAGE_STAMP"
fi

# ── run container ──────────────────────────────────────────────────────────────
echo "==> Creating container '$CONTAINER_NAME'"
echo "    project : $PROJECT_DIR  →  $PROJECT_DIR"
echo "    claude  : $CLAUDE_DIR   →  /root/.claude"

container run \
  --name "$CONTAINER_NAME" \
  -it \
  -m "$CONTAINER_MEMORY" \
  -c "$CONTAINER_CPUS" \
  -v "$PROJECT_DIR:$PROJECT_DIR" \
  -v "$CLAUDE_DIR:/root/.claude" \
  -w "$PROJECT_DIR" \
  "${TERM_ARGS[@]}" \
  "$IMAGE_TAG" \
  /bin/bash
