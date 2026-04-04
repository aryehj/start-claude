# CLAUDE.md

This repo contains tooling for spinning up isolated Claude Code dev containers
using Apple Containers. One script, one container per project.

## Layout

```
start-claude.sh  — main script; sets up image, creates and attaches container
skills/          — reusable Claude Code skills (back up of ~/.claude/skills/)
plans/           — implementation plans written by /plan skill
ROADMAP.md       — planned work
README.md        — usage reference
ADR.md           — architecture decision records
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

**Setup runs inline, image built via `container build`.** The script runs setup
commands inside a temporary `debian:bookworm-slim` container, exports the
filesystem with `container export --output`, then builds `claude-dev:latest`
using `container build` with a `FROM scratch + ADD rootfs.tar` Dockerfile. The
builder daemon is started automatically if not running. This replaced the old
`container export --image` flag which was removed in v0.11.0.

**`container system start` is idempotent.** The script always calls it before
any other container operations. It returns immediately if the service is already
running, so there's no need to check status first.

**`container inspect` returns `[]` with exit 0 for missing containers.** The
existence check uses `[[ "$(container inspect ...)" != "[]" ]]` rather than
checking the exit code.

**Claude Code installer binary is symlinked into `/usr/local/bin`.** The official installer places the `claude` binary in `~/.local/bin`, which is not in the default PATH. The setup script symlinks it into `/usr/local/bin` (`ln -sf /root/.local/bin/claude /usr/local/bin/claude`) so `claude` is available regardless of shell login mode. `PATH` is also exported before the installer runs to suppress its "not in PATH" warning. `~/.local/bin` is also added to `PATH` in `/root/.bashrc` so the claude binary itself doesn't warn at startup that its install location isn't in PATH. `uv` avoids this entirely by using `UV_INSTALL_DIR=/usr/local/bin`.

**`UV_CACHE_DIR` resolves dynamically to `${TMPDIR:-/tmp}/uv-cache`.**
Claude Code's sandbox makes `/root/.cache` read-only, which breaks UV's default
cache path. The sandbox also mounts `/tmp` read-only at the bubblewrap level, so
hardcoding `/tmp/uv-cache` fails even when it's in the sandbox allowlist.
Instead, `UV_CACHE_DIR` is set in `.bashrc` and `/etc/profile.d/` to
`${TMPDIR:-/tmp}/uv-cache`, which resolves at shell startup to the
sandbox-provided writable temp directory (the sandbox sets `$TMPDIR`
automatically). Both `/tmp/uv-cache` and `$TMPDIR/uv-cache` are in the sandbox
`filesystem.allowWrite` list as a belt-and-suspenders measure. This also fixes
`uv run --with` which creates temporary virtual environments in `$TMPDIR`.
See ADR-001 in `ADR.md`.

**`TERM`, `COLORTERM`, and `TERM_PROGRAM` are forwarded into the container.** Without these, Claude Code falls back to a lower color mode (16 or 256 colors) and renders very differently from the host. Both `container run` (new container) and `container exec` (re-attach) pass them via `TERM_ARGS`.

**`~/.claude` is shared across all containers via a host volume mount.**
`~/.claude-containers/shared/` on the host is mounted to `/root/.claude` inside
every container. This persists auth credentials (`.credentials.json`), memory,
and user settings across container restarts and across projects. `claude login`
only needs to be run once; all containers share the session.

**`/root/.claude.json` is also persisted, as a file bind-mount.** Claude Code
stores `oauthAccount` and related auth state in the top-level `~/.claude.json`,
not just in `~/.claude/.credentials.json` — so losing it forces a re-login even
when `.credentials.json` survives. The script creates
`~/.claude-containers/claude.json` on the host (initialized to `{}` if absent)
and mounts it to `/root/.claude.json` alongside the `~/.claude/` directory
mount. This way both halves of Claude Code's auth state survive `--rebuild`.

**Skills are synced from the upstream repo on every new-container build.**
Right before `container run`, the script downloads the repo tarball from
`$CLAUDE_SKILLS_ARCHIVE_URL` (default: the `main` branch of
`aryehj/start-claude`), extracts it, and for each directory under the archive's
`skills/` folder, removes the matching directory under
`~/.claude-containers/shared/skills/` and copies the upstream version in its
place. Skills present locally but absent upstream are left untouched — the
clobber is per-skill-directory, not a wholesale wipe. Fetch failures warn but
do not abort container creation. This path only runs when a new container is
being created; re-attach to an existing container skips the sync. See ADR-005.

**Theme is set at the project level, not globally.** The light theme is
configured in each project's `.claude/settings.local.json` rather than in the
global `~/.claude.json`. This avoids needing to persist or merge `.claude.json`
across container lifecycles. The migration block in the settings injection
section adds `"theme": "light"` to existing settings files that lack it.

## Making changes

The setup script is embedded as a `bash -c '...'` heredoc inside
`start-claude.sh`. Edit it there. After changing it, run with `--rebuild` to
apply the changes:

```bash
start-claude.sh --rebuild
```

This removes the existing project container (if any) and the `claude-dev:latest`
image, then rebuilds from scratch.
