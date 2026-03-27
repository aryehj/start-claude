# CLAUDE.md

This repo contains tooling for spinning up isolated Claude Code dev containers
using Apple Containers. One script, one container per project.

## Layout

```
start-claude.sh  — main script; sets up image, creates and attaches container
ROADMAP.md       — planned work
README.md        — usage reference
CLAUDE.md        — this file
```

## What the script does

`start-claude.sh` sets up a `claude-dev:latest` image on first run (cached after
that), then creates and attaches a named container with:

- The project directory mounted at its host path (not `/workspace`)
- Node LTS, Claude Code CLI (via official installer), uv/uvx, git, ripgrep, fd, jq
- bubblewrap, socat, libseccomp2/dev, `@anthropic-ai/sandbox-runtime` (Claude Code sandbox dependencies)

It also starts the container service automatically (`container system start`) so
the script works even if the service isn't already running.

If the named container already exists, it just starts and re-attaches it.

## Key decisions

**Single shared image, per-project containers.** The image (`claude-dev:latest`)
is built once and reused. Each project gets its own named container so state
(installed packages, history) is isolated between projects.

**No `container build` — setup runs inline.** `container build` requires a
builder daemon that times out on first use. Instead, the script runs setup
commands inside a temporary `debian:bookworm-slim` container, then exports the
result as `claude-dev:latest` via `container export --image`. Same outcome,
no builder dependency.

**`container system start` is idempotent.** The script always calls it before
any other container operations. It returns immediately if the service is already
running, so there's no need to check status first.

**`container inspect` returns `[]` with exit 0 for missing containers.** The
existence check uses `[[ "$(container inspect ...)" != "[]" ]]` rather than
checking the exit code.

**Claude Code installer binary is symlinked into `/usr/local/bin`.** The official installer places the `claude` binary in `~/.local/bin`, which is not in the default PATH. The setup script symlinks it into `/usr/local/bin` (`ln -sf /root/.local/bin/claude /usr/local/bin/claude`) so `claude` is available regardless of shell login mode. `PATH` is also exported before the installer runs to suppress its "not in PATH" warning. `~/.local/bin` is also added to `PATH` in `/root/.bashrc` so the claude binary itself doesn't warn at startup that its install location isn't in PATH. `uv` avoids this entirely by using `UV_INSTALL_DIR=/usr/local/bin`.

**`TERM`, `COLORTERM`, and `TERM_PROGRAM` are forwarded into the container.** Without these, Claude Code falls back to a lower color mode (16 or 256 colors) and renders very differently from the host. Both `container run` (new container) and `container exec` (re-attach) pass them via `TERM_ARGS`.

**Auth requires one-time `claude login` per new container.** Claude Code stores
OAuth credentials in the macOS Keychain, which is not accessible inside the
container. Run `claude login` once inside a new container; re-attaching to the
same container retains the session.

## Making changes

The setup script is embedded as a `bash -c '...'` heredoc inside
`start-claude.sh`. Edit it there. After changing it, run with `--rebuild` to
apply the changes:

```bash
start-claude.sh --rebuild
```

This removes the existing project container (if any) and the `claude-dev:latest`
image, then rebuilds from scratch.
