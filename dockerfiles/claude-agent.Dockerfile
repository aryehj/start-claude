# syntax=docker/dockerfile:1.6
#
# claude-agent base image — Debian bookworm with Claude Code + OpenCode CLIs,
# Node LTS, uv, git, ripgrep, fd, jq.
# Built by start-agent.sh into Colima's docker runtime as claude-agent:latest.
#
# Unlike start-claude.sh, this image omits the Claude Code bubblewrap sandbox
# dependencies (bubblewrap, socat, libseccomp2, @anthropic-ai/sandbox-runtime).
# The sandbox cannot run inside an unprivileged Docker container — making it
# work would require CAP_SYS_ADMIN, which would weaken the Colima VM boundary
# that is already the real isolation layer here. start-agent.sh force-disables
# sandbox.enabled in project settings.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin"

# ── system packages ──────────────────────────────────────────────────────────
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      bash curl wget git ca-certificates \
      python3 \
      jq ripgrep fd-find \
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

# ── Claude Code CLI ──────────────────────────────────────────────────────────
# The official installer drops the binary in ~/.local/bin; symlink into
# /usr/local/bin so it's reachable regardless of login-shell mode. Also append
# ~/.local/bin to PATH in .bashrc so the binary itself does not warn at startup.
RUN curl -fsSL https://claude.ai/install.sh | bash \
 && ln -sf /root/.local/bin/claude /usr/local/bin/claude \
 && echo 'export PATH="$HOME/.local/bin:$PATH"' >> /root/.bashrc

# ── OpenCode CLI ─────────────────────────────────────────────────────────────
# Installed via the official npm package (opencode-ai). Keeps the install in
# lockstep with the npm version the user would get on the host and avoids a
# separate curl-pipe installer. The binary is named `opencode`.
RUN npm install -g opencode-ai@latest

# ── Pi CLI ───────────────────────────────────────────────────────────────────
# Pi (pi.dev) is a Node-based agentic coding CLI from the pi-mono repo.
# Installed via the canonical npm package (@earendil-works/pi-coding-agent).
# The binary is named `pi`. Requires Node >=22.19.0; setup_lts.x installs
# Node 22.x LTS which satisfies this floor.
RUN npm install -g @earendil-works/pi-coding-agent@latest

# ── SearXNG MCP shim ─────────────────────────────────────────────────────────
# ~40-line FastMCP wrapper that exposes a single `websearch` tool backed by a
# local SearXNG instance. Installed to /opt/searxng-mcp/server.py so opencode
# can spawn it as a stdio MCP server when --enable-local-search is active.
# Custom shim used instead of ihor-sokoliuk/mcp-searxng (npm) because the npm
# package bundles a `web_url_read` tool that bypasses opencode's webfetch
# permission model. See ADR-014.
COPY searxng-mcp/server.py /opt/searxng-mcp/server.py
# Dedicated venv — Debian bookworm's system Python is PEP 668 externally
# managed, so `uv pip install --system` hard-fails. The venv is baked into the
# image and invoked directly (see start-agent.sh command wiring).
RUN uv venv /opt/searxng-mcp/venv \
 && uv pip install --python /opt/searxng-mcp/venv/bin/python 'mcp[cli]' httpx

# ── UV project venv redirect (dynamic $TMPDIR) ───────────────────────────────
# Redirect venvs out of the bind-mounted project dir, which may carry a
# macOS-binary .venv that won't run on Linux (ADR-007). Resolved at shell
# startup rather than baked in. UV_CACHE_DIR is not redirected here — unlike
# start-claude.sh, no read-only /root/.cache mount is in play (ADR-001 does
# not apply without the bubblewrap sandbox).
RUN cat > /etc/profile.d/uv-cache.sh <<'UVEOF'
export UV_PROJECT_ENVIRONMENT="${TMPDIR:-/tmp}/.venv"
mkdir -p "$UV_PROJECT_ENVIRONMENT" 2>/dev/null || true
UVEOF

RUN cat >> /root/.bashrc <<'UVEOF'
export UV_PROJECT_ENVIRONMENT="${TMPDIR:-/tmp}/.venv"
mkdir -p "$UV_PROJECT_ENVIRONMENT" 2>/dev/null || true
UVEOF

# ── allowlist mount point ────────────────────────────────────────────────────
# start-agent.sh bind-mounts the allowlist here :ro so the agent can read but
# not rewrite which URLs are permitted.
RUN mkdir -p /etc/claude-agent

# ── git identity placeholders ────────────────────────────────────────────────
# Real values are injected at `docker run` time via GIT_AUTHOR_* / GIT_COMMITTER_*
# env vars (see start-agent.sh). The gitconfig lines below exist so direct git
# usage works outside any sandbox that might not expose the env vars.
RUN git config --global user.name  "Dev" \
 && git config --global user.email "dev@localhost"

WORKDIR /root
CMD ["/bin/bash"]
