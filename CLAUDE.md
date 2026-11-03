# CLAUDE.md

This repo contains tooling for spinning up isolated Claude Code dev containers
using Apple Containers. One script, one container per project.

## Layout

```
start-claude.sh              — Apple Containers path; per-project microVM with Claude Code
start-agent.sh               — Colima path; shared VM + container with Claude Code + OpenCode + Pi + VM-level egress allowlist
research.py                  — Python script; isolated Colima VM + Vane + SearXNG research environment
merge_global_settings.py     — shared helper both scripts run on the host to merge the template into ~/.claude/settings.json (stdlib-only, Python 3.9-safe)
dockerfiles/                 — Dockerfiles built by start-agent.sh (claude-agent.Dockerfile)
templates/                   — seed templates copied to host state dirs on first run
  global-claude.md                    — seeded to ~/.claude-containers/shared/CLAUDE.md
  global-claude-settings.json         — source for settings.json (showThinkingSummaries, attribution, theme, prefersReducedMotion, permissions); applied by merge_global_settings.py
  sandbox-allowlist.txt               — shared egress allowlist: seeded to the sandbox's allowlist.txt by start-agent.sh and to sandbox.network.allowedDomains in .claude/settings.local.json by start-claude.sh
  research-denylist-sources.txt       — seeded to ~/.research/denylist-sources.txt by research.py
  research-denylist-additions.txt     — seeded to ~/.research/denylist-additions.txt by research.py
skills/                      — reusable Claude Code skills (back up of ~/.claude/skills/)
skills-agents/               — small-model ports of scope/build/wrap for Pi and OpenCode (seeded to ~/.agents/skills/)
plans/                       — implementation plans written by /scope skill
scripts/                     — host-side maintenance scripts
  bump-searxng.sh                      — moves start-agent.sh's SEARXNG_IMAGE pin to the current upstream digest (pulled in the claude-agent VM); no commit
tests/                       — unit tests and infra smoke tests
  test-agent-firewall.sh               — in-container firewall smoke tests for start-agent.sh (all README cases but hot-reload: egress, inference proxy allow/deny and no direct host path, real key absent, DNS blackhole, VM-local services)
  test-cross-vm-isolation.sh           — host-driven cross-VM isolation test: claude-agent ↔ research cannot reach each other; claude-agent reaches the inference proxy, research reaches the host inference port
  test_agent_sh.py                     — static checks: no host-port publish, pi integration invariants, sandbox trust-boundary
  test_dockerfile.py                   — static checks: pi and opencode CLI install lines in claude-agent.Dockerfile, TOOLS_CACHE_BUST arg right before them
  test_tool_refresh.py                 — --refresh-tools/--restore-previous-image exclusions and image lifecycle (fake docker), tool-version record, bump-searxng.sh offline
  test_research.py                     — unit tests for research.py pure helpers
  test_settings_template.py           — validates global-claude-settings.json is well-formed and git push is absent from allow list
  test_global_settings_helper.py       — unit tests for merge_global_settings.py: CLI contract, forced/merged/seeded rules, deny rules vs. commands
  probe-deny-rules.py                  — runs the real `claude -p` against a mock Messages API on 127.0.0.1 with PATH shims (no account) to check the deny rules; logs to .verify/
  test_file_modes.py                   — entry-point scripts keep their executable bit (in-place edits on the bind mount can drop it)
  test_sandbox_allowlist.py           — validates sandbox-allowlist.txt is well-formed and that start-claude.sh injects allowedDomains with *.d expansion
  probe-denylist.sh                    — host-driven Squid denylist end-to-end probe (allow + deny URLs)
  probe-vane-egress.sh                 — smoke test for research-vane egress env vars and sidecar HTTPS round-trip
  probe-tinyproxy-owner-rule.sh        — host-driven live check of CLAUDE_AGENT_OUT's owner-match rule (tinyproxy user refused, normal user as control)
  probe-vm-ipv6.sh                     — host-driven check of whether tinyproxy has an IPv6 path out of the VM or to the Mac (CLAUDE_AGENT_OUT is IPv4 only)
  verify-omlx-key-handling.sh          — host-driven live check that a keyless `--reload-allowlist` keeps the proxy's backend and omlx key, and that the startup probe's keyless curl reaches omlx; log in .verify/
  verify-tool-refresh.sh               — live acceptance check after a SearXNG bump or --refresh-tools: SearXNG results from 2+ engines, opencode config + MCP, `opencode run` on omlx gemma-4-e4b-it-6bit calling the searxng tool, layer reuse, build-cache probe; runs on the host or in the container; log in .verify/
experiments/                 — archived experiments (not part of CI)
  vane-eval/                           — OFAT eval harness (archived); see experiments/vane-eval/README.md
  model-experiment/                    — raw session transcripts from the model-comparison experiment
README.md                    — usage reference
ADR.md                       — architecture decision records
CLAUDE.md                    — this file
.verify/                     — gitignored logs from probes and human-check scripts
```

## What the script does

`start-claude.sh` sets up a `claude-dev:latest` image on first run (cached after
that), then creates and attaches a named container with:

- The project directory mounted at its host path (not `/workspace`)
- Node LTS, Claude Code CLI (via official installer), uv/uvx, git, ripgrep, fd, jq, unzip, zip
- bubblewrap, socat, libseccomp2/dev, `@anthropic-ai/sandbox-runtime` (Claude Code sandbox dependencies)

It also starts the container service automatically (`container system start`) so
the script works even if the service isn't already running.

If the named container already exists, it just starts and re-attaches it.

## Key decisions

- **Single shared image, per-project containers.** One `claude-dev:latest` image built once; each project gets its own named container for state isolation.
- **Image built via `container build` with inline setup.** Setup runs in a temporary `debian:bookworm-slim` container, exported as a tarball, then built via `FROM scratch + ADD rootfs.tar`. (The old `container export --image` flag was removed in v0.11.0.)
- **`container system start` is idempotent.** Always called before any container operation; returns immediately if the service is already running.
- **Container existence is checked via a version-robust `container_exists()` helper.** `container inspect`'s missing-container contract changed across Apple Containers major versions: older builds exit 0 with stdout `[]`; the v-major release exits non-zero with the error on stderr and empty stdout. The helper treats a container as present only when inspect succeeds **and** prints a non-empty, non-`[]` payload — covering all three forms. See ADR-042.
- **Claude Code installer binary is symlinked into `/usr/local/bin`.** The official installer places `claude` in `~/.local/bin` (not in PATH); the setup script symlinks it so `claude` is available in all shell modes.
- **`UV_CACHE_DIR` resolves dynamically to `${TMPDIR:-/tmp}/uv-cache`.** Avoids the read-only `/root/.cache` and `/tmp` sandbox mounts. See ADR-001, ADR-004.
- **`UV_PROJECT_ENVIRONMENT` redirects venvs to `${TMPDIR:-/tmp}/.venv`.** Prevents macOS-binary `.venv`s from leaking into the Linux container. See ADR-007.
- **`TERM`, `COLORTERM`, and `TERM_PROGRAM` are forwarded into the container.** Without them, Claude Code falls back to 16-color mode.
- **`~/.claude` is shared across all containers via a host volume mount.** `~/.claude-containers/shared/` persists auth, memory, and settings; `claude login` runs once. Run only one container at a time to avoid stomping.
- **`/root/.claude.json` is also persisted as a file bind-mount.** Preserves `oauthAccount` auth state that `~/.claude/.credentials.json` alone doesn't cover. See ADR-006.
- **Skills are synced from the upstream repo on every new-container build.** Per-skill-directory clobber; local-only skills left untouched; fetch failures warn but don't abort. See ADR-005.
- **Global container CLAUDE.md is seeded from `templates/global-claude.md`.** Gives every session shared environment context; in `start-agent.sh`, also seeded to `AGENTS.md` for OpenCode (trailing start-claude section stripped). See ADR-015.
- **Git identity is set via both `~/.gitconfig` and environment variables.** Env vars override gitconfig and work regardless of sandbox mount topology. See ADR-009.
- **One shared helper merges global `~/.claude/settings.json`.** Both scripts run `merge_global_settings.py SETTINGS TEMPLATE` with the host's `python3` (stdlib-only, 3.9-safe). A missing file starts from `templates/global-claude-settings.json`, and the same rules then apply to every file. If the helper fails, the script warns and keeps launching. Each key gets one treatment: **forced** every run (`showThinkingSummaries: true`, `attribution: {"commit": "", "pr": ""}`, `prefersReducedMotion: true`), **removed** (`effortLevel`, the stale `coauthorTag`), **promoted** (`theme`, see below), **seeded** if absent (the whole `permissions` block), and **merged** (the template's push/publish `permissions.deny` entries are added if missing; the user's other allow/ask/deny entries are untouched). `attribution` uses the object form without `sessionUrl` because older CLIs sharing `~/.claude` skip a settings file that holds `false` or `sessionUrl`. See ADR-051.
- **A curated `permissions` allowlist is seeded into fresh settings.json files.** The template's `allow` list pre-approves dev-loop commands; its `deny` list blocks push and publish paths (`git push` including `-C`/`-c`/`--git-dir`/`--work-tree` forms, git alias definitions, npm/yarn/pnpm/cargo/uv publish, `twine upload` however launched, `docker push`/`docker image push`, `gh` write commands). Rules use the `Bash(x*)` form. They are defense in depth: `github.com` is already off both egress allowlists. See ADR-039, ADR-051.
- **`effortLevel` is intentionally unpinned.** Use `/effort` or project-level `settings.local.json` for situational overrides. See ADR-017.
- **Sandbox is configured in strict mode.** `sandbox.failIfUnavailable: true` and `sandbox.allowUnsandboxedCommands: false` in project `settings.local.json`; migration block adds these to existing files.
- **A default-deny `sandbox.network` allowlist is seeded into every `settings.local.json`.** `templates/sandbox-allowlist.txt` (bare domains, comments) is the source of truth; `start-claude.sh` reads it and expands each domain `d` to `d` and `*.d` when building `sandbox.network.allowedDomains`. The same file seeds `start-agent.sh`'s allowlist (Anthropic, package registries, reference/research hosts; ADR-049) but omits write-capable hosts (`github.com`, container registries, `huggingface.co`). Migration: seed-if-absent — existing files with a `network` key are left untouched (user-pruned entries stay pruned); `--reseed-sandbox-allowlist` forces overwrite. This is a **guardrail, not a hard boundary**: `settings.local.json` is writable by the agent; the microVM itself retains full unrestricted egress at the VM level. See ADR-044.
- **Container runs with `--cap-add SYS_ADMIN` and `--cap-add NET_ADMIN` so the bubblewrap sandbox works.** `SYS_ADMIN` is needed so `bwrap` can create nested namespaces — Apple Containers' seccomp profile gates `clone(CLONE_NEW*)` on it. `NET_ADMIN` is needed so bwrap can configure the loopback interface in the new network namespace it creates per command (`RTM_NEWADDR` on `lo` fails without it). Safe here because each container is its own microVM, so both caps empower the process only within its dedicated VM, not against the macOS host. See ADR-043.
- **`@anthropic-ai/sandbox-runtime` is version-pinned, and `sandbox.enableWeakerNestedSandbox` is forced on.** Pinned because it is a security dependency whose bwrap invocation changes between releases: unpinned, `--rebuild` silently imported 0.0.66 (2026-07-17), which added `--unshare-user --cap-drop ALL` to the secure path. As root under Apple Containers' masked `/proc`, that cannot start at all — a fresh procfs mount from a non-initial userns requires a fully-visible procfs, and the dropped caps also break `apply-seccomp`'s nested-userns `uid_map` write. Weaker mode takes the `--unshare-user --bind /proc /proc` branch instead. It is *stronger* than the pre-0.0.66 behaviour it replaces (the child userns makes inherited mounts `MNT_LOCKED`, so `/` can no longer be remounted rw, and `/proc/<pid>/environ` is no longer readable); it concedes only that sandboxed commands can enumerate other processes in their own container. Network isolation is unaffected. Bump the pin deliberately, and re-test. See ADR-047.
- **Theme defaults to `dark-ansi`.** Seeded into fresh settings.json files; existing files with no `theme` key get `dark-ansi`. Every explicit choice, `auto` included, is preserved, so picking `auto` via `/theme` survives `--rebuild`. See ADR-040, ADR-052.

## start-agent.sh key decisions

`start-agent.sh` is a sibling to `start-claude.sh`, not a replacement. It runs Claude Code, OpenCode, and Pi on top of a single shared Colima VM and a single shared docker container, with a VM-level egress allowlist the in-container LLM cannot modify, and routes local inference to Ollama or omlx on the macOS host through a default-deny proxy in the VM.

- **Colima, one shared VM + one sandbox active at a time.** Single `claude-agent` Colima profile; VM launched with `--mount $SANDBOX_ROOT:w` so only the active sandbox is visible inside the VM. Switching sandboxes stops and restarts the VM with the new mount (~10s). Default 8 GiB / 6 CPUs (overridable via `CLAUDE_AGENT_MEMORY` / `CLAUDE_AGENT_CPUS`). See ADR-034.
- **Dockerfile, not an inline heredoc.** Image built from `dockerfiles/claude-agent.Dockerfile` via `docker build` — more readable and cacheable than the `start-claude.sh` inline approach.
- **Egress allowlist via in-VM tinyproxy + CLAUDE_AGENT / CLAUDE_AGENT_IN / CLAUDE_AGENT_OUT iptables chains.** tinyproxy runs inside the Colima VM; CLAUDE_AGENT (from DOCKER-USER, FORWARD) REJECTs all unmatched container egress, CLAUDE_AGENT_IN (from INPUT) REJECTs container traffic to VM-local services other than tinyproxy and the inference proxy (sshd, dnsmasq), and CLAUDE_AGENT_OUT (from OUTPUT) REJECTs new TCP connections from the `tinyproxy` user to `$HOST_IP` and the private ranges, since tinyproxy's traffic crosses neither container chain. Its first rule RETURNs ESTABLISHED,RELATED, because tinyproxy's replies to containers also leave through OUTPUT and container subnets sit inside `172.16.0.0/12`. All are replaced in one `iptables-restore --noflush` commit, so none is empty mid-apply. CLAUDE_AGENT_OUT6 does the same job for IPv6, refusing tinyproxy everything but loopback, in its own `ip6tables-restore` commit at the end of the firewall script (the VM shares a private IPv6 prefix with the Mac; see ADR-050). The agent container is created with `--dns 192.0.2.1` (blackhole), so external names do not resolve; reattach warns if an older container lacks it. See ADR-010.
- **Allowlist in `.sandbox_config/`, bind-mounted `:ro` in the container.** `$SANDBOX_ROOT/.sandbox_config/allowlist.txt` seeded on first run from `templates/sandbox-allowlist.txt` (shared with `start-claude.sh`), written in place so the bind mount keeps its inode; mounted read-only at `/etc/claude-agent/allowlist.txt` so the agent can read but not rewrite it. `--reload-allowlist` applies host-side edits in ~2s without touching the container. Lines that are not bare domains (URLs, paths, ports, single labels) are skipped with a warning. See ADR-034, ADR-049.
- **Warm reattach issues one SSH handshake (batch probe) plus one for firewall.** All read-only VM facts (bridge network, host IP, agent-net CIDR, tinyproxy install/active status, stored config hash) are gathered in a single `colima ssh` call via `vm-probe.sh`. Tinyproxy config is pushed only when its SHA-256 hash changed or the service is inactive; unchanged warm paths skip the push and reload entirely. The inference probe runs as a background job so its 3-second timeout overlaps later work. See ADR-045.
- **Seed omits write-capable hosts.** tinyproxy can't filter by path or method, so `github.com`, registries, and upload hubs are excluded. Code reads still work via `codeload.github.com` + `githubusercontent.com`.
- **Ollama via host networking, behind the inference proxy.** `HOST_IP` from the VM's default route is the proxy's upstream only; the container is pointed at `http://$BRIDGE_IP:11434` via `OLLAMA_HOST`. There is no iptables carve-out to the host any more.
- **Inference goes through a default-deny nginx proxy in the VM.** nginx listens on `$BRIDGE_IP:$INFERENCE_PORT` and is the only inference route: exact-match path-plus-method allowlist per backend (omlx: chat completions, `GET /v1/models`, `/v1/messages`, `/v1/messages/count_tokens`; Ollama: chat completions, `GET /v1/models`, `GET /api/tags`), catch-all 403, fixed upstream paths. It holds the omlx key and overwrites client credentials, so `OMLX_API_KEY` is never forwarded into the container and the OpenCode and Pi configs carry a placeholder `apiKey`. The config push is hash-gated and fails closed (catch-all check, `nginx -t`). tinyproxy is blocked from the host and private ranges (IPv4 only). An existing container keeps its create-time key: reattach warns and names `--reset-container`. See ADR-050.
- **The inference backend is sticky, and a keyless run keeps the proxy's omlx key.** `--backend`, then `CLAUDE_AGENT_BACKEND`, then the backend the installed proxy serves, then `ollama`; a run without `OMLX_API_KEY` carries the installed key over (the VM probe reports yes/no only). The startup omlx probe sends no key and treats any HTTP answer as reachable. See ADR-053.
- **OpenCode inference provider via `opencode.json` injection.** Script writes/migrates a provider entry using `@ai-sdk/openai-compatible`; `ollama` and `omlx` entries coexist; config and data dirs bind-mounted for persistence. Every model is declared image-capable (here and for Pi), since discovery can't tell and both clients otherwise drop images; `autoupdate` is forced off, because OpenCode's own major upgrade leaves no working binary under npm's allow-scripts.
- **Per-mode OpenCode models via `--plan-model`, `--exec-model`, `--small-model`.** Bare IDs prefixed with the active provider key; full `provider/model` strings used as-is.
- **Pi inference provider via `models.json` + `settings.json` injection.** Script writes a `local` provider entry in `$PI_CONFIG_DIR/agent/models.json` (same probe-and-discover logic as opencode) and writes `defaultProvider`/`defaultModel` to `settings.json`. `CLAUDE_AGENT_DEFAULT_MODEL` controls the Pi default; the plan/exec/small env vars are opencode-only. Pi state (auth, model config) is bind-mounted at `/root/.pi`. Cloud providers (Anthropic, OpenAI) are accessed via env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) forwarded from the host — no additional plumbing required.
- **`--backend=omlx` selects omlx as the local inference server.** MLX-based Apple Silicon inference on port 8000 with API-key auth; the key is held by the inference proxy, not the container. See ADR-012, ADR-050.
- **Per-sandbox auth and memory state; no cross-script sharing.** Each sandbox's `.sandbox_config/claude/` and `.sandbox_config/claude.json` hold its own Claude Code auth and memory. There is no shared state with `start-claude.sh`; `claude login` must be run once per sandbox. See ADR-034.
- **`--init-sandbox PATH` creates a sandbox directory tree.** Creates `.sandbox_config/`, `projects/`, and all required subdirs at `PATH`; refuses if `.sandbox_config/` already exists. Running `start-agent.sh` outside any sandbox root is a hard error with a remediation message pointing at `--init-sandbox`. See ADR-034.
- **Small-model skill ports seeded on every fresh container.** `skills-agents/{scope,build,wrap}/` are copied into `$SANDBOX/.sandbox_config/agents/skills/` by `seed_agent_skills()`, called from the fresh-container path alongside `sync_skills()`. Both `--rebuild` and `--reset-container` trigger this path; bare reattach skips it. `--init-sandbox` creates the bind-mount target directory only; the first `start-agent.sh` run seeds the skills as a side effect. Local-only skill dirs are left untouched; copy failures warn but do not abort. See ADR-038.
- **`--rebuild` semantics.** Removes image + container non-interactively; Colima VM deletion requires an extra `y` because it wipes the entire VM runtime.
- **`--reset-container` semantics.** Removes the container (and SearXNG container) but keeps the image and VM intact; mutually exclusive with `--rebuild`. Use when only container state needs resetting — avoids network egress for a full image rebuild.
- **`--refresh-tools` / `--restore-previous-image`.** OpenCode and Pi stay at `@latest`; refresh tags `claude-agent:latest` as `:previous`, rebuilds with a fresh `TOOLS_CACHE_BUST` build arg (declared right before the npm `RUN`, so only that layer onward reruns), recreates both containers after a successful build, and prints old → new versions. Restore retags `previous` → `latest` and recreates. Both exclude `--rebuild`, `--reset-container` and the allowlist flags. Versions are recorded per image ID in `.sandbox_config/tool-versions` by a `--network none` throwaway container, never on a warm reattach. See ADR-054.
- **`NODE_USE_ENV_PROXY=1` makes Node honor the proxy natively.** Node 24 undici reads `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` when this flag is set — no shim packages needed. See ADR-013.
- **webfetch allowed, websearch denied by default.** `opencode.json` generated with `permission.webfetch: "allow"` and `permission.websearch: "deny"`; webfetch egress bounded by the tinyproxy allowlist.
- **One network topology.** The agent container always joins `claude-agent-net`, whatever the search flag says; `--disable-search` only controls SearXNG, the `8080` RETURN rule, and the MCP block. Older bridge-only containers are joined to the network on reattach via `docker network connect` (never `disconnect`). See ADR-048.
- **Start failures: agent container reports, SearXNG self-heals.** `attach_existing` checks `docker start` and `.State.Running`; on failure it prints Docker's stderr and exits naming `--reset-container` (never auto-removes: the container may hold user-installed tools). A SearXNG container that fails to start is `docker rm -f`'d and recreated. Neither gets a restart policy: dockerd would start them at VM boot, before the firewall is installed. See ADR-048.
- **SearXNG-backed websearch runs by default.** `searxng` container started alongside `claude-agent`; fans out through tinyproxy via `outgoing.proxies` (SearXNG silently ignores `HTTPS_PROXY`). See ADR-014. Pass `--disable-search` to skip. The image is pinned by digest in `SEARXNG_IMAGE`; bump it with `scripts/bump-searxng.sh`, verify with `tests/verify-tool-refresh.sh`, then commit. A searxng container on another image is recreated on any launch, so a bump or its revert takes effect without `--reset-container` (ADR-048, ADR-054).
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
- **Warm reattach batches read-only VM probes and hash-gates the Squid push.** All read-only facts (bridge gateway+subnet, host IP, research-net CIDR, squid install/active status, hashlimit availability, stored config hash) are gathered in one `colima ssh` call via `probe_vm`. The Squid config + denylist are pushed and the daemon reloaded (`squid -k reconfigure`, not `restart`) only when their SHA-256 (`squid_config_hash`) changed or Squid is inactive; an unchanged warm reattach skips the push and reload entirely. The inference probe runs on a background thread so its 3-second timeout overlaps container bring-up. The iptables RESEARCH chain is still rebuilt on every attach. See ADR-046.
- **Hard-exits if `~/.research/allowlist.txt` is detected.** Legacy installations must `rm -rf ~/.research/` then `--rebuild`; no automatic migration. See ADR-022.
- **Vane container wired through Squid via `HTTP_PROXY` and `HTTPS_PROXY`.** `NO_PROXY` exempts SearXNG and `host.docker.internal`. See ADR-029.
- **Self-healing container start via `start_or_recreate`.** If `docker start` returns non-zero or the container is not `Running` immediately after, the script prints a visible warning (including captured stderr), `docker rm -f`s the broken container, and recreates it. Covers both the `RWLayer nil` dangling-layer case (hard non-zero exit) and crash-on-boot (zero exit but exits immediately). Both containers also get `--restart unless-stopped` so transient crashes self-recover; an explicit `docker stop` still sticks. See ADR-041.
- **Script-controlled Vane SearXNG URL (config.json authority).** On every bring-up, `ensure_vane_searxng_url` patches `~/.research/vane-data/data/config.json`'s `search.searxngURL` to `http://research-searxng:8080` if it differs. Vane ignores the `SEARXNG_API_URL` env var (which is removed); config.json is the authoritative source. The patch preserves all other fields; parse failures warn rather than clobber. See ADR-041.

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
