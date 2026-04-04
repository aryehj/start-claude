# Architecture Decision Records

## ADR-001: Set `UV_CACHE_DIR` to a writable path in the container environment

**Date:** 2026-04-03
**Status:** Accepted

### Context

Inside the container, Claude Code's sandbox restricts `/root/.cache` to
read-only. UV defaults to `/root/.cache/uv` for its cache directory. Every
`uv run` invocation fails with:

```
error: Failed to initialize cache at `/root/.cache/uv`
  Caused by: failed to create directory `/root/.cache/uv`: Read-only file system (os error 30)
```

Claude Code would then spend multiple tool calls trying to work around this
(prefixing `UV_CACHE_DIR=/tmp/...` on individual commands), burning context and
often still failing when it forgot the prefix.

### Decision

Set `UV_CACHE_DIR=/tmp/uv-cache` at multiple levels:

1. **`TERM_ARGS` (`-e UV_CACHE_DIR=/tmp/uv-cache`)** — passed to both
   `container run` and `container exec`. This is the critical path: Claude Code
   inherits the container's environment, and all `bash -c` subprocesses it spawns
   inherit it in turn.
2. **`/root/.bashrc`** — interactive login shells inside the container.
3. **`/etc/environment`** — PAM-based login sessions.
4. **`/etc/profile.d/uv-cache.sh`** — login shells sourcing profile.d.

Layer 1 is sufficient for Claude Code's sandbox. Layers 2-4 are
belt-and-suspenders for manual shell sessions.

### Consequences

- `uv run`, `uv pip`, and `uvx` work out of the box inside the sandbox with no
  manual `UV_CACHE_DIR` prefix.
- The cache lives under `/tmp/uv-cache`, which is writable but ephemeral — it
  does not survive container restarts. This is acceptable because UV
  re-downloads packages quickly and the cache is not large.
- Requires `--rebuild` to take effect on existing containers.

## ADR-002: Add `/tmp/uv-cache` to sandbox `filesystem.allowWrite`

**Date:** 2026-04-03
**Status:** Accepted

### Context

ADR-001 set `UV_CACHE_DIR=/tmp/uv-cache` as an environment variable, but this
alone was insufficient. Claude Code's bubblewrap sandbox mounts most of `/tmp`
as read-only. The env var told uv *where* to write, but the sandbox prevented
the actual writes:

```
error: Failed to initialize cache at `/tmp/uv-cache`
  Caused by: failed to create directory `/tmp/uv-cache`: Read-only file system (os error 30)
```

Setting `$TMPDIR` (`/tmp/claude`) also failed — the sandbox restricts that path
for its own use.

### Decision

Add `/tmp/uv-cache` to `sandbox.filesystem.allowWrite` in each project's
`.claude/settings.local.json`. The script now:

1. **New projects:** The generated `settings.local.json` includes the
   `filesystem.allowWrite` array with `/tmp/uv-cache`.
2. **Existing projects:** The migration block in `start-claude.sh` checks for
   the presence of `/tmp/uv-cache` in `allowWrite` and adds it if missing,
   alongside the existing boolean→object migration.

### Consequences

- `uv` commands work inside the sandbox without any manual workarounds.
- The env var (`UV_CACHE_DIR`) and sandbox permission (`filesystem.allowWrite`)
  are now kept in sync by the same script.
- Existing containers need their `settings.local.json` updated (happens
  automatically on next `start-claude.sh` run) but do not require `--rebuild`.

## ADR-004: Resolve `UV_CACHE_DIR` dynamically via `$TMPDIR`

**Date:** 2026-04-04
**Status:** Accepted (supersedes parts of ADR-001 and ADR-002)

### Context

ADR-001 hardcoded `UV_CACHE_DIR=/tmp/uv-cache` and passed it as a static env var
via `TERM_ARGS`. ADR-002 added `/tmp/uv-cache` to the sandbox `allowWrite` list.
In practice, Claude Code's bubblewrap sandbox mounts `/tmp` read-only at the
filesystem level before the allowlist is evaluated, so writes to `/tmp/uv-cache`
still failed. Users reported that `uv run --with` (which creates temporary
virtual environments) also failed for the same reason.

The sandbox sets `$TMPDIR` to a guaranteed-writable directory at runtime. Using
this instead of hardcoding `/tmp` resolves both issues.

### Decision

1. **Remove `UV_CACHE_DIR` from `TERM_ARGS`.** No longer pass it as a static
   container env var.
2. **Set `UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"` in `.bashrc` and
   `/etc/profile.d/uv-cache.sh`.** The variable resolves at shell startup to the
   sandbox-provided writable temp directory. Falls back to `/tmp` for interactive
   use outside the sandbox.
3. **Drop `/etc/environment`.** It does not support shell variable expansion, so
   it cannot use `$TMPDIR`.
4. **Add `$TMPDIR/uv-cache` to sandbox `filesystem.allowWrite`** alongside the
   existing `/tmp/uv-cache` entry, as a belt-and-suspenders measure.
5. **`mkdir -p` on every shell startup.** Each sandbox session may get a fresh
   `$TMPDIR`, so the `uv-cache` subdirectory is created at profile load time.

### Consequences

- `uv run`, `uv pip`, `uvx`, and `uv run --with` all work inside the sandbox
  without manual workarounds.
- The cache is ephemeral per sandbox session (each may use a different `$TMPDIR`),
  which is acceptable — UV re-downloads quickly.
- Requires `--rebuild` to bake the new profile scripts into the image. Existing
  containers with the old static env var will continue to use `/tmp/uv-cache`
  (unchanged behavior) until rebuilt.

## ADR-003: Set theme in project-level settings, not global `.claude.json`

**Date:** 2026-04-03
**Status:** Accepted

### Context

The script previously set `"theme": "light"` by overwriting `/root/.claude.json`
on every `container run`:

```bash
bash -c 'echo "{\"theme\":\"light\"}" > /root/.claude.json && exec bash'
```

`/root/.claude.json` is a top-level file, separate from the `/root/.claude/`
directory that is volume-mounted for credential persistence. Because `.claude.json`
was not persisted, it was ephemeral — but it also meant the overwrite destroyed
any auth-related state that Claude Code wrote there (e.g. `oauthAccount`,
`hasCompletedOnboarding`), potentially contributing to re-login prompts on
container recreation.

Mounting `.claude.json` as a second volume was considered but adds complexity
(seeding the file, merging settings across versions).

### Decision

Move the theme setting to the project-level `settings.local.json`, which the
script already creates and migrates. The `container run` entrypoint is now plain
`bash` with no file writes.

- New projects get `"theme": "light"` in the generated `settings.local.json`.
- Existing projects get it added by the migration block (same pattern as the
  sandbox and `allowWrite` migrations).

### Consequences

- `.claude.json` is no longer touched by the script. Claude Code manages it
  internally; its contents are ephemeral to each container but no longer
  clobbered on creation.
- Theme is project-scoped, which is how `settings.local.json` is designed to
  work. Different projects could theoretically use different themes.
- Auth persistence depends solely on the `~/.claude/` volume mount (which
  contains `.credentials.json`). If `.claude.json` turns out to hold state
  required for session continuity, a separate volume mount would need to be
  added. *(This caveat materialised — see ADR-006.)*

## ADR-005: Sync skills from upstream repo on new container build

**Date:** 2026-04-04
**Status:** Accepted

### Context

The `skills/` directory in this repo is the authoritative copy of the Claude
Code skills we want available in every container. Previously, getting them
into `~/.claude/skills/` was manual: the README instructed users to `cp -r`
or symlink each skill into place. Skills drift quickly, and anyone using an
older copy of the shared `.claude` mount had no way of knowing their skills
were stale.

The shared mount (`~/.claude-containers/shared/` → `/root/.claude`) already
centralises skill storage across containers, so pushing updated skills into
it once per container build propagates to every project.

### Decision

Right before `container run` creates a new container, the script downloads
the upstream repo tarball (default: `main` branch of `aryehj/start-claude`,
overridable via `CLAUDE_SKILLS_ARCHIVE_URL`), extracts it, and for each
directory under the archive's `skills/`:

1. `rm -rf` the matching directory in `~/.claude-containers/shared/skills/`
2. `cp -R` the upstream version in its place

Skills that exist locally but are absent upstream are left untouched. Fetch
or extraction failures produce a warning and container creation proceeds.

### Consequences

- Every new container (including `--rebuild`, which deletes the existing
  container first) picks up the latest upstream skills automatically.
- Users can iterate on a skill locally (e.g. edit files under
  `~/.claude-containers/shared/skills/plan/`) only until the next container
  build clobbers it — local-only edits to synced skills are disposable. Users
  who want durable customisations should fork the repo and point
  `CLAUDE_SKILLS_ARCHIVE_URL` at their fork, or name their skill differently
  so it doesn't collide with an upstream name.
- The sync requires network access at container-creation time. Offline
  builds still work but skip the sync with a warning.
- Re-attaching to an existing container does not trigger a sync, matching
  the existing invariant that image/container state only changes on new
  builds.

## ADR-006: Persist `/root/.claude.json` via a file bind-mount

**Date:** 2026-04-04
**Status:** Accepted (addresses caveat in ADR-003)

### Context

ADR-003 moved the theme setting out of `.claude.json` so the script stopped
clobbering that file, but left its persistence unaddressed — the file was
ephemeral per container. This turned out to break authentication across
`--rebuild`: Claude Code stores `oauthAccount` and other auth state in
`~/.claude.json`, not just in `~/.claude/.credentials.json`. When `--rebuild`
destroyed the container, the fresh container had `.credentials.json` mounted
in (via the `~/.claude/` volume) but no `.claude.json`, and Claude Code
prompted for re-login every time.

The user confirmed they are container-only (no host Claude install), so
separation between host and container `.claude.json` state is not needed.

### Decision

Add a second bind mount: the host file `~/.claude-containers/claude.json`
maps to `/root/.claude.json` in the container. The script creates and
initializes the file to `{}` on the host if it does not already exist, so the
bind mount resolves to a file rather than an auto-created directory.

The file sits alongside (not inside) `~/.claude-containers/shared/`, because
`shared/` is already mounted at `/root/.claude`, and nesting `claude.json`
inside it would expose it at the wrong path (`/root/.claude/claude.json`).

### Consequences

- `claude login` now survives `--rebuild`. Both halves of Claude Code's auth
  state (`~/.claude/.credentials.json` and `~/.claude.json`) are persisted on
  the host.
- All containers share the same `.claude.json`, matching the existing
  "shared across containers" model for `~/.claude/`. Per-project config
  still lives in each project's `.claude/settings.local.json`, unaffected.
- Relies on Apple Containers supporting file-level bind mounts
  (`-v host_file:container_file`). If a future version drops that support,
  fall back to mounting the parent directory or seeding `.claude.json` into
  `~/.claude-containers/shared/` with a symlink.
- To reset auth state: delete `~/.claude-containers/claude.json` and
  `~/.claude-containers/shared/.credentials.json`, then re-run
  `claude login`.
