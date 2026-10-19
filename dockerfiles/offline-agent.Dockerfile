# syntax=docker/dockerfile:1.6
#
# offline-agent base image — Debian bookworm with OpenCode + Pi CLIs, Node LTS,
# uv, git, ripgrep, fd, jq, unzip/zip, rsync (offline-agent.py's export helpers).
# Built by offline-agent.py into the offline-agent Colima VM as
# offline-agent:latest.
#
# The container has no egress except the local inference server, so the image
# is the only way to add tools: edit the user-packages block at the bottom and
# run `offline-agent.py --rebuild`. Unlike claude-agent.Dockerfile, there is no
# Claude Code (it needs the Anthropic API), no LibreOffice/doc-tools, and no
# SearXNG shim.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin"

# ── system packages ──────────────────────────────────────────────────────────
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      bash curl wget git ca-certificates \
      python3 \
      jq ripgrep fd-find \
      unzip zip \
      rsync \
 && apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/*

# ── Node.js (LTS) + global npm packages ──────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
 && apt-get install -y nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g npm@latest

# ── uv ───────────────────────────────────────────────────────────────────────
# UV_INSTALL_DIR puts binaries directly in /usr/local/bin, so no PATH fixup and
# no "add to PATH" warning.
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

# ── OpenCode + Pi CLIs ───────────────────────────────────────────────────────
# Both shipped as canonical npm packages: opencode-ai (binary `opencode`) and
# @earendil-works/pi-coding-agent (binary `pi`, requires Node >=22.19.0 which
# setup_lts.x satisfies). Installed in one layer so npm bootstraps once.
RUN npm install -g --allow-scripts=opencode-ai,@google/genai,protobufjs opencode-ai@latest @earendil-works/pi-coding-agent@latest

# ── OpenCode offline switches ────────────────────────────────────────────────
# `autoupdate: false` in opencode.json is not enough: Phase 1 of
# plans/offline-agent.md saw opencode still try registry.npmjs.org and
# models.opencode.ai. These four together left only the inference endpoint.
# Pi needed no equivalent.
ENV OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1 \
    OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    OPENCODE_DISABLE_DEFAULT_PLUGINS=1

# ── UV project venv redirect (dynamic $TMPDIR) ───────────────────────────────
# Redirect venvs out of the project dir, which may carry a macOS-binary .venv
# that won't run on Linux (ADR-007). Resolved at shell startup rather than
# baked in.
RUN cat > /etc/profile.d/uv-cache.sh <<'UVEOF'
export UV_PROJECT_ENVIRONMENT="${TMPDIR:-/tmp}/.venv"
mkdir -p "$UV_PROJECT_ENVIRONMENT" 2>/dev/null || true
UVEOF

RUN cat >> /root/.bashrc <<'UVEOF'
export UV_PROJECT_ENVIRONMENT="${TMPDIR:-/tmp}/.venv"
mkdir -p "$UV_PROJECT_ENVIRONMENT" 2>/dev/null || true
UVEOF

# ── git ──────────────────────────────────────────────────────────────────────
# The project is a virtiofs lower layer whose files show as owned by 0:0 in
# the container, which git treats as dubious ownership; trust every directory.
# Real identity is injected at `docker run` time via GIT_AUTHOR_* /
# GIT_COMMITTER_* env vars (see offline-agent.py); these are fallbacks.
RUN git config --system --add safe.directory '*' \
 && git config --global user.name  "Dev" \
 && git config --global user.email "dev@localhost"

# ── user packages ────────────────────────────────────────────────────────────
# Add your own tools here, then run `offline-agent.py --rebuild`. Keep
# additions in this block: every layer above stays a cache hit, so a rebuild
# costs about as much as the new packages. Editing anything above this block
# rebuilds from that point down. Example:
#   RUN apt-get update -qq \
#    && apt-get install -y --no-install-recommends shellcheck \
#    && rm -rf /var/lib/apt/lists/*
RUN true

WORKDIR /root
CMD ["/bin/bash"]
