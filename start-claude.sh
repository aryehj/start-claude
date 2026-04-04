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
IMAGE_STAMP="$HOME/.claude-dev-image-built"
CLAUDE_CONFIG_DIR="$HOME/.claude-containers/shared"
CLAUDE_JSON_FILE="$HOME/.claude-containers/claude.json"
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

# ── inject project settings ───────────────────────────────────────────────────
# settings.local.json is gitignored by Claude Code and project-specific.
PROJECT_SETTINGS_FILE="$PROJECT_DIR/.claude/settings.local.json"
if [[ -f "$PROJECT_SETTINGS_FILE" ]]; then
  # Migrate sandbox settings: bool→object form, ensure filesystem.allowWrite
  python3 - "$PROJECT_SETTINGS_FILE" << 'PYEOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)
changed = False
if 'theme' not in data:
    data['theme'] = 'light'
    changed = True
    print(f"==> Added theme:light to {path}")
if isinstance(data.get('sandbox'), bool):
    data['sandbox'] = {"enabled": True, "autoAllowBashIfSandboxed": True}
    changed = True
    print(f"==> Migrated sandbox boolean→object in {path}")
sb = data.setdefault('sandbox', {})
fs = sb.setdefault('filesystem', {})
aw = fs.setdefault('allowWrite', [])
for p in ['/tmp/uv-cache', '$TMPDIR/uv-cache']:
    if p not in aw:
        aw.append(p)
        changed = True
        print(f"==> Added {p} to sandbox.filesystem.allowWrite in {path}")
if changed:
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
PYEOF
else
  mkdir -p "$PROJECT_DIR/.claude"
  printf '{\n  "theme": "light",\n  "sandbox": {\n    "enabled": true,\n    "autoAllowBashIfSandboxed": true,\n    "filesystem": {\n      "allowWrite": ["/tmp/uv-cache", "$TMPDIR/uv-cache"]\n    }\n  }\n}\n' > "$PROJECT_SETTINGS_FILE"
  echo "==> Created $PROJECT_SETTINGS_FILE"
fi

# ── check for existing container ──────────────────────────────────────────────
if [[ "$(container inspect "$CONTAINER_NAME" 2>/dev/null)" != "[]" ]]; then
  echo "Container '$CONTAINER_NAME' already exists — attaching."
  container start "$CONTAINER_NAME" 2>/dev/null || true
  container exec -it -w "$PROJECT_DIR" "${TERM_ARGS[@]}" "$CONTAINER_NAME" /bin/bash
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

  container run --name "$SETUP_NAME" -m "$CONTAINER_MEMORY" "$BASE_IMAGE" bash -c '
    set -euo pipefail

    # ── system packages ──────────────────────────────────────────────────────
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      bash curl wget git ca-certificates gnupg \
      build-essential python3 python3-pip \
      jq ripgrep fd-find unzip \
      bubblewrap socat libseccomp2 libseccomp-dev
    apt-get upgrade -y
    rm -rf /var/lib/apt/lists/*

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

    npm install -g npm@latest @anthropic-ai/sandbox-runtime@0.0.46

    # ── uv ───────────────────────────────────────────────────────────────────
    # UV_INSTALL_DIR puts the binaries directly into /usr/local/bin, so no
    # PATH fixup is needed and the installer does not print the "add to PATH"
    # warning.
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

    # ── Claude Code CLI ──────────────────────────────────────────────────────
    # The installer puts the binary in ~/.local/bin, which is not in the default
    # PATH. Symlink into /usr/local/bin for invocability; also add ~/.local/bin
    # to PATH in .bashrc so the claude binary itself does not warn at startup.
    export PATH="/root/.local/bin:$PATH"
    curl -fsSL https://claude.ai/install.sh | bash
    ln -sf /root/.local/bin/claude /usr/local/bin/claude
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> /root/.bashrc
    # UV needs a writable cache dir; /root/.cache is read-only in the sandbox.
    # The sandbox sets $TMPDIR to a guaranteed-writable directory, but /tmp
    # itself may be read-only at the mount level inside bubblewrap. Use $TMPDIR
    # dynamically so UV writes to the sandbox-writable temp dir. Fall back to
    # /tmp for interactive use outside the sandbox.
    cat >> /root/.bashrc << '"'"'UVEOF'"'"'
export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"
mkdir -p "$UV_CACHE_DIR" 2>/dev/null || true
UVEOF
    cat > /etc/profile.d/uv-cache.sh << '"'"'UVEOF'"'"'
export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"
mkdir -p "$UV_CACHE_DIR" 2>/dev/null || true
UVEOF
  '

  echo "==> Exporting $IMAGE_TAG"
  until container inspect "$SETUP_NAME" 2>/dev/null | grep -q '"status":"stopped"'; do
    sleep 0.1
  done
  BUILD_TMP=$(mktemp -d)
  container export --output "$BUILD_TMP/rootfs.tar" "$SETUP_NAME"
  container rm "$SETUP_NAME"
  trap - EXIT
  printf 'FROM scratch\nADD rootfs.tar /\nCMD ["/bin/bash"]\n' > "$BUILD_TMP/Dockerfile"
  if ! container builder status 2>/dev/null | grep -q "running"; then
    echo "==> Starting image builder..."
    container builder start
    until container builder status 2>/dev/null | grep -q "running"; do
      sleep 1
    done
  fi
  container build -t "$IMAGE_TAG" "$BUILD_TMP"
  rm -rf "$BUILD_TMP"
  echo "==> Stopping image builder..."
  container builder stop

  # Record image build time for age check
  date +%s > "$IMAGE_STAMP"
fi

# ── run container ──────────────────────────────────────────────────────────────
echo "==> Creating container '$CONTAINER_NAME'"
echo "    project : $PROJECT_DIR  →  $PROJECT_DIR"

mkdir -p "$CLAUDE_CONFIG_DIR"
# Ensure the top-level .claude.json exists on the host so the bind mount
# below resolves to a file (not an auto-created directory). This file holds
# oauthAccount and other auth state that Claude Code writes outside ~/.claude,
# so it needs to survive --rebuild alongside ~/.claude/.credentials.json.
[[ -f "$CLAUDE_JSON_FILE" ]] || echo '{}' > "$CLAUDE_JSON_FILE"

# ── sync skills from upstream repo ────────────────────────────────────────────
# Pulls skills/ from the upstream repo and drops each skill directory into the
# shared ~/.claude/skills mount. Existing skills with the same name are
# replaced wholesale; skills not present upstream are left untouched.
SKILLS_ARCHIVE_URL="${CLAUDE_SKILLS_ARCHIVE_URL:-https://github.com/aryehj/start-claude/archive/refs/heads/main.tar.gz}"
SKILLS_DEST="$CLAUDE_CONFIG_DIR/skills"
echo "==> Syncing skills from upstream into $SKILLS_DEST"
mkdir -p "$SKILLS_DEST"
SKILLS_TMP=$(mktemp -d)
if curl -fsSL "$SKILLS_ARCHIVE_URL" -o "$SKILLS_TMP/archive.tar.gz" \
   && tar -xzf "$SKILLS_TMP/archive.tar.gz" -C "$SKILLS_TMP"; then
  SKILLS_SRC=("$SKILLS_TMP"/*/skills)
  SKILLS_SRC="${SKILLS_SRC[0]}"
  if [[ -d "$SKILLS_SRC" ]]; then
    for skill_path in "$SKILLS_SRC"/*/; do
      [[ -d "$skill_path" ]] || continue
      skill_name=$(basename "$skill_path")
      rm -rf "$SKILLS_DEST/$skill_name"
      cp -R "$skill_path" "$SKILLS_DEST/$skill_name"
      echo "    injected skill: $skill_name"
    done
  else
    echo "    warning: no skills/ directory found in upstream archive" >&2
  fi
else
  echo "    warning: failed to fetch skills from $SKILLS_ARCHIVE_URL (continuing)" >&2
fi
rm -rf "$SKILLS_TMP"

container run \
  --name "$CONTAINER_NAME" \
  -it \
  -m "$CONTAINER_MEMORY" \
  -c "$CONTAINER_CPUS" \
  -v "$PROJECT_DIR:$PROJECT_DIR" \
  -v "$CLAUDE_CONFIG_DIR:/root/.claude" \
  -v "$CLAUDE_JSON_FILE:/root/.claude.json" \
  -w "$PROJECT_DIR" \
  "${TERM_ARGS[@]}" \
  "$IMAGE_TAG" \
  bash
