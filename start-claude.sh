#!/usr/bin/env bash
# new-project.sh — spin up a Claude Code dev container for a project
#
# Usage:
#   new-project.sh [project-dir] [container-name]
#
# Defaults:
#   project-dir    = current directory
#   container-name = basename of project-dir

set -euo pipefail

# ── args ──────────────────────────────────────────────────────────────────────
PROJECT_DIR="${1:-$(pwd)}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"          # absolute, resolved
CONTAINER_NAME="${2:-$(basename "$PROJECT_DIR")}"
BASE_IMAGE="${CLAUDE_CONTAINER_IMAGE:-debian:bookworm-slim}"
CLAUDE_DIR="$HOME/.claude"

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

# ── check for existing container ──────────────────────────────────────────────
if [[ "$(container inspect "$CONTAINER_NAME" 2>/dev/null)" != "[]" ]]; then
  echo "Container '$CONTAINER_NAME' already exists — starting it."
  container start "$CONTAINER_NAME"
  container exec -it "$CONTAINER_NAME" /bin/bash
  exit 0
fi

# ── build dev image (skip if cached) ──────────────────────────────────────────
IMAGE_TAG="claude-dev:latest"
if container image inspect "$IMAGE_TAG" &>/dev/null; then
  echo "==> Image $IMAGE_TAG already exists — skipping setup."
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
    rm -rf /var/lib/apt/lists/*

    # ── Node.js (LTS) ────────────────────────────────────────────────────────
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
    apt-get install -y nodejs
    rm -rf /var/lib/apt/lists/*

    # ── uv ───────────────────────────────────────────────────────────────────
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ln -s /root/.local/bin/uv /usr/local/bin/uv
    ln -s /root/.local/bin/uvx /usr/local/bin/uvx

    # ── Claude Code CLI ──────────────────────────────────────────────────────
    npm install -g @anthropic-ai/claude-code
  '

  echo "==> Exporting $IMAGE_TAG"
  container export --image "$IMAGE_TAG" "$SETUP_NAME"
  container rm "$SETUP_NAME"
  trap - EXIT
fi

# ── run container ──────────────────────────────────────────────────────────────
echo "==> Creating container '$CONTAINER_NAME'"
echo "    project : $PROJECT_DIR  →  $PROJECT_DIR"
echo "    claude  : $CLAUDE_DIR   →  /root/.claude"

container run \
  --name "$CONTAINER_NAME" \
  -it \
  -v "$PROJECT_DIR:$PROJECT_DIR" \
  -v "$CLAUDE_DIR:/root/.claude" \
  -w "$PROJECT_DIR" \
  "$IMAGE_TAG" \
  /bin/bash
