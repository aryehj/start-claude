# Architecture Decision Records

## ADR-001: Set `UV_CACHE_DIR` to a writable path in the container environment

**Date:** 2026-04-03
**Status:** Superseded in part by ADR-004 — dynamic `$TMPDIR` resolution replaced the static `/tmp/uv-cache` path.

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

1. **`CONTAINER_ENV` (`-e UV_CACHE_DIR=/tmp/uv-cache`)** — passed to both
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
**Status:** Superseded in part by ADR-004 — dynamic `$TMPDIR` resolution replaced the static `/tmp/uv-cache` path.

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
via `CONTAINER_ENV`. ADR-002 added `/tmp/uv-cache` to the sandbox `allowWrite` list.
In practice, Claude Code's bubblewrap sandbox mounts `/tmp` read-only at the
filesystem level before the allowlist is evaluated, so writes to `/tmp/uv-cache`
still failed. Users reported that `uv run --with` (which creates temporary
virtual environments) also failed for the same reason.

The sandbox sets `$TMPDIR` to a guaranteed-writable directory at runtime. Using
this instead of hardcoding `/tmp` resolves both issues.

### Decision

1. **Remove `UV_CACHE_DIR` from `CONTAINER_ENV`.** No longer pass it as a static
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
**Status:** Superseded (theme injection removed 2026-05-12)

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

### Decision (original)

Move the theme setting to the project-level `settings.local.json`, which the
script already creates and migrates. The `container run` entrypoint is now plain
`bash` with no file writes.

- New projects get `"theme": "light"` in the generated `settings.local.json`.
- Existing projects get it added by the migration block (same pattern as the
  sandbox and `allowWrite` migrations).

### Supersession

The theme injection was removed entirely. Claude Code prompts the user on first
run and persists the choice to `~/.claude.json`, which is already shared across
containers via the bind-mount from ADR-006. Injecting a fixed theme is
unnecessary and prevents users from choosing their own.

### Consequences

- `.claude.json` is no longer touched by the script. Claude Code manages it
  internally; its contents are ephemeral to each container but no longer
  clobbered on creation.
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

## ADR-007: Redirect UV project venv via `UV_PROJECT_ENVIRONMENT`

**Date:** 2026-04-07
**Status:** Accepted

### Context

Projects mounted into containers often have a `.venv` directory created on the
host (macOS/ARM). The Claude Code agent inside the Linux container sees this
`.venv`, attempts to use it, and fails — the binaries are wrong platform, paths
are wrong, and the sandbox may block writes. The agent then wastes cycles trying
workarounds (manual `--python` flags, prefixing env vars on individual commands,
or trying to `rm -rf` and recreate the venv in place).

This is a well-known problem in any "mount host project into dev container"
workflow. UV provides `UV_PROJECT_ENVIRONMENT` for exactly this use case: it
overrides where `uv sync`, `uv run`, etc. create the project virtual
environment (default: `.venv` in the project root).

### Decision

1. **Set `UV_PROJECT_ENVIRONMENT="${TMPDIR:-/tmp}/.venv"` in `.bashrc` and
   `/etc/profile.d/uv-cache.sh`.** Same dynamic `$TMPDIR` resolution pattern
   as `UV_CACHE_DIR` (ADR-004). The variable resolves at shell startup to the
   sandbox-provided writable temp directory.
2. **`mkdir -p` on every shell startup** to ensure the directory exists (same
   pattern as `UV_CACHE_DIR`).
3. **Add `/tmp/.venv` and `$TMPDIR/.venv` to `sandbox.filesystem.allowWrite`**
   in each project's `.claude/settings.local.json`, alongside the existing UV
   cache entries. Both the default settings block and the migration block are
   updated.

UV intentionally keeps `UV_PROJECT_ENVIRONMENT` as a user-level environment
variable rather than a `pyproject.toml` / `uv.toml` setting — a project should
not be able to direct installs to arbitrary paths on the user's machine. This
makes the env-var approach the canonical solution.

### Consequences

- `uv sync`, `uv run`, `uv add`, etc. create and use a Linux venv in `$TMPDIR/.venv`
  instead of the project's `.venv`. The host `.venv` is completely ignored, not
  deleted.
- The venv is ephemeral per sandbox session (each may use a different `$TMPDIR`),
  so `uv sync` must run once per session. This is acceptable — UV installs from
  cache quickly, and the cache itself persists across commands within a session.
- Requires `--rebuild` to bake the new profile scripts into the image.
- Existing project `settings.local.json` files pick up the new `allowWrite`
  entries automatically on next `start-claude.sh` run (no rebuild needed for
  the sandbox permissions, only for the env var).

## ADR-010: start-agent: in-VM tinyproxy + DOCKER-USER iptables for egress allowlist

**Date:** 2026-04-15
**Status:** Accepted

### Context

`start-agent.sh` (sibling to `start-claude.sh`) runs both Claude Code and
OpenCode in a shared Colima-hosted docker container, and needs a network
egress allowlist that the in-container LLM cannot modify. Several placements
were possible:

1. **Host-side pf + host-side proxy.** Strongest isolation — the rules live
   two VM boundaries away from the container. But pfctl anchors, launchd
   plists for the proxy, vmnet CIDR discovery, and `sudo` for every reload
   all bleed setup pain into the user's macOS install.
2. **In-container iptables + in-container proxy.** Trivially simple, but
   trivially bypassable: the LLM is `root` inside the container by default,
   and a single `iptables -F` (or editing the proxy config) undoes it.
3. **In-VM iptables (DOCKER-USER) + in-VM tinyproxy.** Rules live outside
   the container's network namespace, inside a Lima VM the container cannot
   reach without a container escape plus VM root — two jumps outside any
   realistic behavioral threat model, with none of the host-side setup
   overhead of option 1.

### Decision

Option 3.

- **tinyproxy** runs as a systemd service inside the Colima VM, bound to the
  docker bridge gateway IP (discovered at script time via
  `docker network inspect bridge`). Config: `FilterDefaultDeny Yes`,
  `FilterExtended Yes`, `FilterURLs No`, and a `Filter` file of anchored
  regexes (`(^|\.)domain\.tld$`) generated on the host from
  `~/.claude-agent/allowlist.txt`. The filter applies to the hostname
  portion of HTTPS `CONNECT` requests as well as plain HTTP.
- **iptables** rules live in a dedicated `CLAUDE_AGENT` chain. `DOCKER-USER`
  (which the docker daemon explicitly leaves alone on restart) carries a
  single `-s $BRIDGE_CIDR -j CLAUDE_AGENT` jump. The child chain holds the
  RETURN rules (established/related, proxy port, host inference port,
  bridge DNS) and a catch-all REJECT. Re-applying is atomic: `iptables -F
  CLAUDE_AGENT` wipes prior state, then `-A` repopulates in order. No
  comment-tag matching, no rule-number arithmetic, no risk of leaving
  stale rules co-resident with new ones.
- **Re-apply on every run** instead of persisting. The firewall script is
  regenerated on the host and streamed into `sudo sh` over `colima ssh` on
  every invocation. No state drift between "rules the host script thinks
  are active" and "rules actually active after a VM reboot." The cost is a
  ~1s colima-ssh round trip at the start of every launch.
- **Allowlist lives at `~/.claude-agent/allowlist.txt`** on the macOS host,
  plain text, one domain per line, suffix matching. `--reload-allowlist`
  regenerates the filter and reloads tinyproxy (SIGHUP) without touching
  the running container — a ~2s fast path.

### Consequences

- The LLM inside the container has no write path to the iptables rules or
  the allowlist. Modifying either requires a container escape plus VM root.
- All outbound traffic except the established-return path, the proxy port,
  the Ollama port, and DNS to the bridge gateway is REJECTed at the VM
  level. Applications inside the container that don't honor `HTTPS_PROXY` /
  `HTTP_PROXY` will simply fail to reach anything — an acceptable price,
  and arguably a feature (no silent bypass). *(Node.js apps — Claude Code
  and OpenCode — are handled by ADR-013's `global-agent` bootstrap.)*
- Every invocation pays a small latency tax (applying firewall rules), in
  exchange for zero configuration drift.
- The seed allowlist is intentionally permissive for development and
  research workflows; users are expected to prune it.
- Per-rule granularity is coarse (hostname match only). If the allowlist
  grows past ~200 entries or needs per-path / per-method semantics, squid
  with `acl ... dstdomain` is the conventional upgrade.

## ADR-009: Set git identity via environment variables, not just gitconfig

**Date:** 2026-04-08
**Status:** Accepted

### Context

`git config --global` writes to `/root/.gitconfig` during image build. This
works for direct shell usage but fails inside Claude Code's bubblewrap sandbox,
which may not expose `/root/.gitconfig` in its mount namespace. When the sandbox
runs `git commit`, git cannot auto-detect the author identity and the commit
fails with "Author identity unknown".

### Decision

Set git identity in three layers (matching the existing pattern for other env
vars):

1. **`git config --global`** — baked into the image for non-sandboxed git.
2. **`.bashrc` and `/etc/profile.d/git-identity.sh`** — exports
   `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`,
   `GIT_COMMITTER_EMAIL` so interactive shells have them.
3. **`CONTAINER_ENV`** — passes the same four variables as container-level env
   vars so they are inherited from process birth, before shell init.

Git's `GIT_AUTHOR_*` / `GIT_COMMITTER_*` environment variables take precedence
over all config files, so they work regardless of whether the sandbox mounts
`/root/.gitconfig`.

### Consequences

- Git commits work inside the bubblewrap sandbox without needing sandbox config
  changes.
- The env vars override any repo-level `.gitconfig`. This is acceptable because
  the container is single-user and all projects should use the same identity.
- Defaults are `Dev` / `dev@localhost`, overridable at script invocation via
  `--git-name`/`--git-email` CLI flags or `$GIT_USER_NAME`/`$GIT_USER_EMAIL`
  env vars.
- Requires `--rebuild` to bake the new profile scripts into the image.

## ADR-011: start-agent: build the image with `docker build --network=host`

**Date:** 2026-04-15
**Status:** Accepted

### Context

ADR-010's DOCKER-USER egress allowlist rejects every packet from the docker
bridge CIDR that isn't destined for tinyproxy, host Ollama, or bridge DNS.
Runtime containers honor `HTTPS_PROXY` and route their traffic through
tinyproxy, but `docker build` RUN steps also execute inside short-lived
containers attached to the bridge — and those build containers have no proxy
env set. The first install step (`apt-get update && apt-get install ...`) hit
the REJECT rule immediately:

```
W: Failed to fetch http://deb.debian.org/debian/dists/bookworm/InRelease
   Could not connect to deb.debian.org:80 — connect (113: No route to host)
```

Three options to unstick the build:

1. **Route the build through tinyproxy via `--build-arg HTTP_PROXY=...`.**
   Docker treats the proxy vars as predefined build ARGs and exposes them as
   env vars during RUN. In practice this is unreliable on the legacy builder:
   some tools (notably apt) only consult the lowercase `http_proxy`, and
   propagating both cases through every RUN step means either touching the
   Dockerfile or writing an `/etc/apt/apt.conf.d/99proxy` shim.
2. **Temporarily drop the REJECT rule while building, then re-add.** Simple
   in theory, but introduces a timing window where every bridge-attached
   container is unfirewalled, plus a cleanup path that has to survive build
   failures and SIGINT.
3. **`docker build --network=host`.** RUN steps share the Colima VM's host
   network namespace. The VM's netns is not subject to the `FORWARD` chain
   (DOCKER-USER only fires for packets being routed between interfaces), so
   the build has full egress. Runtime containers still attach to the docker
   bridge via `docker run` and remain firewalled normally — the `--network`
   flag applies only to the build.

### Decision

Option 3. `docker build` is invoked with `--network=host` in `start-agent.sh`.

### Consequences

- Build-time network egress is unrestricted. Acceptable because we trust the
  Dockerfile — it's checked into the repo, the image is rebuilt only on
  explicit `--rebuild`, and the LLM running at runtime has no write path to
  the Dockerfile or the build invocation.
- Runtime egress enforcement is unchanged. Containers created by `docker run`
  attach to the bridge and pass through `DOCKER-USER` like before; the six
  smoke tests in `README.md` (default-deny, allowed-via-proxy,
  denied-via-proxy, Ollama carve-out, env sanity, hot-reload) all pass with
  this configuration.
- No Dockerfile changes needed to handle proxies, and no timing window where
  the bridge is unfirewalled. The whole interaction is local to the
  `docker build` line.
- If we ever migrate to BuildKit (the legacy builder is deprecation-warned on
  every run), the same flag works there too — BuildKit accepts
  `--network=host` and honors it identically for RUN steps. No lock-in.

## ADR-012: start-agent: omlx as an alternative local inference backend

**Date:** 2026-04-16
**Status:** Accepted

### Context

`start-agent.sh` hard-coded Ollama as the local inference backend. Ollama
binds to `localhost` by default; reaching it from the Colima VM requires
rebinding to `0.0.0.0`, which exposes the model server to every device on
the user's LAN. The `plans/ollama-host-firewall.md` plan addresses this with
a host-side pf firewall, but that adds operational complexity (anchor files,
LaunchDaemons, interface-scoped rules).

[omlx](https://github.com/jundot/omlx) is an MLX-based inference server for
Apple Silicon with an OpenAI-compatible API and built-in `--api-key` support.
When started with an API key, omlx returns 401/403 to unauthenticated
requests, so it can safely bind to `0.0.0.0` without a host-side firewall —
LAN peers that probe the port get rejected at the application layer.

### Decision

Add omlx as an alternative backend behind `--backend=omlx` (default remains
`ollama`). A single `case "$BACKEND"` dispatch block governs all
backend-specific behavior:

1. **Port.** `INFERENCE_PORT` is 11434 for Ollama, 8000 for omlx. All
   downstream references (iptables carve-out, preflight probe, OpenCode
   config, container env) use `$INFERENCE_PORT`.
2. **API key.** `OMLX_API_KEY` from the host env is passed into the
   container as an env var and written into the OpenCode provider config's
   `apiKey` field. Missing key is a warning, not a hard error (the user may
   run omlx without auth during local testing).
3. **Container env vars.** `OLLAMA_HOST` is only set for the `ollama`
   backend; `OMLX_HOST` and `OMLX_API_KEY` are set for `omlx`. Neither
   backend pollutes the other's namespace.
4. **OpenCode config.** The `ollama` and `omlx` provider entries use
   separate keys in `opencode.json` and coexist across backend switches.
5. **Preflight probe.** Ollama probes `/api/tags`; omlx probes `/v1/models`
   with a Bearer token if the key is set.

### Consequences

- Users on shared networks can use `--backend=omlx` and skip the host-side
  pf firewall entirely. The `plans/ollama-host-firewall.md` plan is
  unnecessary when using omlx.
- No Dockerfile changes. omlx runs on the macOS host; the container reaches
  it via the same iptables carve-out used for Ollama (just a different port).
- Adding future backends (llama.cpp, vLLM, etc.) means adding one more
  `case` branch in the dispatch, one conditional in the env/preflight/config
  blocks, and one doc section. The pattern scales linearly.

## ADR-013: start-agent: bootstrap `global-agent` so Node.js respects the proxy

**Date:** 2026-04-17
**Status:** Accepted

### Context

ADR-010's DOCKER-USER iptables rules reject all outbound traffic from the
container except to tinyproxy, the inference port, and bridge DNS. The
container sets `HTTP_PROXY` / `HTTPS_PROXY` so that proxy-aware tools (curl,
apt, wget) route through tinyproxy, and the allowlist controls which domains
are reachable.

Claude Code and OpenCode are Node.js applications. Node.js's built-in
`http`/`https` modules and the `fetch` implementation (backed by `undici`)
do **not** honor `HTTP_PROXY`/`HTTPS_PROXY` environment variables. When
Claude Code attempted to reach `api.anthropic.com`, the request went direct,
hit the DOCKER-USER REJECT rule, and failed — the agent could not
authenticate.

ADR-010's consequences section noted that "applications inside the container
that don't honor `HTTPS_PROXY` / `HTTP_PROXY` will simply fail to reach
anything." For CLI tools like curl this was fine; for the primary workload
(Claude Code) it was a showstopper.

### Decision

1. **Install `global-agent` globally in the Dockerfile** alongside the other
   npm packages (`npm install -g ... global-agent`). `global-agent` monkey-
   patches Node.js's `http.globalAgent` and `https.globalAgent` at require
   time to route requests through the proxy specified in
   `GLOBAL_AGENT_HTTP_PROXY`.
2. **Set `NODE_OPTIONS=--require global-agent/bootstrap`** as a container
   env var in `DOCKER_ENV_ARGS`. This causes every Node.js process (Claude
   Code, OpenCode, any npm scripts) to load `global-agent` before
   application code runs.
3. **Set `GLOBAL_AGENT_HTTP_PROXY`** to the tinyproxy URL
   (`http://$BRIDGE_IP:$TINYPROXY_PORT`). `global-agent` reads this to
   determine the proxy endpoint.
4. **Set lowercase `http_proxy`/`https_proxy`/`no_proxy`** alongside the
   uppercase variants. Some Node.js libraries (and Python's `urllib`) check
   lowercase; belt-and-suspenders.

### Consequences

- Claude Code and OpenCode API calls now route through tinyproxy and are
  subject to the domain allowlist. Authentication to `api.anthropic.com`
  works because `anthropic.com` is in the default allowlist.
- Every Node.js process in the container pays the `global-agent` require
  cost (~5ms). Negligible compared to network round-trips.
- `NO_PROXY` / `no_proxy` exempt `localhost`, `127.0.0.1`, the bridge IP,
  and the host IP — so local inference traffic (Ollama/omlx) still goes
  direct, as intended.
- `NODE_OPTIONS` is set at container env level (not baked into the image),
  so it applies to both `docker run` (new container) and `docker exec`
  (reattach) paths. The image remains usable without the proxy if someone
  runs it outside the firewalled VM.
- Requires `--rebuild` to install `global-agent` in the image.

### 2026-04-17 revision: replace `global-agent` with `NODE_USE_ENV_PROXY=1`

The original implementation — `npm install -g global-agent` plus
`NODE_OPTIONS=--require global-agent/bootstrap` — had compounding failure
modes:

1. Globally-installed npm packages aren't on Node's default require path
   (needs `NODE_PATH`).
2. `global-agent` v4 dropped the `bootstrap` submodule entry, so even with
   `NODE_PATH` set, `--require global-agent/bootstrap` throws
   `MODULE_NOT_FOUND`.
3. Every Node process in the container consequently crashed before any
   application code ran. Claude Code's Bun-native API client masked the
   breakage; its Node helpers (including the OAuth code-exchange path used
   by `/login`) crashed immediately and surfaced as a misleading
   `OAuth error: Request failed with status code 403`. OpenCode failed to
   launch outright.

Node 24 ships undici with first-class proxy support behind
`NODE_USE_ENV_PROXY=1`. Set that env var alongside `HTTP_PROXY`,
`HTTPS_PROXY`, and `NO_PROXY`; Node's built-in `fetch`, `http`, and
`https` then honor the proxy without any require-time wiring. Removes the
npm package, the `--require` preload, the `NODE_PATH` dependency, the
lowercase proxy-var duplicates, and `GLOBAL_AGENT_HTTP_PROXY` — one
officially-supported flag replaces the whole stack.

## ADR-014: start-agent: SearXNG-backed local websearch

**Date:** 2026-04-21
**Status:** Accepted

### Context

`permission.websearch` in `opencode.json` was hard-set to `"deny"` (ADR-010
era) because the only available backend was the Exa MCP, which routes all
queries through a third-party gateway — defeating the tinyproxy allowlist as
the single egress-control mechanism and leaking query content to Exa. The
plan was to replace it with a local gateway when one became practical.

SearXNG is a privacy-respecting, self-hosted meta-search engine with an
official docker image and a stable JSON API (`/search?format=json`). It is
also the documented search backend for Perplexica, so one SearXNG instance
can serve both the agent's MCP websearch tool and a future Perplexica
deployment on the same bridge.

### Decision

**Why a local search gateway at all.** The allowlist (tinyproxy filter) is
the single source of egress truth for the stack. A third-party search
gateway (Exa, Serper, Brave Search API, etc.) is an unconstrained egress
hole: any query can be routed through it, and the gateway operator sees every
search term. A local meta-search engine that fans out through tinyproxy keeps
all query routing under the allowlist's control.

**Why SearXNG specifically.** It is Perplexica's documented search backend
(pre-requisite for a future Perplexica deployment), ships an official docker
image (`docker.io/searxng/searxng`), exposes a stable JSON API, and supports
`outgoing.proxies` for routing its own fan-out through a proxy. No other
shortlisted option (direct-DDG-scraping MCP servers, Brave Search API shim)
cleanly satisfies both the Perplexica reuse requirement and the
allowlist-as-single-control requirement.

**Why SearXNG on the docker bridge, not host netns.** Running SearXNG's
container with `--network host` would put its fan-out traffic in the VM's
host network namespace, bypassing the `FORWARD` chain and therefore the
`CLAUDE_AGENT` iptables rules entirely. SearXNG would be able to reach any
upstream engine regardless of the allowlist — breaking the single-control
property. Running it as a bridge-attached container means its egress hits the
`DOCKER-USER → CLAUDE_AGENT` chain, and the tinyproxy `RETURN` rule
(`-d $BRIDGE_IP -p tcp --dport 8888`) covers SearXNG's outbound CONNECTs
the same way it covers the agent container's. Two additional iptables rules
are added: one RETURN for bridge→bridge:8080 (claude-agent MCP shim →
SearXNG JSON API) and one implicit coverage via the existing tinyproxy rule
(SearXNG → tinyproxy for fan-out, already allowed source-agnostically).

**Why `outgoing.proxies` in `settings.yml` and NOT `HTTPS_PROXY` env vars.**
Primary-source finding in SearXNG's `searx/network/client.py`: the httpx
client is built with an explicit `transport=SxngAsyncHTTPTransport(...)`.
Per httpx's documented behavior, passing `transport=` overrides the default
transport, which is also what handles env-var proxy pickup
(`AsyncHTTPTransport` reads `HTTPS_PROXY` automatically; a custom transport
does not). Result: `HTTPS_PROXY` set on the SearXNG container has no effect
on its outbound engine requests — they bypass tinyproxy silently. The working
knob is `outgoing.proxies.all//: http://<bridge-ip>:8888` in `settings.yml`,
which SearXNG passes as a `proxy=` argument to its custom transport. A future
contributor who sees the bridge env has `HTTPS_PROXY` set and assumes the
proxy is already wired would be wrong; this ADR names the failure mode so
that assumption isn't made silently.

**Why a custom ~40-line Python FastMCP shim over `ihor-sokoliuk/mcp-searxng`
(npm).** The community npm package is actively maintained and is in the
official `modelcontextprotocol/servers` list, but it bundles a second tool,
`web_url_read`, that fetches and Markdowns arbitrary URLs. That duplicates
opencode's native `webfetch` capability and — critically — sits outside
opencode's `permission.webfetch` control. The MCP-provided `web_url_read`
would bypass opencode's permission model entirely; opencode does not support
per-tool disabling within a given MCP server. The custom shim exposes exactly
one tool (`websearch`) with no URL-fetch side channel. Rejecting the more
popular package on security-posture grounds is the kind of decision that gets
silently reversed during a "let's use the standard package" cleanup; the
decision is documented here so it isn't.

**Why a narrow curated engine set with a matching allowlist (two-lock pair).**
The default SearXNG image enables 50+ engines spanning torrent trackers, art
sites, and social networks — each requiring its own allowlist entry and adding
attack surface for prompt-injection via engine-provided results. The seeded
`settings.yml` uses `use_default_settings.engines.keep_only` to whitelist
nine engines (Google, Bing, DuckDuckGo, Brave, Qwant, Wikipedia, arXiv,
GitHub code search, Stack Exchange) and disables everything else. Enabling a
new engine requires editing both `~/.claude-agent/searxng/settings.yml` AND
`~/.claude-agent/allowlist.txt` — two separate files, intentional friction so
"turn on a new engine" cannot happen via a single-file change.

**`--enable-local-search` is a single flag that gates three components.**
The flag controls (a) SearXNG container lifecycle, (b) the inter-container
iptables RETURN rule, and (c) the `mcp.searxng` block and
`permission.websearch: "allow"` in `opencode.json`. All three are applied or
removed together so the system never enters a state where the MCP config
references a missing container or the firewall blocks a live one.

**User-defined network `claude-agent-net` for inter-container DNS.** Docker's
embedded DNS (container-name resolution) only functions on user-defined
networks, not the default bridge. Both `claude-agent` and `searxng` are
attached to `claude-agent-net` (created idempotently) so `http://searxng:8080`
resolves correctly from inside `claude-agent`. Tinyproxy remains bound to
`$BRIDGE_IP` (default bridge gateway); containers on `claude-agent-net` reach
it via VM-internal routing between bridge interfaces, with a corresponding
`DOCKER-USER -s $AGENT_NET_CIDR -j CLAUDE_AGENT` jump rule ensuring the
tinyproxy RETURN rule fires. Containers not in the `claude-agent-net` network
(the default bridge path when `--enable-local-search` is off) are unaffected.

### Consequences

- `--enable-local-search` enables privacy-preserving websearch with all
  traffic governed by the existing tinyproxy allowlist. No new egress control
  mechanism is introduced.
- SearXNG's `~/.claude-agent/searxng/` directory survives `--rebuild`. The
  generated `secret_key` is stable; regenerating requires manually removing
  that directory.
- The SearXNG instance is also the pre-requisite for a future Perplexica
  deployment: point Perplexica's config at `http://searxng:8080` on the same
  bridge, add a CLAUDE_AGENT RETURN rule for perplexica→searxng:8080, done.
- `NO_PROXY=searxng` is added to `claude-agent`'s env so the Python MCP
  shim's direct HTTP call to `http://searxng:8080` bypasses tinyproxy (which
  would reject it — `searxng` is not on the domain allowlist, nor should it
  be, since it is a container name not a public host).
- Any engine that bypasses `outgoing.proxies` (e.g. one that opens a raw
  socket instead of going through the configured httpx transport) also bypasses
  the allowlist. The seeded engine list was chosen from engines that use the
  standard httpx transport; the verification step (`grep -rn 'httpx\.(get|post'`
  across engine source) should be re-run if the engine list is expanded.

## ADR-015: Seed a global container CLAUDE.md from a repo template

### Context

`~/.claude-containers/shared/` is bind-mounted to `/root/.claude/` in every
container spawned by `start-claude.sh` and `start-agent.sh`. Claude Code
auto-injects `~/.claude/CLAUDE.md` into every session's system prompt, but we
weren't populating that file, so models running inside containers routinely
formed wrong hypotheses about the environment: probing `/Users/<name>/.claude/`
(the macOS layout suggested by the cwd) instead of `/root/.claude/`; retrying
fetches of `github.com` without recognizing the hostname-allowlist rejection;
fighting the read-only sandbox mounts on `/tmp` and `/root/.cache` instead of
retargeting to `$TMPDIR`. The repo-root `CLAUDE.md` documents these facts but
is only auto-injected when cwd is under this repo, so projects opened
elsewhere in the container got none of it.

### Decision

Keep a single `templates/global-claude.md` in the repo, structured as common
facts → claude-agent specifics → claude-dev exceptions. On startup, both
scripts copy the template to `~/.claude-containers/shared/CLAUDE.md` if that
path does not already exist. User edits are preserved. A
`--reseed-global-claudemd` flag on each script overwrites unconditionally so
template updates can be pulled in explicitly. No dynamic regeneration (no
splicing of the current backend, current allowlist, etc.) in this iteration —
static content, one source of truth, consequences below.

### Consequences

- New container sessions get environment context in their system prompt
  regardless of cwd, reducing misdirected probing and retry loops.
- With seed-if-missing semantics, users who ran the script before this change
  won't pick up template updates without passing `--reseed-global-claudemd`.
  Acceptable tradeoff: the file is user-editable, so silent overwrites would
  destroy local customization.
- Static claims can go stale. Any change to default allowlist contents, the
  set of supported backends, or sandbox behavior must be reflected in
  `templates/global-claude.md` as part of the same change.
- Baking the file into the Dockerfile would not work: the shared bind mount
  overrides `/root/.claude/` at runtime, so anything written there at image
  build time is hidden. Host-side seeding is the only path that's actually
  read.
- OpenCode (in `start-agent.sh` only) is seeded from the same template into
  `~/.claude-agent/opencode-config/AGENTS.md` and referenced from
  `opencode.json`'s `instructions` field. The trailing `claude-dev` exceptions
  section is stripped on the OpenCode copy via `awk` — it describes
  bubblewrap / `$TMPDIR` constraints that only apply inside `start-claude.sh`,
  and OpenCode never runs there. `--reseed-global-claudemd` reseeds both the
  Claude Code and OpenCode copies.

## ADR-016: start-agent: Vane as default-on AI research UI; SearXNG + Vane default-on

**Date:** 2026-04-22
**Status:** Partially superseded — see ADR-028. The default-on SearXNG decision
still stands for OpenCode's websearch backend. The Vane portion is superseded: Vane
was extracted from `start-agent.sh` into the standalone `research.py` script.

### Context

ADR-014 introduced SearXNG as an opt-in feature (`--enable-local-search`). In
practice, the search stack is lightweight (one extra container on the bridge)
and broadly useful — there is no good reason to skip it. Additionally, Vane
(formerly Perplexica, `itzcrazykns1337/vane:slim-latest`) was identified as a
human-facing AI research UI that complements the agent's MCP websearch tool:
SearXNG serves both. The prior opt-in flag created friction and left the
feature undiscovered by default.

### Decision

**Flip search from opt-in to default-on.** SearXNG and Vane now start
alongside `claude-agent` on every run. `--disable-search` (env:
`CLAUDE_AGENT_DISABLE_SEARCH=1`) suppresses both. `--enable-local-search` is
kept as a no-op deprecated alias with a warning to ease the transition.

**Vane container wired to SearXNG.** Vane runs as `docker.io/itzcrazykns1337/
vane:slim-latest` on the same `claude-agent-net` user-defined network, with
`SEARXNG_API_URL=http://searxng:8080` pre-configured. Port 3000 is bound to
the macOS host (`-p 3000:3000`). Vane's data directory
(`~/.claude-agent/vane-data/`) is bind-mounted so LLM configuration and
search history survive `--rebuild` (which removes the container but not the
volume directory).

**LLM pre-configuration is not possible via env var.** The Vane image does not
expose an env var for the Ollama/omlx endpoint. Users configure it once via
the web UI at `http://localhost:3000` on first access; the setting persists in
the data volume. A startup note directs new users there.

**No new firewall rules needed.** Vane → SearXNG traffic uses the existing
intra-`AGENT_NET_CIDR` port-8080 RETURN rule. Vane → Ollama/omlx uses the
existing `HOST_IP:INFERENCE_PORT` RETURN rule, which has no source-CIDR
restriction — it covers all containers routed through the CLAUDE_AGENT chain.

### Consequences

- All new `claude-agent` runs get a local search stack and a browser-accessible
  AI research UI without any flags.
- Port 3000 on the macOS host is occupied by default. Users with a local dev
  server on 3000 must pass `--disable-search` or stop Vane manually.
- `--rebuild` removes both the SearXNG and Vane containers; data persists in
  `~/.claude-agent/searxng/` and `~/.claude-agent/vane-data/`.
- First-time Vane use requires a one-time manual LLM endpoint configuration
  in the web UI; subsequent runs restore the persisted config automatically.

## ADR-017: Remove stale model-behavior pins

**Date:** 2026-04-24
**Status:** Accepted

### Context

Four environment-level and skill-level pins accumulated over time, each added
for a different reason:

- **`CLAUDE_CODE_DISABLE_1M_CONTEXT=1`** — added with the rationale "quality
  degradation observed with the larger window." No specific incident documented,
  no ADR.
- **`CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1`** — added to address a real,
  widely-reported Feb–Mar 2026 issue where adaptive thinking allocated zero
  reasoning tokens, causing hallucinations and unsound code changes. On
  Opus 4.7+ this env var is a dead letter: adaptive thinking is the only mode
  and the variable is silently ignored.
- **`effortLevel: medium` in `settings.json`** — added to cap cost. Had the
  side-effect of suppressing thinking across all models and overriding
  model-specific defaults that Anthropic calibrates per release (currently
  `xhigh` for Opus 4.7, `high` for Sonnet 4.6).
- **`model: claude-opus-4-5` + `effort: high` in `skills/plan/SKILL.md`** —
  hard-pinned the `/plan` skill to a versioned model ID with a fixed effort
  level. No rationale documented; both stale.

Each pin had become stale, counterproductive, or a dead letter on current-
generation models, and none had a clear rollback criterion or owner.

### Decision

Remove all four pins. Rely on Claude Code's per-release model-native defaults
rather than environment-level overrides.

- **Skills use alias-based model references** (`opus`, `sonnet`) rather than
  versioned IDs, so they auto-track Anthropic's latest release in each family
  without manual maintenance. `/cleanup` is pinned to `sonnet` because it
  writes `CLAUDE.md` and `ADR.md` prose that durably shapes future sessions.
  `/plan` is pinned to `opus` for planning quality. `/implement` is
  intentionally left unpinned — picking the implementation model is a
  per-task judgment call best left to the session.
- **Effort is omitted from skill frontmatter** across the board. Skills inherit
  the session effort, making `/effort <level>` work situationally without
  baking a level into the skill definition.
- **Situational overrides** use `/effort <level>` per-session or
  `effortLevel` in a project's `.claude/settings.local.json` — not global
  pins that apply to every model and every task.

### Consequences

- Higher baseline token spend per session: model-native effort often exceeds
  `medium`, particularly on Opus 4.7 (`xhigh` default).
- Container sessions following a future model that regresses on adaptive
  thinking could reintroduce the Feb–Mar 2026 zero-token-thinking symptoms.
  The rollback lever is `/effort <level>` per-session or a project-level
  `effortLevel` override — **not** re-adding `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING`
  (silently ignored on Opus 4.7+). If a future release brings back the
  zero-token failure mode, re-pin via `settings.json`'s `effortLevel` rather
  than the env var.
- The effort system and model-native defaults change with each Claude release.
  Removing the global pin means no maintenance burden and no false confidence
  that a stale value is still optimal.

## ADR-018: research.py as a Python probe; do not proactively port start-agent.sh

**Date:** 2026-04-25
**Status:** Accepted

### Context

`plans/draft-research-python.md` proposed writing the new `research` host-side
orchestrator in Python (stdlib-only, single file) rather than as another bash
sibling to `start-agent.sh`. The plan was framed as a probe: build it in Python
greenfield, then evaluate whether the result is clearly cleaner than a bash
equivalent would have been. If yes, the case for porting `start-agent.sh` later
becomes empirical; if not, the lesson is cheap.

Phase 1 produced `research.py` (879 lines, stdlib-only, executable via shebang)
with feature parity to the `research.sh` outline in
`plans/research-vm-isolation.md`, plus `tests/test_research.py` (17 unit tests
exercising the pure helpers — all passing).

### Decision

Keep `research.py` as Python. Do **not** proactively port `start-agent.sh` or
`start-claude.sh`. The repo is, for now, deliberately bilingual: bash for the
two existing host-side orchestrators, Python for `research.py`.

The path to a future `start-agent.sh` port remains open if it is otherwise
warranted (e.g., a major refactor is already on the table), but the evidence
from this probe does not justify a port on its own merits.

### Evaluation against Phase 2 criteria

1. **Line count.** 879 Python lines vs. the bash plan's 400-500 estimate — a
   ~1.7-2x ratio against Python at first glance. Adjusted: ~107 of the 879
   are the allowlist seed (identical in either language) and ~75 are argparse
   declarations that replace bash's hand-rolled flag parsing + drift-prone
   help text. Net "logic" line count is closer to 1.4x the bash estimate, and
   in exchange Python gets typed dataclasses, a real arg parser, and pure
   helpers that are unit-testable. Not a clear win on size; not a loss either.

2. **Templating fragility eliminated.** `render_iptables_apply_script()` is
   the single biggest concrete win. The function returns a fully-interpolated
   shell script with zero `${VAR}` references remaining — verified by a unit
   test (`test_iptables_no_uninterpolated_vars`). The bash equivalent would
   nest a heredoc inside a `colima ssh` heredoc, requiring two layers of
   `\$VAR` escaping that historically bite during edits. This category of bug
   is *gone*, not reduced.

3. **Testability is real, not theoretical.** `tests/test_research.py` exists
   and runs (17/17 passing). The pure helpers (`allowlist_to_regex_filter`,
   `render_searxng_settings`, `render_iptables_apply_script`,
   `render_tinyproxy_conf`) became testable as a side effect of the
   data-vs-orchestration split. A bash version would have no equivalent —
   the regex generator can only be tested by running the whole script.

4. **Edit-run ergonomics.** Comparable to bash. No build step, single file,
   `./research.py` works. Python's import-time errors (typos in dataclass
   fields, missing imports) surface faster than bash's run-time errors, but
   not dramatically.

5. **Bug-density during development.** Lower than the equivalent bash would
   have been, mostly attributable to (2). Subprocess wrappers around `colima
   ssh` were also less fiddly than the existing `start-agent.sh` heredocs —
   `vm_sh()` pipes the command via stdin, sidestepping colima's argv-join
   double-quoting. This matches Unknown #2 in the plan.

### Why not port start-agent.sh

- `start-agent.sh` is 1201 lines of working, debugged code with substantial
  institutional knowledge baked into its 17 ADRs. The bug surface introduced
  by a wholesale port — even one done carefully — would likely exceed the
  ergonomic gain for some time.
- The Python wins demonstrated above (templating, testability, real arg
  parsing) are largest in *new* code where the bash patterns haven't been
  written yet. Retrofitting them onto an existing working script captures
  less of the upside.
- `start-agent.sh` is operationally critical for this dev environment.
  `research.py` is not — it spins up an isolated browser-facing tool. The
  blast radius of a regression differs by orders of magnitude.

### Consequences

- The repo now has two host-side languages. New host-side scripts should
  default to Python following the patterns in `research.py` (dataclasses for
  config, pure helpers separated from subprocess wrappers, argparse for CLI,
  unit tests for pure helpers).
- `start-agent.sh` and `start-claude.sh` remain bash. Edits to those scripts
  should follow the existing bash idioms; do not introduce piecemeal Python
  helpers called from bash, which would be the worst of both languages.
- If a future change to `start-agent.sh` would touch a substantial fraction
  of the script (say >30% of lines, or rework of the firewall/allowlist core),
  reconsider porting at that point — the migration cost amortizes over a
  rewrite that was happening anyway. Until then, the bilingual repo is the
  cheaper steady state.
- `tests/test_research.py` is the seed for the broader test suite proposed in
  `plans/draft-tests.md`. Phase 4 of that plan (settings-injection tests) is
  exactly the kind of pure-function logic that would also be unit-testable if
  extracted into a Python module — but that is a decision for that plan, not
  this one.

## ADR-019: Externalize research.py allowlist seed to `templates/research-allowlist.txt`

**Date:** 2026-04-25
**Status:** Accepted

### Context

`research.py` carried its default allowlist as a Python triple-quoted string
constant (`_ALLOWLIST_SEED`, ~96 lines). The seed needed substantial expansion
(tiered READ-ONLY / UPLOAD-CAPABLE structure, dozens of new domains). Keeping
it embedded in Python made the list harder to diff, harder to review in PRs,
and impossible to edit without touching Python syntax. This parallels
`templates/global-claude.md`, which is seeded to
`~/.claude-containers/shared/CLAUDE.md` by `start-agent.sh` and
`start-claude.sh` on first run.

### Decision

Move the allowlist body to `templates/research-allowlist.txt` (plain text, one
domain per line). Add a module-level constant:

```python
TEMPLATE_ALLOWLIST = Path(__file__).parent / "templates" / "research-allowlist.txt"
```

`seed_allowlist()` reads from this path instead of the in-memory string. If
the file is missing, it raises `FileNotFoundError` with a message pointing at
the expected path — this is a developer-facing failure (broken checkout), not
a user-runtime failure. Existing on-disk `~/.research/allowlist.txt` files are
not overwritten; `--reseed-allowlist` (Phase 3) is the opt-in migration path.

### Consequences

- Editing the seed allowlist is now a one-line diff in a plain text file,
  reviewable without Python context. Template changes are visible at a glance
  in PR diffs.
- A `research.py` invocation from a broken checkout (templates/ missing or
  deleted) now fails loudly rather than silently falling back to a stale seed.
- `research.py` depends on being run from a complete repo checkout, same
  assumption `start-claude.sh` makes for `templates/global-claude.md`. This
  is intentional and documented.
- Existing users will not see the expanded seed until they run
  `--reseed-allowlist` or manually delete `~/.research/allowlist.txt`. New
  users get the expanded seed automatically.

## ADR-020: Tiered allowlist structure — READ-ONLY vs UPLOAD-CAPABLE

**Date:** 2026-04-25
**Status:** Accepted

### Context

`templates/research-allowlist.txt` previously mixed read-oriented hosts
(arxiv.org, wikipedia.org) with upload-capable hosts (gitlab.com,
huggingface.co) without distinguishing them. tinyproxy filters by hostname
only — it cannot distinguish a read path from a write path on the same host.
A model operating in the research environment could, if instructed by an
injected page, push to gitlab.com or upload a dataset to huggingface.co as
easily as it could read from them. The seed gave no signal that these hosts
carried different risk.

### Decision

Rewrite the template with two explicit tiers:

- **READ-ONLY** (active by default): hosts where the primary use-case is
  reading — search engines, reference sites, preprint servers, academic
  publishers, software docs, standards bodies, open data repositories, news,
  universities. Expanded from 66 to 223 active entries.
- **UPLOAD-CAPABLE** (commented out): hosts where the same hostname also
  accepts writes — `github.com`, `gitlab.com`, `huggingface.co`, `zenodo.org`,
  `ssrn.com`, `kaggle.com`, etc. Each entry carries a short inline rationale.
  Users uncomment per-host, accepting the residual risk explicitly.

An **EXPLICITLY EXCLUDED** documentation block at the end names pure exfil
channels (anonymous paste/upload sites, webhook capture services,
reverse-tunnel hosts) and explains why they are absent.

The template preamble states the constraint plainly: tinyproxy cannot enforce
"read-only mode" on a hostname — the tiering is documentation of risk, not
technical enforcement.

### Consequences

- New users get a substantially larger and better-organized seed (223 entries
  vs 66), covering the common research use-cases that the old seed missed.
- The risk distinction between read-oriented and upload-capable hosts is
  visible in the file, not implied. Users who need github.com make a conscious
  choice rather than inheriting it silently.
- `--reseed-allowlist` lets existing users migrate to the new template without
  deleting state manually.
- `gitlab.com` and `huggingface.co` are no longer active by default; users who
  relied on them must uncomment. This is an intentional breaking change for
  a security reason.

## ADR-021: research.py uses Squid for domain filtering; start-agent.sh stays on tinyproxy

**Date:** 2026-04-26
**Status:** Accepted

### Context

research.py's layered denylist grew large enough to break tinyproxy. With
`FilterExtended Yes`, tinyproxy compiles every denylist entry into a single POSIX
extended regex NFA. Memory scales superlinearly with entry count:

| Filter shape | Behavior |
|---|---|
| Empty | Starts, serves requests |
| ~14k entries (`fake` list only) | Starts, serves requests |
| ~409k entries (`pro` + `fake`) | Starts; OOM-killed on first non-matching CONNECT |
| ~1.56M entries (`pro` + `fake` + `tif`) | OOM-kills at startup |

The failure is per-request, not just startup: even with more RAM, every
non-matching CONNECT walks the entire NFA, so memory headroom degrades under
concurrent requests. Intermittent failure is worse than a hard startup failure.

Squid's `acl ... dstdomain "/path/to/list"` uses a hash table: O(1) lookup per
request, O(n) memory — routinely used for million-entry lists in corporate proxies.

Alternatives rejected:
- **3proxy**: lighter, but ACL syntax for giant external lists is less documented;
  thinner operational track record at this scale.
- **DNS-level filtering** (dnsmasq/unbound): faster lookups, but clients see
  NXDOMAIN-shaped failures instead of HTTP 403 — harder to diagnose and
  inconsistent with the proxy model the rest of the stack uses.

start-agent.sh's allowlist has ~280 entries. The regex NFA is fine at that scale,
and pulling Squid into the agent path adds a dependency for no benefit.

### Decision

Replace tinyproxy with Squid in research.py. Clean cut — no `--proxy=` toggle,
no fallback paths, no migration-period bilingualism. start-agent.sh is unchanged.

Key implementation details:

- **Port 8888 is kept.** Squid listens on the same port, so the iptables RESEARCH
  chain and SearXNG's `outgoing.proxies` config require no changes.
- **Minimal `squid.conf`.** ~15 directives: `http_port`, `dstdomain` ACL,
  `http_access` rules, `cache deny all`. `cache deny all` is load-bearing —
  this is a pure filtering proxy; disabling the cache also avoids sizing
  `cache_dir`.
- **`acl CONNECT method CONNECT` must be declared** before use in `http_access`
  rules. Squid 6 rejects configs that reference undefined ACL names, causing a
  fatal startup failure.
- **`_prune_subdomains()` is required.** Squid 6 rejects a `dstdomain` ACL file
  that contains both a domain and any of its subdomains (e.g. `.sub.example.com`
  alongside `.example.com`). The helper removes any domain whose ancestor is
  already present before the ACL file is written. Coverage is not reduced —
  the parent already covers the subdomain via Squid's suffix matching.
- **`squid -k reconfigure`** is the fast-reload mechanism. It rotates the ACL
  table in-place without dropping in-flight CONNECT tunnels.

### Consequences

- research.py can load the full hagezi denylist (~1.56M entries once `tif` is
  re-enabled in Phase 2) without OOM on the default 2 GiB VM.
- Reload of 409k entries takes ~6s wall time (Python computation + SSH transfer +
  squid reconfigure). At 1.56M entries this may reach ~24s; a compressed SSH
  transfer (`gzip | tee | gunzip`) would significantly reduce the transfer
  portion if that becomes unacceptable.
- start-agent.sh keeps tinyproxy. The two scripts now use different proxy
  backends, intentionally. New host-side scripts with large filter lists should
  default to Squid; small allowlists are fine with tinyproxy.
- Squid is an additional VM-level dependency for the `research` Colima profile,
  installed idempotently via `apt-get install squid` on first bring-up.

## ADR-023: Layered denylist model for research.py

**Date:** 2026-04-26
**Status:** Accepted

### Context

research.py needed an egress filter that lets Vane (the AI research UI) scrape
arbitrary search-result URLs while still blocking low-quality or weaponizable
content. An allowlist approach (ADR-019, ADR-020) was incompatible with that
goal — a domain that appears in a search result cannot be predicted in advance,
so any allowlist aggressive enough to provide useful filtering would also block
legitimate results.

The migration also surfaced a secondary concern: legitimate-but-exfil-capable
services (pastebins, webhook capture, reverse tunnels, code-hosting write paths)
would not appear in any curated research-quality filter, yet a prompt-injection
payload could instruct Vane to exfiltrate through them.

### Decision

Replace research.py's allowlist with a three-layer denylist model:

    Final denylist = (cached-upstream ∪ denylist-additions.txt) − denylist-overrides.txt

**Layer 1 — Upstream feeds** (`~/.research/denylist-sources.txt`, seeded from
`templates/research-denylist-sources.txt`): URL-pinned downloads from
hagezi/dns-blocklists. Three feeds:
- `multi.pro` — broad-coverage malware, phishing, tracking, content farms,
  AI SEO slop. Primary quality filter.
- `fake` — misinformation and propaganda sites. Research-quality filter.
- `tif` — active threat intelligence feed; rotates frequently as threats are
  taken down.

**Layer 2 — Local additions** (`~/.research/denylist-additions.txt`, seeded
from `templates/research-denylist-additions.txt`): ~50 curated domains that
the upstream feeds won't include because they're legitimate services, not
malicious infrastructure. These are the exfil-capable targets: anonymous paste
and upload sites, webhook capture services, messaging delivery APIs, reverse
tunnels, and code-hosting write paths.

**Layer 3 — Local overrides** (`~/.research/denylist-overrides.txt`, empty by
default): User-maintained escape hatch for upstream false positives. Any domain
in overrides is removed from the final filter regardless of which upstream feed
pulled it in. `--refresh-denylist` never clobbers this file.

**Why denylist for research.py; why allowlist stays for start-agent.sh:**
- research.py runs Vane interactively — the user selects queries and supervises
  results. The bottleneck is research quality; the threat is prompt-injection via
  returned content, not autonomous tool use. A denylist lets Vane reach the long
  tail of search results while filtering known-bad domains.
- start-agent.sh runs Claude Code and OpenCode with more autonomous tool surface
  — model-initiated webfetch, PR creation, code pushes. Tighter egress control
  (allowlist + tinyproxy) is warranted there. The asymmetry is intentional.

**Pinning policy:**
Upstream feed URLs in `denylist-sources.txt` are pinned to a specific commit SHA
(or release tag), never to `main` / `HEAD`. This ensures upstream changes — including
potentially malicious additions from a compromised maintainer — flow through only
after a human reviews the diff. `--refresh-denylist` fetches the currently-pinned
SHA's content; updating the pin requires editing `denylist-sources.txt` and running
`--reseed-denylist`, which is an explicit human action.

**Rate limiting:**
iptables rate limiting (hashlimit / limit module) is added to the RESEARCH chain
as defense-in-depth against bulk exfil. Rate limiting is secondary to the denylist;
a slow trickle can still exfiltrate under the limit, and an attacker-controlled
domain bypasses both. The actual exfil control is human supervision.

### Consequences

- Vane can scrape arbitrary search-result URLs, including the long tail of
  legitimate research domains that an allowlist could never enumerate.
- The upstream hagezi feeds provide broad quality filtering (~400k–1.5M entries)
  that no manually-curated allowlist could match. Research quality improves.
- Known-bad infrastructure (malware, phishing, content farms) is filtered
  consistently without manual maintenance once feeds are pinned.
- A real adversary who controls their own domain — or who registers a fresh
  domain not yet in upstream feeds — bypasses the denylist. This is an
  acknowledged residual risk. Human supervision of Vane is the actual exfil
  control, not the proxy.
- Refresh cadence matters: `tif` (threat intel) benefits from daily or weekly
  refresh as entries rotate. `pro` and `fake` are stable; monthly refresh is
  sufficient. Falling behind on `tif` means the active-threat coverage degrades.
- See ADR-021 for why Squid is used instead of tinyproxy at this scale.

## ADR-022: Hard-exit on legacy allowlist.txt; no smooth migration

**Date:** 2026-04-26
**Status:** Accepted

### Context

The allowlist→denylist migration (ADR-019 → ADR-021) replaced `~/.research/allowlist.txt`
with a three-file layout (`denylist-sources.txt`, `denylist-additions.txt`,
`denylist-overrides.txt`) plus a `denylist-cache/` directory. An existing
installation has the old file; a new `research.py` invocation would proceed past
the seed step (which checks for the *new* files, not the old one), silently
ignore the old allowlist, and bring up the research VM in denylist mode with no
prior setup — either fetching upstream feeds from scratch or failing if the
sources file is misconfigured.

### Decision

Detect the old layout at startup: if `~/.research/allowlist.txt` exists,
`_check_legacy_allowlist()` prints a hard error naming the two manual steps
(`rm -rf ~/.research/`, then `./research.py --rebuild`) and exits non-zero
immediately, before any seeding, fetching, or VM bring-up.

No automatic migration: don't translate `allowlist.txt` to an equivalent
denylist, don't rename it, don't seed around it. YAGNI — the old allowlist
model is incompatible with the new one by design (allowlist vs. denylist mode),
and a translated allowlist would be semantically wrong anyway. The user must
take the explicit action.

### Consequences

- Existing users get a clear, actionable error instead of a silent behavior
  change or a partially-initialized state.
- `denylist-overrides.txt` and any custom edits to old `allowlist.txt` are lost
  on `rm -rf ~/.research/`. Accepted: users have had no time to accumulate
  meaningful state in the new layout, and the old allowlist was superseded.
- No migration code to maintain. The check is a two-line conditional; once the
  user has migrated, it never fires again.

## ADR-024: Expand start-agent.sh allowlist with research.py READ-ONLY entries

**Date:** 2026-04-26
**Status:** Accepted

### Context

start-agent.sh's allowlist (~150 entries) was seeded for Claude Code + git
workflows, not research workflows. When OpenCode uses its `webfetch` and
`websearch` tools, it reaches domains common in research — academic publishers,
preprint servers, documentation hosts, reference sites — that were absent from
the allowlist, causing 403s on valid fetches.

`templates/research-allowlist.txt` had already accumulated ~223 READ-ONLY
research entries (ADR-020). Phase 2 of the allowlist/denylist migration retired
that file (research.py switched to denylist mode), but its READ-ONLY entries
remained valid candidates for start-agent.sh.

### Decision

Merge the READ-ONLY tier of `templates/research-allowlist.txt` into
start-agent.sh's inline heredoc allowlist, preserving start-agent.sh's
existing category comments and organization style. UPLOAD-CAPABLE entries
(commented-out tier in the research template) are excluded — the start-agent.sh
security model intentionally keeps write-capable hosts off the default allowlist.

After consuming these entries, `templates/research-allowlist.txt` has no remaining
consumers and is deleted from the repo.

### Consequences

- OpenCode `webfetch` / `websearch` hits far fewer 403s on common research
  domains; agent effectiveness for research tasks improves.
- start-agent.sh remains allowlist-based (not denylist). The expanded allowlist
  is still human-reviewable and LLM-uneditable. The threat model from ADR-010
  is unchanged.
- Write-capable hosts (github.com, gitlab.com, huggingface.co, etc.) remain
  absent from the default seed. Users who need them add them manually and accept
  the residual write-path risk explicitly (same as before).

## ADR-025: Use hagezi `wildcard/<list>-onlydomains.txt` instead of `domains/<list>.txt`

**Date:** 2026-04-26
**Status:** Accepted

### Context

After the Squid migration (ADR-021), the research VM was loading hagezi's
`domains/pro.txt`, `wildcard/fake.txt`, and `domains/tif.txt`. End-to-end
testing through the proxy revealed that requests to *apex* domains of well-
known ad/tracking families were leaking through. For example,
`https://doubleclick.net/` returned 200 while `https://accounts.doubleclick.net/`
correctly returned 403.

Root cause: hagezi's `domains/` files target DNS resolvers (where a wildcard
match on `*.foo.com` covers `foo.com` itself by virtue of how DNS lookups
work). They list 4,000+ subdomains under `doubleclick.net` but never the
apex `doubleclick.net`. Squid's `dstdomain` ACL doesn't share that semantic
— `.accounts.doubleclick.net` matches `accounts.doubleclick.net` and its
subdomains, but not `doubleclick.net` itself.

This is a structural mismatch between DNS-blocker feed shape and HTTP-proxy
ACL shape, not a code bug.

### Decision

Switch all upstream feeds in `templates/research-denylist-sources.txt` to
hagezi's `wildcard/<list>-onlydomains.txt` variants. These list one
registrable/apex domain per line with subdomain hierarchies pre-rolled-up.
`denylist_to_squid_acl()` prefixes each entry with `.`, so a single
`.foo.com` line covers the apex *and* every subdomain via Squid's
suffix-match.

A small set of canonical Google ad apexes that hagezi deliberately omits
(`doubleclick.net`, `googleadservices.com`, `googletagmanager.com`,
`googletagservices.com`, `google-analytics.com`, `adservice.google.com`)
is appended to `templates/research-denylist-additions.txt`. Hagezi omits
these intentionally because they're load-bearing for legitimate Google
services on the open internet; for an isolated research VM the trade-off
is reversed.

`_prune_subdomains()` is retained as a safety net for `additions` and
`overrides` overlaps but becomes a near-no-op for upstream feeds (already
pruned upstream).

### Consequences

- Apex domains of every blocked family are now correctly blocked by
  construction. The probe `tests/probe-denylist.sh` confirms
  `googlesyndication.com` (in pro) returns 403; `doubleclick.net` and
  `googleadservices.com` (omitted by hagezi) return 403 once the
  additions block is applied.
- Feed sizes drop ~50% (e.g. `pro` went from ~395k to ~183k entries).
  Squid worker RSS dropped from observed ~99 MB at full coverage to a
  smaller footprint.
- TIF can be enabled by default at the standard 2 GiB VM size: combined
  feeds total ~800k apex entries with comfortable headroom.
- The decision is portable: any future migration to a different proxy
  daemon that uses hostname-suffix ACLs (3proxy, h2o, custom) inherits
  the correct semantics. A move to a regex- or DNS-based filter would
  need to revisit feed format.
- The hagezi-omitted-apex list is small but evergreen. New entries will
  be discovered during normal use and added to the additions template
  over time.

## ADR-026: Auto-prune orphan files in `~/.research/denylist-cache/`

**Date:** 2026-04-26
**Status:** Accepted

### Context

`compose_denylist()` reads `~/.research/denylist-cache/*.txt` via a glob
and unions every entry into the final denylist. When `denylist-sources.txt`
changes — a template SHA bump, switching feed paths (e.g. `pro.txt` →
`pro-onlydomains.txt`), or commenting out a feed — the previously-cached
files remain on disk. They were silently merged into the next compose,
producing a denylist that was the *union* of the old and new feeds rather
than just the current ones.

This came up directly during the ADR-025 migration: after switching to
onlydomains feeds, the old `pro.txt` and `tif.txt` cache files were still
present and inflating the composed denylist by ~500k stale entries. The
fix at the time was a manual `rm` step in the migration instructions.
Every future feed-path change would re-introduce the same trap.

### Decision

Add `prune_orphan_cache_files(paths) -> List[str]` that deletes any `.txt`
in the cache dir whose basename isn't produced by a current URL in
`denylist-sources.txt`. Call it at the start of both:

- `refresh_denylist_cache()` — before fetching, so newly-fetched files
  aren't accidentally pruned by a race
- `reload_denylist_fast_path()` — so users who edit `sources.txt` and
  reload (without refreshing) also get cleanup

Pruned filenames print as `==> Pruned orphan cache file: <name>` so the
behavior is visible.

Source URLs that are commented out are treated as removed for prune
purposes — uncommenting one will re-fetch on the next refresh.

### Consequences

- Editing `sources.txt` and running `--reload-denylist` or
  `--refresh-denylist` is self-healing: no manual `rm`, no migration
  instructions.
- Future template SHA bumps that change feed basenames don't require
  any user action beyond `--reseed-denylist && --reload-denylist`.
- The behavior is destructive: a user who manually drops a `.txt` into
  `denylist-cache/` outside of the refresh path will see it deleted on
  the next run. Accepted: that directory is a managed cache, not a
  user-edit surface — `denylist-additions.txt` and `denylist-overrides.txt`
  exist for user-curated entries.
- Five new unit tests in `tests/test_research.py` cover the helper:
  happy path, no-op, all-sources-removed, missing cache dir, and
  commented-out-URL handling.

## ADR-027: research.py: route Vane through Squid via `HTTPS_PROXY` only

**Date:** 2026-04-26
**Status:** Superseded by ADR-029 — both `HTTP_PROXY` and `HTTPS_PROXY` are now set on `research-vane`; the HTTPS-only workaround was based on a wrong-Vane observation.

### Context

`research.py` is built around a default-allow Squid proxy with a denylist
(ADR-021, ADR-023): the design intent is that Vane can scrape arbitrary
URLs *unless* they match the denylist, and that all of Vane's egress is
visible and filterable at Squid.

Initially the Vane container was started with only `SEARXNG_API_URL` set —
no proxy env vars. Squid was not in Vane's egress path at all. Vane's
scrape attempts went direct to the docker bridge, hit the L3 default
REJECT in the `RESEARCH` iptables chain, and failed silently. Vane fell
back to whatever snippets SearXNG returned, the LLM synthesized from
snippets, and (because the LLM has no introspection into the upstream
pipeline) reported its synthesis as "scraping" with fabricated
success/failure tallies. Symptoms looked like "search just doesn't work
very well" rather than a configuration bug.

### First attempt and the regression it caused

The obvious fix was to pass all three proxy env vars on Vane's
`docker run`:

```
HTTP_PROXY=http://{bridge_ip}:{SQUID_PORT}
HTTPS_PROXY=http://{bridge_ip}:{SQUID_PORT}
NO_PROXY={CONTAINER_SEARXNG},host.docker.internal,localhost,127.0.0.1
```

This made queries hang indefinitely. The `/api/chat` SSE stream stayed
open, Vane emitted the "searching" UI-state event with the decomposed
sub-queries, and then nothing — no SearXNG calls, no Squid traffic, no
errors. A sidecar `curl` with the *exact same* env vars worked perfectly
(curl correctly applies `NO_PROXY` and bypassed Squid for the SearXNG
hostname), proving the env values were correct and the regression was
specifically Vane's HTTP-client behavior. Some interaction between
`HTTP_PROXY` and Vane's HTTP client (Next.js / its langchain pipeline)
silently swallowed the SearXNG `fetch()` for `http://research-searxng:8080`.

### Decision

Pass only two env vars on Vane's `docker run` in `ensure_vane_container`:

```
HTTPS_PROXY=http://{bridge_ip}:{SQUID_PORT}
NO_PROXY={CONTAINER_SEARXNG},host.docker.internal,localhost,127.0.0.1
```

Specifically: **drop `HTTP_PROXY`**. That single variable is what triggers
Vane's bad path. With only `HTTPS_PROXY` set:

- `http://research-searxng:8080` (Vane → SearXNG) — no proxy applies
  (HTTPS_PROXY only governs HTTPS URLs) → goes direct over the docker
  bridge → works.
- `http://host.docker.internal:8000` (Vane → host LLM) — same; direct →
  works.
- `https://*` scrape targets — `HTTPS_PROXY` applies → routed through
  Squid → denylist enforced as designed.

`NO_PROXY` is kept as belt-and-suspenders — even though `HTTP_PROXY` is
no longer set, some libraries consult `NO_PROXY` independently when
choosing whether to apply `HTTPS_PROXY`. Including the in-network
hostnames there ensures the SearXNG call is unambiguously direct.

The change only takes effect on container creation, so picking it up on
an existing install requires `docker rm -f research-vane && ./research.py`
(or a full `--rebuild`).

### Consequences

- **HTTPS scrape targets are routed through Squid** and the denylist
  applies to them. The vast majority of the modern web is HTTPS, so the
  "default-allow with denylist" design intent is in force for the cases
  that matter.
- **HTTP-only scrape targets (a small minority of the web) are not
  reachable** by Vane — they go direct and hit the L3 REJECT. Accepted as
  a tradeoff for keeping the SearXNG path working without depending on
  Vane's HTTP-client honoring `NO_PROXY` correctly.
- Squid's `access.log` becomes the authoritative record of HTTPS scrapes,
  with `TCP_DENIED` markers for denylist hits. More reliable than any
  system-prompt instruction asking the model to list scraped URLs
  (which only produces confabulated summaries).
- The deeper "why does setting `HTTP_PROXY` break Vane's `fetch()`"
  question is left unresolved. A more principled fix would require
  tracing through Vane's (Perplexica-fork) HTTP-client setup. Out of
  scope here — the workaround is robust and the tradeoff is acceptable.
- A smoke test in `tests/probe-vane-egress.sh` checks that `HTTPS_PROXY`
  and `NO_PROXY` are present on the running container and that a sidecar
  HTTPS request through Squid succeeds.
- The `start-agent.sh` Vane container is *not* changed by this ADR.
  That container shares a bridge with `claude-agent`, which is a
  different threat model (search-result-borne prompt injection reaching
  the coding agent). Tracked separately.

### Update (post-dedup)

ADR-028's dedup of the two Vane containers revealed that the original
"queries hang" observation could not be reliably attributed to
`research-vane` — during that debug session two Vane containers shared
host port 3000, and the browser likely reached `start-agent.sh`'s Vane
(different env, different SearXNG sibling name, different network). Direct
re-testing post-dedup with `HTTP_PROXY` set on `research-vane` showed
queries succeeding; Squid's `access.log` confirmed 80+ fresh CONNECT
entries from Vane's IP, plus `TCP_DENIED/403` hits for denylist-blocked
ad/tracking domains. The HTTPS-only workaround was unnecessary. See ADR-029.

## ADR-028: Extract Vane from start-agent.sh into standalone research.py

**Date:** 2026-04-26
**Status:** Accepted (supersedes Vane portion of ADR-016)

### Context

ADR-016 made Vane default-on in `start-agent.sh`, running alongside
`claude-agent` and `searxng` on the `claude-agent-net` docker network. Vane was
always browser-only: no container-to-agent API, no MCP integration. It was an
incidental co-resident of the agent environment, sharing SearXNG with OpenCode
but otherwise independent. Its presence added complexity to `start-agent.sh`:
a third container lifecycle, a second data volume, port 3000 host binding, and
the `--disable-search` flag that was required to opt out of both Vane *and* the
SearXNG MCP stack (a coarse knob).

Meanwhile, `research.py` (ADR-018) was implemented as a dedicated Python
orchestrator for exactly the use-case Vane serves: an isolated, browser-facing
research UI with its own egress proxy. The key insight: Vane's threat model
differs from the coding agent's. In `start-agent.sh`, egress is allowlist-based
to prevent the coding agent from exfiltrating via write-capable hosts. For Vane,
egress must be permissive enough to scrape arbitrary search results — a denylist
model (ADR-023) is the correct fit. Running Vane inside `start-agent.sh`'s
allowlist-constrained VM was architecturally wrong: it either over-constrained
Vane (can't reach scrape targets not on the allowlist) or required loosening the
allowlist in ways that weakened the coding agent's posture.

### Decision

Remove Vane (and only Vane) from `start-agent.sh`. SearXNG, the
`claude-agent-net` user-defined network, the SearXNG MCP shim in
`claude-agent.Dockerfile`, and OpenCode's `mcp.searxng` + `websearch=allow`
config all remain unchanged — OpenCode's websearch tool is not affected.

Specific removals from `start-agent.sh`:

- `VANE_CONTAINER`, `VANE_DATA_DIR`, `VANE_PORT` variables
- The Vane container lifecycle block (`ensure_vane_container`)
- The Vane container rm in `--rebuild` cleanup
- The Vane startup echo line

`--disable-search` is retained but now controls only SearXNG (and therefore
OpenCode websearch). Its description is updated accordingly.

Users who want Vane run `research.py` instead, which provides VM-level isolation,
a denylist-based egress model appropriate for scraping, and its own SearXNG
instance. The two SearXNG instances are independent by design (see Notes in
`plans/research-vm-isolation.md`).

### Consequences

- `start-agent.sh` no longer binds port 3000. `research.py` and `start-agent.sh`
  can run simultaneously without port conflicts.
- OpenCode websearch is unaffected: the SearXNG MCP path was never routed through
  Vane.
- Users who accessed Vane at `localhost:3000` via `start-agent.sh` must switch to
  `./research.py`. First-time setup: run `research.py`, open `localhost:3000`,
  configure the LLM endpoint once. State persists in `~/.research/vane-data/`.
- `start-agent.sh` is simpler: one less container, one less data volume, one less
  port. The `--disable-search` semantics narrow from "skip SearXNG + Vane" to
  "skip SearXNG (disables OpenCode websearch)".
- The `start-agent.sh` allowlist no longer needs to accommodate Vane's scrape
  targets. Scrape-range decisions are `research.py`'s concern alone.

## ADR-029: research.py: route Vane through Squid via both `HTTP_PROXY` and `HTTPS_PROXY`

**Date:** 2026-04-26
**Status:** Accepted (supersedes ADR-027)

### Context

ADR-027 documented an HTTPS-only proxy configuration for `research-vane`, based
on an observation that setting `HTTP_PROXY` alongside `HTTPS_PROXY` caused Vane's
queries to hang indefinitely at the "searching" UI state with no SearXNG calls
visible in Squid. ADR-028 subsequently extracted Vane from `start-agent.sh` into
`research.py`, which inadvertently revealed the root cause: during the original
debug session, two separate Vane containers were bound to host port 3000
simultaneously — one from `start-agent.sh` and one from `research.py`. The
browser reached whichever won the bind, so observations about "research-vane's"
behavior were actually observations about `start-agent.sh`'s Vane, which had a
different SearXNG sibling name (`searxng` rather than `research-searxng`),
different network configuration, and different env vars.

Re-testing post-dedup with `HTTP_PROXY=http://{bridge_ip}:8888` set on
`research-vane` showed queries succeeding end-to-end. Squid's `access.log`
confirmed scrape CONNECTs from Vane's IP (80+ entries across Springer, Wikipedia,
Substack, and others) plus `TCP_DENIED/403` hits for denylist-matched ad/tracking
domains (`pagead2.googlesyndication.com`, `stats.g.doubleclick.net`,
`securepubads.g.doubleclick.net`, `analytics.google.com`). The regression
described in ADR-027 does not reproduce against `research-vane` post-dedup.

### Decision

`ensure_vane_container` passes all three proxy env vars on `docker run`:

```
HTTP_PROXY=http://{bridge_ip}:{SQUID_PORT}
HTTPS_PROXY=http://{bridge_ip}:{SQUID_PORT}
NO_PROXY={CONTAINER_SEARXNG},host.docker.internal,localhost,127.0.0.1
```

The structural justification is unchanged from what ADR-027 intended: the
`RESEARCH` iptables chain (`research.py:486`) REJECTs all
`research-net→external` traffic, so any scrape (HTTP or HTTPS) needs a proxy
path out. `NO_PROXY` exempts `research-searxng` and `host.docker.internal` so
Vane's SearXNG calls and LLM calls go direct over the bridge as intended.

### Consequences

- Both HTTP and HTTPS scrape targets are routed through Squid; the denylist
  applies uniformly to all of Vane's outbound web traffic.
- `tests/probe-vane-egress.sh` is updated to assert `HTTP_PROXY`, `HTTPS_PROXY`,
  and `NO_PROXY` are all present and well-formed on the running container.
- ADR-027 is marked Superseded. Its historical record of the wrong-Vane confusion
  is preserved as institutional knowledge.
- The deeper "why did the original observation look like Vane swallowing `fetch()`"
  question is not investigated further. The most likely explanation per ADR-028 is
  that the `start-agent.sh` Vane could not resolve `research-searxng` after the
  user mutated its UI URL field, but that is not load-bearing for the current
  decision.
- Existing installs need `docker rm -f research-vane && ./research.py` (or full
  `--rebuild`) to pick up the corrected env vars.

---

## ADR-030: Temperature mutation via config.json + container restart in run_vane.py

**Date:** 2026-04-26
**Status:** Accepted

### Context

The Vane confirm-sweep (`tests/vane-eval/run_vane.py`) needs to replay cheap-phase
cells with varying temperature, prompt style, and model through Vane's full search
pipeline. Vane's `POST /api/search` body (`src/app/api/search/route.ts`) accepts
only `chatModel`, `embeddingModel`, `query`, `sources`, `optimizationMode`,
`history`, `systemInstructions`, and `stream` — no `temperature` or thinking flag.
The openaiLLM provider inside Vane falls back to `this.config.options?.temperature`
when the search agent calls `generateText` without an explicit options object,
meaning temperature lives in the provider's config record, not in the API request.

### Decision

`run_vane.py` controls temperature by:

1. Reading `~/.research/vane-data/data/config.json` (the host path bound into the
   container as `/home/vane/data/config.json`).
2. Mutating `modelProviders[id=<provider_id>].config.options.temperature` to the
   desired value via `mutate_temperature()`, which is idempotent — returns `False`
   without writing if the value already matches.
3. Calling `docker restart research-vane` and polling
   `GET /api/providers` until a 200 response before issuing the next cell's search
   request. Restarts only occur when the value actually changed.

Prompt style is handled without a restart: `research_system` maps to
`systemInstructions` in the request body; `structured` prepends a format hint to
`query`; `bare` leaves both fields empty.

Thinking is not exposed by Vane's API or config — it remains a model-selection
knob exactly as in `run_cheap.py` (`cell.thinking` selects a model with
server-side reasoning enabled).

Pass `--no-restart` to skip the docker restart + config write (useful when
running a single-cell smoke test where temperature is known to be already correct).

### Consequences

- Container restarts add ~10–30s per temperature boundary. A sweep with three
  distinct temperature values across three cells incurs at most two restarts.
- If `~/.research/vane-data/data/config.json` doesn't exist (e.g., the Vane
  container was never started, or the data dir is missing), temperature mutation
  silently fails with a warning and the cell runs with whatever temperature Vane
  currently has.
- `run_vane.py` is designed to run on the macOS host, not from inside the
  `start-claude.sh` microVM — the dev container has no route to `localhost:3000`
  and no `docker` binary for the research VM.

---

## ADR-031: run_thinking.py revision — status taxonomy, fail-fast, and uniform thinking budget

**Date:** 2026-04-27
**Status:** Accepted

### Context

The first thinking-axis sweep (324 cells, 4.18 hr wall) produced corrupted data:
122/324 cells (38%) hit `finish_reason="length"` at `max_tokens=1024`, cutting
output mid-sentence. Six cells had empty `content` because the model never emerged
from `reasoning_content` before hitting the cap. The manifest labelled all six
`ok`. Additionally, per-model thinking budgets differed (E4B=4000, MoE=8000,
31b=12000), confounding model-size effects with reasoning-token budget effects.
The status logic was duplicated between `write_cell_output` and `_run_phase` and
used a heuristic (`thinking=True and reasoning=None → skip:no-thinking-support`)
that misfired on cells that produced full responses without entering the reasoning
channel.

### Decision

**Status taxonomy.** A single `classify_status(cell, result)` helper in
`lib/cells.py` defines a worst-wins precedence ladder and is called from both
`write_cell_output` (per-cell frontmatter) and `_run_phase` (manifest row).
The ladder: `error > error:no-content > warn:truncated > warn:reasoning-leaked > ok`.
`skip:no-thinking-support` is removed — per-cell absence of `reasoning_content`
on a `thinking=True` cell is a data point, not a failure; the phase-level human
prompt asserts the model configuration.

**Fail-fast assertion.** `_run_phase` checks the first successful cell of each
`(model, thinking=ON)` phase. If `reasoning_content` is absent and there was no
transport error, it raises `RuntimeError` immediately rather than running 50+
cells against a misconfigured model.

**Uniform 4000-token thinking budget.** All three Gemma 4 models are configured
at 4000 reasoning tokens (via the omlx server-side setting, applied during the
human-prompted reload). `max_tokens=8192` (up from 1024) ensures `max_tokens`
always exceeds the reasoning budget, making empty-content pathology impossible.
`timeout_s` raised from 600 to 1200 to accommodate worst-case 31b·think=on cells.

**Temperature axis.** Default temperatures changed from `[0.0, 0.3, 0.7]` to
`[0.2, 0.6, 1.0]` to bracket Google's published Gemma recommendation (`t=1.0,
top_k=64, top_p=0.95`). The prior axis sampled only below the recommended regime.

**Frontmatter.** `finish_reason` and `output_tokens` are now written to each cell
file's YAML frontmatter, pulled from `result["raw"]["choices"][0]["finish_reason"]`
and `result["raw"]["usage"]["completion_tokens"]`. Previously these required
grepping the embedded JSON blob.

**Manifest summary.** `MANIFEST.md` now opens with a `## Status summary` block
listing non-zero status counts in precedence order, making run health an
at-a-glance check rather than a 324-row scroll.

### Consequences

- `classify_status` is the single source of truth for status classification.
  Any new status values must be added there and added to `_STATUS_ORDER` in
  `run_thinking.py` for correct manifest ordering.
- The fail-fast fires on misconfiguration before files are written for corrupt
  cells, but does not fire if the very first cell fails with a transport error
  (the check gates on `not result["error"]`).
- Uniform 4000-token budget means findings about 31b are findings about
  "31b at 4k reasoning tokens", not native-capability claims.
- `finish_reason` and `output_tokens` in frontmatter are parsed from `result["raw"]`
  and default to `"unknown"` / `0` if the response shape is missing those fields
  (e.g. on transport error).

## ADR-032: Reframe `/plan` around acceptance criteria; drop per-phase Files and Testing

**Date:** 2026-04-29
**Status:** Accepted

### Context

`/plan` reliably produced over-specified, bloated plans. Two structural drivers:

1. **Audience framing pushed toward restating discoverable context.** The "Write
   for a capable implementer" rule said the implementer has "the contents of this
   plan plus the working directory (e.g., CLAUDE.md) and nothing else — no memory
   of this conversation, no outside knowledge of the surrounding task." Defining
   the implementer as context-free is functionally an instruction to restate
   anything they might want — CLAUDE.md, project conventions, the working
   directory itself were all available, but the framing pretended they weren't.

2. **The mandatory phase scaffold (`### Steps` / `### Files` / `### Testing`)
   forced weight into every phase regardless of size.** `### Files` duplicated
   path citations already in Steps. `### Testing` mostly restated the project's
   test mechanism (`uv run pytest tests/`) — pure boilerplate. Genuine
   planning-time test signal (edge cases, manual-verification surface, "no
   testable behavior") was the rare exception, not the norm. Step preambles
   then re-narrated "why" already covered in plan-level Context, because each
   phase read as semi-standalone.

The redundancy compounded with `/implement`'s ownership of TDD. `/implement`
already owns the test runner, the tests-first ordering rule, and the
tests-pass-before-commit gate. `### Testing` in plans was a second, weaker
version of the same contract written at planning time without grounding in
the actual test suite.

### Decision

Four concrete changes to `skills/plan/SKILL.md`:

1. **Audience reframing.** The "capable implementer" rule affirms what *is*
   available — plan, working directory (CLAUDE.md, README, ADR, source,
   recent git history), project conventions, standard tool knowledge —
   instead of declaring the implementer context-free. The "no memory of
   this conversation" carve-out and the don't-confabulate rule are
   preserved.
2. **Plan-level `## Approach` added** between Goals and Unknowns. Houses
   the architectural through-line across phases. Explicitly optional,
   skipped for single-phase or mechanical changes.
3. **Per-phase `### Files` and `### Testing` dropped.** Files duplicated
   Steps; Testing duplicated `/implement`'s ownership.
4. **Per-phase `### Acceptance criteria` added as optional.** Frames
   "done" as outcomes, not test mechanism. Default is to omit; include
   only for phase-specific edge cases, manual-verification surface, or
   non-testable phases (e.g., "docs only"). The explicit when-to-omit
   text mirrors the existing Unknowns pattern, since "optional" sections
   in skills tend to become de-facto required if the model only sees an
   example to fill in.

The corresponding `/implement` change in `skills/implement/SKILL.md`:
step 5's tests-first task derives the test target from a fallback chain
— phase-level Acceptance criteria → plan-level Goals → infer from Steps.
The no-testable-behavior escape hatch covers both implementer-assessed
and plan-declared docs-only phases. Step 2 notes that legacy-format
plans (with `### Files` / `### Testing`) and updated-format plans (with
optional `### Acceptance criteria`) are both valid input.

### Consequences

- **Existing plans in `plans/` are not migrated.** `/implement` reads both
  formats, so the change lands without a flag-day rewrite. This is the
  intentional escape valve.
- The "optional sections become de-facto required" risk is the most likely
  failure mode. If `## Approach` and `### Acceptance criteria` start
  sprouting reflexively on every plan in the next several runs, the next
  iteration tightens the skill text further with explicit negative
  examples of when *not* to include each.
- `/implement`'s test-target derivation is now keyed off the plan rather
  than implementer-time guesswork. Plans that say nothing about phase-level
  done-state fall through to plan-level Goals; the implementer no longer
  has to invent the spec at execution time.
- `/plan` and `/implement` retain a clean ownership split: `/plan` declares
  outcomes, `/implement` chooses mechanism.

## ADR-033: `claude-agent.Dockerfile` omits Claude Code sandbox deps; Colima VM is the isolation boundary

**Date:** 2026-05-02
**Status:** Accepted

### Context

`claude-agent.Dockerfile` originally included the Claude Code bubblewrap sandbox
dependencies (`bubblewrap`, `socat`, `libseccomp2`, `@anthropic-ai/sandbox-runtime`)
on the assumption that the image should mirror `start-claude.sh`'s environment as
closely as possible.

In practice, bubblewrap's sandbox cannot run inside an **unprivileged** Docker
container. `bwrap` uses `CLONE_NEWUSER` (unprivileged user namespaces) on a bare
Linux host, but Docker's default seccomp/AppArmor profile blocks `CLONE_NEWUSER`
inside containers. Making bubblewrap work would require `--cap-add=SYS_ADMIN` or
`--privileged` on the `docker run` call.

`CAP_SYS_ADMIN` is the broadest Linux capability. It enables `mount`/`unmount`,
namespace creation, many device ioctls, cgroup manipulation, `/proc` writes, and
several documented container-escape paths (cgroup v1 `release_agent`, bind-mount
abuse, etc.). Granting it to the container would collapse the trust boundary that
the Colima VM + tinyproxy + iptables chain is designed to provide.

`start-agent.sh` was already force-disabling `sandbox.enabled` in project settings
for exactly this reason, so the sandbox deps were dead weight.

### Decision

Remove `bubblewrap`, `socat`, `libseccomp2` from the `apt-get install` layer and
`@anthropic-ai/sandbox-runtime` from the `npm install -g` layer in
`dockerfiles/claude-agent.Dockerfile`. Add a header comment documenting the
omission and the CAP_SYS_ADMIN rationale.

Keep `start-agent.sh`'s project-settings migration that force-sets
`sandbox.enabled: false`. Claude Code reads settings before checking whether
sandbox binaries are present and would produce an error if the setting were
absent and the default attempted to enable sandboxing.

Also align the `UV_CACHE_DIR` redirect: `start-claude.sh` redirects it to
`${TMPDIR:-/tmp}/uv-cache` to avoid the read-only `/root/.cache` mount that the
bubblewrap sandbox imposes (ADR-001). That constraint does not exist in
`claude-agent` (no sandbox, no read-only cache mount), so `UV_CACHE_DIR` is no
longer redirected in the Dockerfile. `UV_PROJECT_ENVIRONMENT` is still redirected
to `${TMPDIR:-/tmp}/.venv` because the macOS-binary `.venv` leak problem (ADR-007)
applies regardless of sandbox state.

### Consequences

- **Image is slightly smaller** (~6–16 MB, dominated by the npm package).
- **`CAP_SYS_ADMIN` is no longer needed**, preserving the unprivileged-container
  guarantee. The Colima VM + tinyproxy + iptables chain remains the real isolation
  boundary.
- **`UV_CACHE_DIR` defaults to `/root/.cache/uv`** inside the container; this is
  writable and requires no redirect.
- **`start-claude.sh` is unaffected.** That script still installs all four
  sandbox deps and runs with sandbox enabled; the bubblewrap sandbox is the
  correct isolation mechanism inside an Apple Containers microVM.
- Any future attempt to re-enable the sandbox in `claude-agent` must address the
  `CAP_SYS_ADMIN` trade-off explicitly before modifying `start-agent.sh` or
  the Dockerfile.
