# CLAUDE.md

This repo contains tooling for spinning up isolated Claude Code dev containers
using Apple Containers. One script, one container per project.

## Layout

```
start-claude.sh              — Apple Containers path; per-project microVM with Claude Code
start-agent.sh               — Colima path; shared VM + container with Claude Code + OpenCode + Pi + VM-level egress allowlist
research.py                  — Python script; isolated Colima VM + Vane + SearXNG research environment
dockerfiles/                 — Dockerfiles built by start-agent.sh (claude-agent.Dockerfile)
templates/                   — seed templates copied to host state dirs on first run
  global-claude.md                    — seeded to ~/.claude-containers/shared/CLAUDE.md
  global-claude-settings.json         — seeded to settings.json (showThinkingSummaries, coauthorTag, theme, permissions)
  research-denylist-sources.txt       — seeded to ~/.research/denylist-sources.txt by research.py
  research-denylist-additions.txt     — seeded to ~/.research/denylist-additions.txt by research.py
skills/                      — reusable Claude Code skills (back up of ~/.claude/skills/)
skills-agents/               — small-model ports of scope/build/wrap for Pi and OpenCode (seeded to ~/.agents/skills/)
plans/                       — implementation plans written by /scope skill
tests/                       — unit tests and infra smoke tests
  test-agent-firewall.sh               — in-container firewall smoke tests for start-agent.sh (5 of 6 README cases + inter-container port isolation)
  test-cross-vm-isolation.sh           — host-driven cross-VM isolation test: claude-agent ↔ research cannot reach each other
  test_agent_sh.py                     — static checks: no host-port publish, pi integration invariants, sandbox trust-boundary
  test_dockerfile.py                   — static checks: pi and opencode CLI install lines in claude-agent.Dockerfile
  test_research.py                     — unit tests for research.py pure helpers
  test_settings_template.py           — validates global-claude-settings.json is well-formed and git push is absent from allow list
  probe-denylist.sh                    — host-driven Squid denylist end-to-end probe (allow + deny URLs)
  probe-vane-egress.sh                 — smoke test for research-vane egress env vars and sidecar HTTPS round-trip
experiments/                 — archived experiments (not part of CI)
  vane-eval/                           — OFAT eval harness (archived); see experiments/vane-eval/README.md
  model-experiment/                    — raw session transcripts from the model-comparison experiment
README.md                    — usage reference
ADR.md                       — architecture decision records
CLAUDE.md                    — this file
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

- **Single shared image, per-project containers.** One `claude-dev:latest` image built once; each project gets its own named container for state isolation.
- **Image built via `container build` with inline setup.** Setup runs in a temporary `debian:bookworm-slim` container, exported as a tarball, then built via `FROM scratch + ADD rootfs.tar`. (The old `container export --image` flag was removed in v0.11.0.)
- **`container system start` is idempotent.** Always called before any container operation; returns immediately if the service is already running.
- **`container inspect` returns `[]` with exit 0 for missing containers.** Existence check uses string comparison, not exit code.
- **Claude Code installer binary is symlinked into `/usr/local/bin`.** The official installer places `claude` in `~/.local/bin` (not in PATH); the setup script symlinks it so `claude` is available in all shell modes.
- **`UV_CACHE_DIR` resolves dynamically to `${TMPDIR:-/tmp}/uv-cache`.** Avoids the read-only `/root/.cache` and `/tmp` sandbox mounts. See ADR-001, ADR-004.
- **`UV_PROJECT_ENVIRONMENT` redirects venvs to `${TMPDIR:-/tmp}/.venv`.** Prevents macOS-binary `.venv`s from leaking into the Linux container. See ADR-007.
- **`TERM`, `COLORTERM`, and `TERM_PROGRAM` are forwarded into the container.** Without them, Claude Code falls back to 16-color mode.
- **`~/.claude` is shared across all containers via a host volume mount.** `~/.claude-containers/shared/` persists auth, memory, and settings; `claude login` runs once. Run only one container at a time to avoid stomping.
- **`/root/.claude.json` is also persisted as a file bind-mount.** Preserves `oauthAccount` auth state that `~/.claude/.credentials.json` alone doesn't cover. See ADR-006.
- **Skills are synced from the upstream repo on every new-container build.** Per-skill-directory clobber; local-only skills left untouched; fetch failures warn but don't abort. See ADR-005.
- **Global container CLAUDE.md is seeded from `templates/global-claude.md`.** Gives every session shared environment context; in `start-agent.sh`, also seeded to `AGENTS.md` for OpenCode (trailing start-claude section stripped). See ADR-015.
- **Git identity is set via both `~/.gitconfig` and environment variables.** Env vars override gitconfig and work regardless of sandbox mount topology. See ADR-009.
- **`showThinkingSummaries` is enabled in global user settings.** Merged into `~/.claude/settings.json` on startup; makes Claude Code's thinking visible in the transcript.
- **A curated `permissions` allowlist is seeded into fresh settings.json files.** `templates/global-claude-settings.json` carries the full template (showThinkingSummaries, coauthorTag, theme, permissions.allow/deny). Both scripts copy the template when no settings.json exists yet, and add `permissions` non-destructively to existing files that lack it. User edits are never clobbered. See ADR-039.
- **`effortLevel` is intentionally unpinned.** Use `/effort` or project-level `settings.local.json` for situational overrides. See ADR-017.
- **Sandbox is configured in strict mode.** `sandbox.failIfUnavailable: true` and `sandbox.allowUnsandboxedCommands: false` in project `settings.local.json`; migration block adds these to existing files.
- **Theme defaults to `dark-ansi`.** Seeded into fresh settings.json files; existing files with `theme: "auto"` or no `theme` key get promoted to `dark-ansi` by the migration block. Explicit user choices (`dark`, `light`, `dark-daltonized`, etc.) are preserved. `auto` is avoided because OSC 11 background detection misreads Ghostty's mid-gray background as light. See ADR-040.

## start-agent.sh key decisions

`start-agent.sh` is a sibling to `start-claude.sh`, not a replacement. It runs Claude Code, OpenCode, and Pi on top of a single shared Colima VM and a single shared docker container, with a VM-level egress allowlist the in-container LLM cannot modify, and routes local inference to Ollama or omlx on the macOS host.

- **Colima, one shared VM + one sandbox active at a time.** Single `claude-agent` Colima profile; VM launched with `--mount $SANDBOX_ROOT:w` so only the active sandbox is visible inside the VM. Switching sandboxes stops and restarts the VM with the new mount (~10s). Default 8 GiB / 6 CPUs (overridable via `CLAUDE_AGENT_MEMORY` / `CLAUDE_AGENT_CPUS`). See ADR-034.
- **Dockerfile, not an inline heredoc.** Image built from `dockerfiles/claude-agent.Dockerfile` via `docker build` — more readable and cacheable than the `start-claude.sh` inline approach.
- **Egress allowlist via in-VM tinyproxy + CLAUDE_AGENT iptables chain.** tinyproxy runs inside the Colima VM; the CLAUDE_AGENT chain REJECTs all unmatched bridge egress atomically. See ADR-010.
- **Allowlist in `.sandbox_config/`, bind-mounted `:ro` in the container.** `$SANDBOX_ROOT/.sandbox_config/allowlist.txt` seeded on first run; mounted read-only at `/etc/claude-agent/allowlist.txt` so the agent can read but not rewrite it. `--reload-allowlist` applies host-side edits in ~2s without touching the container. See ADR-034.
- **Seed omits write-capable hosts.** tinyproxy can't filter by path or method, so `github.com`, registries, and upload hubs are excluded. Code reads still work via `codeload.github.com` + `githubusercontent.com`.
- **Ollama via host networking.** `HOST_IP` from the VM's default route; container pointed at `http://$HOST_IP:11434` via `OLLAMA_HOST`; iptables RETURN rule carves out that destination.
- **OpenCode inference provider via `opencode.json` injection.** Script writes/migrates a provider entry using `@ai-sdk/openai-compatible`; `ollama` and `omlx` entries coexist; config and data dirs bind-mounted for persistence.
- **Per-mode OpenCode models via `--plan-model`, `--exec-model`, `--small-model`.** Bare IDs prefixed with the active provider key; full `provider/model` strings used as-is.
- **Pi inference provider via `models.json` + `settings.json` injection.** Script writes a `local` provider entry in `$PI_CONFIG_DIR/agent/models.json` (same probe-and-discover logic as opencode) and writes `defaultProvider`/`defaultModel` to `settings.json`. `CLAUDE_AGENT_DEFAULT_MODEL` controls the Pi default; the plan/exec/small env vars are opencode-only. Pi state (auth, model config) is bind-mounted at `/root/.pi`. Cloud providers (Anthropic, OpenAI) are accessed via env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) forwarded from the host — no additional plumbing required.
- **`--backend=omlx` selects omlx as the local inference server.** MLX-based Apple Silicon inference on port 8000 with API-key auth. See ADR-012.
- **Per-sandbox auth and memory state; no cross-script sharing.** Each sandbox's `.sandbox_config/claude/` and `.sandbox_config/claude.json` hold its own Claude Code auth and memory. There is no shared state with `start-claude.sh`; `claude login` must be run once per sandbox. See ADR-034.
- **`--init-sandbox PATH` creates a sandbox directory tree.** Creates `.sandbox_config/`, `projects/`, and all required subdirs at `PATH`; refuses if `.sandbox_config/` already exists. Running `start-agent.sh` outside any sandbox root is a hard error with a remediation message pointing at `--init-sandbox`. See ADR-034.
- **Small-model skill ports seeded on every fresh container.** `skills-agents/{scope,build,wrap}/` are copied into `$SANDBOX/.sandbox_config/agents/skills/` by `seed_agent_skills()`, called from the fresh-container path alongside `sync_skills()`. Both `--rebuild` and `--reset-container` trigger this path; bare reattach skips it. `--init-sandbox` creates the bind-mount target directory only; the first `start-agent.sh` run seeds the skills as a side effect. Local-only skill dirs are left untouched; copy failures warn but do not abort. See ADR-038.
- **`--rebuild` semantics.** Removes image + container non-interactively; Colima VM deletion requires an extra `y` because it wipes the entire VM runtime.
- **`--reset-container` semantics.** Removes the container (and SearXNG container) but keeps the image and VM intact; mutually exclusive with `--rebuild`. Use when only container state needs resetting — avoids network egress for a full image rebuild.
- **`NODE_USE_ENV_PROXY=1` makes Node honor the proxy natively.** Node 24 undici reads `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` when this flag is set — no shim packages needed. See ADR-013.
- **webfetch allowed, websearch denied by default.** `opencode.json` generated with `permission.webfetch: "allow"` and `permission.websearch: "deny"`; webfetch egress bounded by the tinyproxy allowlist.
- **SearXNG-backed websearch runs by default.** `searxng` container started alongside `claude-agent`; fans out through tinyproxy via `outgoing.proxies` (SearXNG silently ignores `HTTPS_PROXY`). See ADR-014. Pass `--disable-search` to skip.
- **`host.docker.internal:host-gateway` set on `docker run`.** Belt-and-suspenders for Colima, where the mapping is not automatic.
- **`docker build` runs with `--network=host`.** Bypasses the DOCKER-USER REJECT rule that blocks `apt-get update` in build-step containers. See ADR-011.
- **Sandbox deps omitted from `claude-agent.Dockerfile`; sandbox force-disabled in project settings.** Bubblewrap inside an unprivileged Docker container requires `CAP_SYS_ADMIN`, which would weaken the Colima VM boundary. The VM-level firewall is the real isolation layer. See ADR-033.
- **`UV_PROJECT_ENVIRONMENT` redirected to `${TMPDIR:-/tmp}/.venv`; `UV_CACHE_DIR` is not.** Unlike `start-claude.sh`, no read-only `/root/.cache` mount is in play — only the macOS-`.venv`-leak problem (ADR-007) requires a redirect here. See ADR-001.

## research.py key decisions

`research.py` is a Python (stdlib-only) orchestrator for a dedicated Colima VM (`research` profile) hosting Vane and its own SearXNG instance, network-isolated from `claude-agent`. Default-allow denylist model, contrasting with `start-agent.sh`'s default-deny allowlist. See ADR-018.

- **Separate Colima profile (`research`) for VM-level isolation.** Independent VM, iptables chain, docker bridge, and container namespace from `claude-agent`; both can run simultaneously. Port 3000 is the only potential host-level conflict.
- **Dedicated SearXNG instance, not shared with `claude-agent`.** `research-searxng` on `research-net`, routing through the `research` VM's Squid proxy; no interaction with `claude-agent`'s SearXNG instance.
- **Denylist (default-allow + blocked domains) via Squid.** Vane reaches arbitrary search-result URLs without pre-approving destinations; Squid's O(1) `dstdomain` ACL handles million-entry denylists. See ADR-021, ADR-023.
- **Port 3000 on the macOS host for Vane.** `research-vane` exposes `http://localhost:3000`. See ADR-028.
- **LLM inference via `host.docker.internal`.** Configured once via the Vane UI; iptables `RESEARCH` chain has a RETURN rule for `$HOST_IP:$INFERENCE_PORT`; traffic goes direct, not through Squid.
- **Denylist seeds in `templates/`.** `research-denylist-sources.txt` and `research-denylist-additions.txt` seeded to `~/.research/` on first run; composed denylist = `(upstream ∪ additions) − overrides`. Use `--reseed-denylist` to pick up template updates. See ADR-023.
- **Hagezi `wildcard/<list>-onlydomains.txt` format.** One apex/domain per line; prefixed with `.` for Squid `dstdomain` suffix-match. `domains/` files exhaustively list subdomains but miss the apex. See ADR-025.
- **Auto-prunes orphan files in `denylist-cache/`.** `prune_orphan_cache_files()` deletes stale `.txt` files left by URL or SHA changes in `denylist-sources.txt`. Called on every refresh and reload. See ADR-026.
- **Hard-exits if `~/.research/allowlist.txt` is detected.** Legacy installations must `rm -rf ~/.research/` then `--rebuild`; no automatic migration. See ADR-022.
- **Vane container wired through Squid via `HTTP_PROXY` and `HTTPS_PROXY`.** `NO_PROXY` exempts SearXNG and `host.docker.internal`. See ADR-029.

## Commit style

Do NOT include `Co-Authored-By` lines in commit messages.

## Making changes

The setup script is embedded as a `bash -c '...'` heredoc inside
`start-claude.sh`. Edit it there. After changing it, run with `--rebuild` to
apply the changes:

```bash
start-claude.sh --rebuild
```

This removes the existing project container (if any) and the `claude-dev:latest`
image, then rebuilds from scratch.

For `start-agent.sh`, the image is built from
`dockerfiles/claude-agent.Dockerfile`. Edit the Dockerfile for image-level
changes; edit `start-agent.sh` for host-side orchestration, firewall, or
allowlist-handling changes. After either, run:

```bash
start-agent.sh --rebuild
```

which removes `claude-agent:latest` and the container, then rebuilds. An
additional confirmation prompt offers to delete the Colima VM too — only
say yes if you want to start over from a clean VM (loses everything else
inside the VM's docker runtime).

### Migrating from the legacy `~/.claude-agent/` layout

If you were using `start-agent.sh` before the sandbox redesign, your state
lives in `~/.claude-containers/` and `~/.claude-agent/`. The new script
ignores those directories; follow this recipe to migrate:

```bash
start-agent.sh --init-sandbox ~/sandboxes/default
cp -r ~/.claude-containers/shared/* ~/sandboxes/default/.sandbox_config/claude/
cp ~/.claude-containers/claude.json ~/sandboxes/default/.sandbox_config/claude.json
cp -r ~/.claude-agent/opencode-config/* ~/sandboxes/default/.sandbox_config/opencode/config/
cp -r ~/.claude-agent/opencode-data/*   ~/sandboxes/default/.sandbox_config/opencode/data/
cp ~/.claude-agent/allowlist.txt        ~/sandboxes/default/.sandbox_config/allowlist.txt
# Move repos in:
mv ~/Code/my-repo ~/sandboxes/default/projects/my-repo
# Once verified, remove legacy state:
# rm -rf ~/.claude-containers ~/.claude-agent
```

For `research.py`, the script is a single Python file at the repo root. Edit it
directly. After changing it:

```bash
./research.py --rebuild
```

This removes the `research-vane` and `research-searxng` containers and recreates
them. An additional confirmation prompt offers to delete the `research` Colima VM
too — only say yes if you want to start from a completely clean state.
