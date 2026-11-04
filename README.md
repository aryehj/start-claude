# TL; DR
The author is a dilletente who starts a lot of projects, but is slow to learn syntax and commands. Therefore, the author automated most of the creation of safe-ish containerized workspaces for LLM-aided development in a given working directory. 

# start-claude.sh

Spins up an isolated [Apple Containers](https://developer.apple.com/documentation/virtualization)
dev environment for a project, pre-configured for Claude Code.

## Requirements

- macOS with Apple Containers installed (`container` CLI on PATH)
- Apple Silicon Mac
- Kata kernel installed: `container system kernel set --recommended`
- Rosetta 2 installed: `softwareupdate --install-rosetta --agree-to-license`

## Setup

```bash
# Optional: Make it available everywhere
echo 'export PATH="/Path/To/start-claude:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

## Usage

```bash
# From inside your project directory
start-claude.sh

# Explicit project dir
start-claude.sh ~/projects/my-app

# Explicit project dir + container name
start-claude.sh ~/projects/my-app my-app

# Custom git identity for commits
start-claude.sh --git-name "Jane" --git-email "jane@example.com"

# Equals form also works
start-claude.sh --git-name=Jane --git-email=jane@example.com ~/projects/my-app

# Overwrite ~/.claude-containers/shared/CLAUDE.md with the current template
start-claude.sh --reseed-global-claudemd
```

The script starts the container service automatically if it isn't already
running, so no manual `container system start` is needed beforehand.

On first run, the script pulls `debian:bookworm-slim`, installs tools inside a
temporary container, then exports it as `claude-dev:latest` — takes a few
minutes. Subsequent projects reuse the cached image and start instantly.

If you run the command again for a project that already has a container, it
re-attaches to the existing one rather than creating a new one.

## What's inside the container

| Tool | Notes |
|------|-------|
| `claude` | Claude Code CLI (installed via official installer) |
| `uv` / `uvx` | Python package manager |
| `node` / `npm` | Node.js LTS |
| `git` | Latest from apt |
| `ripgrep` | `rg` |
| `fd` | `fd-find` |
| `jq` | JSON CLI |
| `curl` / `wget` | |
| `unzip` / `zip` | Archive tools |
| `python3` | System Python |

## Mounts

| Host | Container |
|------|-----------|
| Your project dir | Same path (e.g. `/Users/you/projects/my-app`) |
| `~/.claude-containers/shared/` | `/root/.claude` |
| `~/.claude-containers/claude.json` | `/root/.claude.json` |

**Authentication note:** `~/.claude` and `~/.claude.json` are shared across all
containers via the host volume mounts above. Run `claude login` once in any
container; all containers share the session, and auth survives `--rebuild`.

## Rebuilding the image

To rebuild from scratch (e.g. after editing the setup script in
`start-claude.sh`), use the `--rebuild` flag:

```bash
start-claude.sh --rebuild
```

This removes the existing container for the project (if any) and the
`claude-dev:latest` image, then rebuilds from scratch.

## Update warnings

On launch, `start-claude.sh` prints one line when npm has had a
`@anthropic-ai/sandbox-runtime` release more than 30 days newer than the
version pinned in the setup script, for example:

```
warning: sandbox-runtime 0.0.81 is 41 days behind npm latest 0.0.90 (released 2026-11-30). Update: bump the pin in start-claude.sh, run start-claude.sh --rebuild, then tests/verify-sandbox-runtime.sh
```

The pin is a security dependency (ADR-047), so the update stays a deliberate
step: bump the pin, `--rebuild`, re-test. The check runs on the host through
the shared helper `check_versions.py`. A detached background fetch asks npm
at most once a day and caches the answer in
`~/.claude-containers/version-check.json`; the line itself comes from that
cache just before attaching, so it never delays a launch and appears on the
launch after the fetch that saw the release. Any error prints nothing. Set
`CLAUDE_CONTAINER_VERSION_LAG_DAYS` to change the threshold, or
`CLAUDE_CONTAINER_DISABLE_VERSION_CHECK=1` to turn it off. See `ADR.md`
§ADR-055.

## Global container CLAUDE.md

On first run, the script copies `templates/global-claude.md` from the repo
into `~/.claude-containers/shared/CLAUDE.md`. Claude Code auto-injects that
file into every session running inside any container, giving the model shared
context about the environment (path layout, `$TMPDIR`, sandbox mounts, etc.)
regardless of the project it's opened in. Your edits are preserved across
subsequent runs. Pass `--reseed-global-claudemd` to overwrite with the current
template.

## Claude Code permission allowlist

On first run, `start-claude.sh` copies `templates/global-claude-settings.json`
into `~/.claude-containers/shared/settings.json`. The template pre-approves
common dev-loop operations (file reads/edits, git status/add/commit, build and
test commands, standard shell utilities) and explicitly denies remote-affecting
ops (`git push`, `npm publish`, etc.).

On every run, the shared helper `merge_global_settings.py` updates the file
(ADR-051):

- **Forced** every run: `showThinkingSummaries: true`,
  `attribution: {"commit": "", "pr": ""}` (no Co-Authored-By trailer or PR
  footer), and `prefersReducedMotion: true`. `effortLevel` and the stale
  `coauthorTag` key are removed.
- **Merged** every run: the template's push/publish deny rules are added to
  `permissions.deny` if missing. Your own allow, ask, and deny entries are left
  alone.
- **Seeded** only when absent: the whole `permissions` block. `theme` is set to
  `dark-ansi` when unset; any explicit choice, `auto` included, is kept.

If the helper fails (for example, the file is not valid JSON), the script warns
and launches with the file as-is. Your customizations survive `--rebuild`.

## Sandbox network allowlist

On every run, `start-claude.sh` seeds `sandbox.network.allowedDomains` into the
project's `.claude/settings.local.json`. The default list covers Anthropic,
common package registries (npm, PyPI, crates.io, etc.), and a broad set of
reference, research, and documentation hosts. It is sourced from
`templates/sandbox-allowlist.txt` in this repo; each bare domain `d` is expanded
to both `d` (exact) and `*.d` (all subdomains). The same file seeds
`start-agent.sh`'s allowlist.

**Sandboxed bash commands** (`curl`, `git`, `uv`, etc.) can only reach hosts on
this list — all others are blocked at the kernel level (network namespace +
proxy). The restriction does **not** affect Claude Code's own API traffic, which
runs outside the sandbox.

**`github.com` is omitted by default.** HTTPS `git push` (and other write
operations) to GitHub are blocked in the default config. To re-enable for a
project:

```json
// .claude/settings.local.json
{
  "sandbox": {
    "network": {
      "allowedDomains": ["github.com", "*.github.com", "...other entries..."],
      "deniedDomains": []
    }
  }
}
```

Or add just the hosts you need and re-run `start-claude.sh --reseed-sandbox-allowlist` only when you want to reset the whole list to the current template (this overwrites any per-project customizations to `allowedDomains`).

This is a **guardrail, not a hard boundary**: `settings.local.json` is writable
by the agent. The microVM itself has full unrestricted egress at the VM level.

## Included skills

The `skills/` directory holds reusable Claude Code skills. Whenever
`start-claude.sh` creates a new container, it downloads the upstream repo
archive and injects each skill directory into the shared
`~/.claude-containers/shared/skills/` mount, replacing any existing directory
with the same name. Skills you've added locally under other names are left
alone.

Override the source with `CLAUDE_SKILLS_ARCHIVE_URL` (point it at a fork, a
branch tarball, or any `*.tar.gz` whose top-level has a `skills/` directory).
If the fetch fails, the warning is printed and the container starts anyway.

Invoke a synced skill inside any Claude Code session with its slash name, e.g.
`/wrap`.

| Skill | What it does |
|-------|-------------|
| `scope` | Explores the codebase and writes implementation plans to `plans/` as markdown files (runs on Opus) |
| `build` | Executes the active phase of a plan file, with task tracking and checkpoint commits |
| `wrap` | Post-implementation housekeeping plus a light review pass — updates CLAUDE.md, README.md, appends ADR.md, renames completed plan files, and evaluates the diff for obvious bugs / legibility / coverage / maintainability |

## Environment variable reference

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_CONTAINER_IMAGE` | `debian:bookworm-slim` | Override the base image |
| `CLAUDE_CONTAINER_MEMORY` | `4G` | Per-container memory limit passed to `container run --memory` |
| `CLAUDE_CONTAINER_CPUS` | `4` | Per-container CPU count passed to `container run --cpus` |
| `CLAUDE_CONTAINER_VERSION_LAG_DAYS` | `30` | Warn at launch when npm has a sandbox-runtime release more than this many days newer than the pin |
| `CLAUDE_CONTAINER_DISABLE_VERSION_CHECK` | *(unset)* | Set to `1` to turn the launch-time update check off |
| `CLAUDE_SKILLS_ARCHIVE_URL` | upstream `main` tarball | Override the source archive for skills sync |
| `GIT_USER_NAME` | `Dev` | Git author/committer name (overridden by `--git-name` flag) |
| `GIT_USER_EMAIL` | `dev@localhost` | Git author/committer email (overridden by `--git-email` flag) |
| `UV_CACHE_DIR` | `${TMPDIR:-/tmp}/uv-cache` | UV cache location (resolved dynamically at shell startup; both `/tmp/uv-cache` and `$TMPDIR/uv-cache` are in the sandbox's `filesystem.allowWrite`) |

---

# start-agent.sh

Sibling script to `start-claude.sh`. Instead of one Apple Containers microVM
per project, it runs a single shared [Colima](https://github.com/abiosoft/colima)
VM with a single shared docker container that includes both the **Claude Code**
and **OpenCode** and **Pi** CLIs, and enforces a network egress allowlist at the VM level
that the in-container LLM cannot modify. Local inference is routed to an
[Ollama](https://ollama.com) or [omlx](https://github.com/jundot/omlx) instance
running on the macOS host, through a default-deny proxy in the VM that exposes
only the chat and model-listing routes.

State is organized around **sandboxes** — a single directory tree rooted at
`$SANDBOX_ROOT`, where `.sandbox_config/` holds per-sandbox auth and config and
`projects/` holds your repos. The Colima VM is launched with
`--mount $SANDBOX_ROOT:w` so only that sandbox is visible inside the VM.

Use `start-agent.sh` when you want:

- Claude Code, OpenCode, and Pi in the same environment
- A hard, LLM-uneditable egress allowlist (tinyproxy + iptables in the VM)
- Local 30B-class model inference via host Ollama or omlx
- A single VM to manage rather than one per project

Use `start-claude.sh` when you want per-project microVM isolation via Apple
Containers with no Colima, no docker, and no shared VM.

## First run

Initialize a sandbox, clone a repo into it, and run the script:

```bash
# Create the sandbox directory tree
start-agent.sh --init-sandbox ~/sandboxes/default

# Clone your repo into the sandbox
cd ~/sandboxes/default/projects
git clone https://github.com/you/my-repo.git
cd my-repo

# Start the agent environment
start-agent.sh
```

`--init-sandbox` creates:

```
~/sandboxes/default/
  .sandbox_config/
    claude/          — Claude Code auth, memory, and settings (/root/.claude)
    claude.json      — Claude Code OAuth state (/root/.claude.json)
    opencode/
      config/        — OpenCode config (/root/.config/opencode)
      data/          — OpenCode data (/root/.local/share/opencode)
    pi/              — Pi state (/root/.pi): auth, models, settings
    agents/
      skills/        — small-model skills for Pi and OpenCode (/root/.agents/skills); populated on first start-agent.sh run
    allowlist.txt    — egress domain allowlist (read-only inside the container)
    searxng/         — SearXNG config
  projects/          — your repos go here
```

Subsequent runs from inside any `projects/<repo>` subdirectory reattach
in a few seconds. Running from outside a sandbox root is a hard error with a
remediation message.

**Multiple sandboxes.** Only one sandbox can be active at a time. If you
run `start-agent.sh` from a different sandbox, the script detects the mount
mismatch, stops the `claude-agent` VM, and restarts it with the new
`--mount $SANDBOX_ROOT:w`. This takes ~10s. Projects within a sandbox share
auth and memory state.

## Requirements

- macOS with Colima and docker installed: `brew install colima docker`
- A local inference server on the host (optional):
  - **Ollama** (default): `brew install ollama`, then bind to all interfaces:
    ```bash
    launchctl setenv OLLAMA_HOST 0.0.0.0:11434
    # then restart the Ollama app
    ```
  - **omlx** (alternative): an MLX-based server with API-key auth:
    ```bash
    brew install omlx
    export OMLX_API_KEY=your-secret-key
    omlx serve --model-dir ~/models --api-key "$OMLX_API_KEY"
    ```
    omlx's API-key auth keeps LAN peers out, so it eliminates the need for a
    host-side pf firewall. The key stays on the host and in the in-VM proxy: the
    container never holds it, and the proxy adds it to every request. Use a key
    without quotes, `$`, whitespace, backslashes or `;`; the script refuses to
    start otherwise.

## Usage

```bash
# One-time: create a sandbox, then put projects under $SANDBOX/projects/
start-agent.sh --init-sandbox ~/sandboxes/default

# From inside your project directory
start-agent.sh

# Override VM sizing (defaults: 8 GiB / 6 CPUs)
start-agent.sh --memory=12G --cpus=8

# Custom git identity for commits
start-agent.sh --git-name "Jane" --git-email jane@example.com

# With omlx instead of Ollama (the key stays on the host and in the VM proxy)
export OMLX_API_KEY=your-secret-key
start-agent.sh --backend=omlx
# Later runs stay on omlx and keep the key, even from a shell without OMLX_API_KEY

# Rebuild both images + container (prompts before deleting the Colima VM);
# the only way to update OS packages, Node, uv or Claude Code in the image
start-agent.sh --rebuild

# Reset container state only (keeps image + VM; no network egress)
start-agent.sh --reset-container

# Pull the current OpenCode + Pi into the image (saves the old image as
# claude-agent:previous, recreates the containers) — and undo it
start-agent.sh --refresh-tools
start-agent.sh --restore-previous-image

# Apply edits to the allowlist without touching the running container
start-agent.sh --reload-allowlist

# Replace the allowlist with templates/sandbox-allowlist.txt (old file saved as
# allowlist.txt.bak), then reload. Neither allowlist flag combines with
# --rebuild or --reset-container.
start-agent.sh --reseed-allowlist

# Disable SearXNG (also disables OpenCode websearch; SearXNG runs by default)
start-agent.sh --disable-search

# Set OpenCode models per mode (or export CLAUDE_AGENT_PLAN_MODEL /
# CLAUDE_AGENT_EXEC_MODEL / CLAUDE_AGENT_SMALL_MODEL to persist across runs)
start-agent.sh --plan-model=gemma3:27b --exec-model=qwen2.5-coder:32b --small-model=qwen2.5-coder:7b

# Set the OpenCode default model (no CLI flag; env var only)
CLAUDE_AGENT_DEFAULT_MODEL=ollama/qwen2.5-coder:32b start-agent.sh

# Overwrite $SANDBOX/.sandbox_config/claude/CLAUDE.md AND
# $SANDBOX/.sandbox_config/opencode/config/AGENTS.md with the repo template
start-agent.sh --reseed-global-claudemd
```

First run brings up the Colima VM (`claude-agent` profile) with
`--mount $SANDBOX_ROOT:w`, installs `tinyproxy` inside it, builds
`claude-agent:base` from `dockerfiles/claude-agent.Dockerfile` and
`claude-agent:latest` (OpenCode and Pi on top of it) from
`dockerfiles/claude-agent-tools.Dockerfile`, seeds
the allowlist at `$SANDBOX_ROOT/.sandbox_config/allowlist.txt` from
`templates/sandbox-allowlist.txt`, applies
iptables rules, and drops you into a bash shell inside the container.

Subsequent runs from the same sandbox reattach in a few seconds. Switching
to a different sandbox restarts the VM with the new `--mount` (~10s).

## What's inside

| Tool | Notes |
|------|-------|
| `claude` | Claude Code CLI |
| `opencode` | [OpenCode](https://opencode.ai) 2.x CLI (installed via `@opencode/cli` npm) |
| `pi` | [Pi](https://pi.dev) CLI (installed via `@earendil-works/pi-coding-agent` npm) |
| `uv` / `uvx` | Python package manager |
| `node` / `npm` | Node.js LTS |
| `git`, `ripgrep`, `fd`, `jq`, `unzip`, `zip` | Dev tooling |

## Mounts

| Host | Container | Mode |
|------|-----------|------|
| `$SANDBOX_ROOT/projects/` | same path | RW |
| `$SANDBOX_ROOT/.sandbox_config/claude/` | `/root/.claude` | RW |
| `$SANDBOX_ROOT/.sandbox_config/claude.json` | `/root/.claude.json` | RW |
| `$SANDBOX_ROOT/.sandbox_config/opencode/config/` | `/root/.config/opencode` | RW |
| `$SANDBOX_ROOT/.sandbox_config/opencode/data/` | `/root/.local/share/opencode` | RW |
| `$SANDBOX_ROOT/.sandbox_config/pi/` | `/root/.pi` | RW |
| `$SANDBOX_ROOT/.sandbox_config/agents/skills/` | `/root/.agents/skills` | RW |
| `$SANDBOX_ROOT/.sandbox_config/allowlist.txt` | `/etc/claude-agent/allowlist.txt` | **RO** |

Auth and memory state are per-sandbox; there is no shared state with
`start-claude.sh`. `claude login` must be run once per sandbox. The
`$SANDBOX_ROOT` directory itself is **not** mounted — only the above paths,
keeping the allowlist's `:ro` mount meaningful.

## Global container CLAUDE.md

On first run, `start-agent.sh` copies `templates/global-claude.md` from the
repo into `$SANDBOX_ROOT/.sandbox_config/claude/CLAUDE.md`. Claude Code
auto-injects that file into every session, giving the model shared context about
the environment — path layout, proxy allowlist, local-inference proxy, `$TMPDIR`
conventions — regardless of the project it's opened in. Your edits are preserved
across subsequent runs. Pass `--reseed-global-claudemd` to overwrite with the
current template.

The same template is seeded into
`$SANDBOX_ROOT/.sandbox_config/opencode/config/AGENTS.md`
(mounted at `/root/.config/opencode/AGENTS.md`) and wired into OpenCode via
the `instructions` field in `opencode.json`, so OpenCode picks up the same
environment context. The `claude-dev` exceptions block at the end of the
template is stripped on the OpenCode copy since it doesn't apply inside
`claude-agent`. `--reseed-global-claudemd` reseeds this file too.

## Claude Code permission allowlist

`--init-sandbox` writes `templates/global-claude-settings.json` into
`$SANDBOX_ROOT/.sandbox_config/claude/settings.json` alongside the rest of the
directory tree. The template pre-approves common dev-loop operations (file
reads/edits, git status/add/commit, build and test commands, standard shell
utilities) and explicitly denies remote-affecting ops (`git push`, `npm publish`,
etc.).

On each subsequent run, `merge_global_settings.py` applies the same rules as
`start-claude.sh` (see that section above, and ADR-051): attribution off,
reduced motion and thinking summaries on, the push/publish deny rules merged
into `permissions.deny`, and `permissions` seeded only when absent. User
customizations survive `--rebuild` and `--reset-container` because those flags
do not touch existing files in `.sandbox_config/claude/`.

## Egress allowlist

Everything outbound from the container is denied by default. Two egress
paths are open:

1. `HTTP(S)_PROXY` → in-VM tinyproxy, which enforces a regex filter generated
   from a human-editable domain allowlist.
2. Inference → an in-VM nginx proxy on the Docker bridge address at the
   backend's port (`11434` for Ollama, `8000` for omlx). It forwards a fixed
   list of routes to the macOS host and answers 403 to everything else (see
   [Using the local inference server](#using-the-local-inference-server-from-inside-the-container)).
   The container has no direct path to the host's inference port, including
   through tinyproxy.

With search enabled, the agent can also reach `searxng:8080` on
`claude-agent-net`. External DNS is blackholed: the agent container's
resolver is `192.0.2.1` (never routed), so names like `example.com` do not
resolve locally. Tools that honor the proxy hand the hostname to tinyproxy,
and container names (`searxng`) still resolve through Docker's embedded DNS.
Services on the VM itself (its sshd, its dnsmasq) are closed to containers.

The enforcement lives in three iptables chains inside the Colima VM:
`CLAUDE_AGENT` (from `DOCKER-USER`, for traffic leaving the VM),
`CLAUDE_AGENT_IN` (from `INPUT`, for traffic to the VM's own addresses; only
tinyproxy and the inference proxy are open) and `CLAUDE_AGENT_OUT` (from
`OUTPUT`, rejecting new TCP connections from tinyproxy to the host and to the private ranges
`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16` and
`100.64.0.0/10`, so an allowlisted name cannot be used to reach the host's
inference port or the LAN; an allowlisted host on a private address will not
work through tinyproxy). Those rules cover IPv4 only. For IPv6, a fourth chain,
`CLAUDE_AGENT_OUT6`, refuses tinyproxy everything but loopback, and containers
have IPv6 disabled on their network interface. The container has no
`CAP_NET_ADMIN` and cannot touch the rules even if fully compromised. See
`ADR.md` §ADR-010 for the threat model and §ADR-050 for the inference proxy.

A container created before the DNS blackhole existed keeps its old resolver;
the script warns on reattach. Recreate it with `start-agent.sh --reset-container`.

**Upgrading from before the inference proxy.** A container created earlier keeps
its create-time environment, including the real `OMLX_API_KEY` (`docker exec -e`
cannot unset a variable), and the agent can read it. The script warns on
reattach and never removes the container for you. Run
`start-agent.sh --reset-container` once to recreate it without the key.

### Editing the allowlist

```bash
# On the macOS host — one domain per line; '#' for comments
$EDITOR $SANDBOX_ROOT/.sandbox_config/allowlist.txt

# Apply changes (~2s, no container restart)
start-agent.sh --reload-allowlist
```

The allowlist is mounted **read-only** at `/etc/claude-agent/allowlist.txt`
inside the container; the agent can read it but cannot modify the source file.

Suffix matching applies — `github.com` covers `api.github.com`,
`codeload.github.com`, etc. Each line must be a bare domain: a leading `*.`
and a trailing `.` are dropped, and anything else (a URL such as
`https://foo.com`, a path, a port, a single label such as `com`) is skipped
with a `warning: allowlist.txt:N: ...` line on stderr. The
`--reload-allowlist` summary counts the entries actually loaded. The file is
seeded on first run from `templates/sandbox-allowlist.txt` (the list
`start-claude.sh` also uses), a permissive dev/research list (Anthropic,
package registries, major scholarly publishers, etc.). Prune it to match
your actual usage.

The seed intentionally **omits write-capable hosts** that can't be split from
their read surface at the HTTP-proxy layer: `github.com`, `gitlab.com`,
`bitbucket.org`, `huggingface.co`, container registries (`docker.io`,
`quay.io`, `ghcr.io`), and dataset-upload hubs (`zenodo`, `figshare`,
`kaggle`, `osf`, `dataverse`, `datadryad`). Code reads over tarball/raw still
work via `codeload.github.com` + `githubusercontent.com`. Add the write hosts
back explicitly if your workflow requires `gh`, HTTPS push, image push, or
dataset upload from inside the container.

### Verifying the egress allowlist

Run `tests/test-agent-firewall.sh` from inside the container to verify
default-deny, allowlisted host, denied host, the inference proxy (allowed
routes, refused routes, no direct path to the host, the real key absent from the
container), env wiring, inter-container port isolation, the external DNS
blackhole, container-name DNS, and that VM-local services (sshd) are closed
while the proxy port is open:

```bash
bash tests/test-agent-firewall.sh
```

Exit 0 means all tests passed (the inference tests that need a live backend are
skipped, not failed, when it isn't running on the host; the search tests are
skipped under `--disable-search`). Run it in a container created after the
inference proxy was added. Optional environment variables:

- `INFERENCE_TEST_MODEL`: model for chat requests (default: the first model in
  `/v1/models`).
- `INFERENCE_VISION_MODEL`: an omlx vision model. The vision tests are skipped
  without it, because `/v1/models` does not say which models accept images.
- `INFERENCE_TEST_TIMEOUT`: seconds per model request (default `180`; a request
  may have to load the model first).

**Allowlist hot-reload.** The host-side fast path must update the filter
without restarting the container. From the macOS host:

```bash
echo 'example.com' >> $SANDBOX_ROOT/.sandbox_config/allowlist.txt
start-agent.sh --reload-allowlist
```

Then verify in the container that `example.com` is now reachable via the
proxy (`curl -sS --max-time 10 https://example.com` should succeed). Remove
the line and reload again to restore the default allowlist.

## Using the local inference server from inside the container

`$OLLAMA_HOST` / `$OMLX_HOST` points at an in-VM proxy on the Docker bridge
address, not at the macOS host. No API key is needed from inside the container:
the proxy adds the credentials. Only these routes work; every other route, and
any other method on these paths, returns 403:

- **Ollama:** `POST /v1/chat/completions`, `GET /v1/models`, `GET /api/tags`
- **omlx:** `POST /v1/chat/completions` (image analysis goes through here, with
  base64 images; remote image URLs are rejected by omlx), `GET /v1/models`,
  `POST /v1/messages`, `POST /v1/messages/count_tokens`

That excludes model pull/push, load/unload, omlx's web search, fetch and MCP
routes, and its admin API: do those on the host. The proxy is configured for the
active `--backend` only. See `ADR.md` §ADR-050.

OpenCode and Pi treat every discovered model as able to take images, because
`/v1/models` doesn't say which can, and most can. A text-only model gets the
image anyway and fails or ignores it. To mark one as text-only, give its entry
`"modalities": {"input": ["text"], "output": ["text"]}` in `opencode.json` or
`"input": ["text"]` in Pi's `models.json`. That only lasts until model discovery
next succeeds, because discovery rebuilds both model lists from scratch.

### Ollama (default)

```bash
echo $OLLAMA_HOST              # http://<bridge-ip>:11434
curl -s $OLLAMA_HOST/api/tags  # lists models
```

OpenCode is pre-configured with an `ollama` provider entry in
`~/.config/opencode/opencode.json` pointing at the proxy's
OpenAI-compatible endpoint. Edit the same file to add model entries:

```json
{
  "provider": {
    "ollama": {
      "models": {
        "qwen2.5-coder:32b": { "name": "Qwen2.5 Coder 32B" }
      }
    }
  }
}
```

Pi is pre-configured with a `local` provider in `~/.pi/agent/models.json`
pointing at the same Ollama endpoint. Model discovery runs automatically on
each `start-agent.sh` invocation; the first discovered model is written to
`~/.pi/agent/settings.json` as the default. To use a cloud provider with pi
(e.g. Anthropic or OpenAI), set the relevant API key env var
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) on the host before launching — pi
reads provider API keys directly from process env.

### omlx (`--backend=omlx`)

```bash
echo $OMLX_HOST                # http://<bridge-ip>:8000
curl -s $OMLX_HOST/v1/models   # no key needed; the proxy adds it
```

`OMLX_API_KEY` is not set in the container. The OpenCode and Pi configs carry a
placeholder `apiKey`, and on each run the script replaces exact copies of the real key in
them, when `OMLX_API_KEY` is set on the host.

OpenCode is pre-configured with an `omlx` provider entry. Add model
entries the same way:

```json
{
  "provider": {
    "omlx": {
      "models": {
        "mlx-community/Qwen2.5-Coder-32B-Instruct-4bit": { "name": "Qwen2.5 Coder 32B" }
      }
    }
  }
}
```

Pi is pre-configured with a `local` provider in `~/.pi/agent/models.json`
pointing at the omlx endpoint, with the same auto-discovery behaviour as the
Ollama case above.

## Local websearch (SearXNG)

**SearXNG** runs alongside `claude-agent` by default and is wired into OpenCode
as a local `websearch` MCP tool. It fans out to search engines through the same
tinyproxy allowlist, so all engine traffic is governed by
`$SANDBOX_ROOT/.sandbox_config/allowlist.txt` — no third-party search gateway.
OpenCode's own built-in `websearch` tool, which goes through Exa, Tavily and
similar gateways, stays denied in `opencode.json`; the SearXNG tool
(`searxng_websearch`) is separate from it. The SearXNG server is configured with
`codemode: false`, so OpenCode 2.x offers it as a plain tool rather than through
its JavaScript Code Mode, which small local models do not use reliably.
The SearXNG config (including a generated `secret_key`) is seeded on first run at
`$SANDBOX_ROOT/.sandbox_config/searxng/settings.yml` and survives `--rebuild`.
To reset it, delete that directory and re-run.

Engines kept by default: Google, Bing, DuckDuckGo, Brave, Qwant, Wikipedia,
arXiv, GitHub code search, Stack Exchange. `keep_only` does not enable an
engine, and upstream ships Google, Bing and Qwant disabled, so the seed also
turns Bing on with a top-level `engines:` entry. A launch adds that entry to an
older `settings.yml` that has no `engines:` key and still lists `bing`, then
recreates the searxng container (ADR-054). The general web engines that run are
Bing, DuckDuckGo and Brave, with Wikipedia for info boxes. Add an engine by
editing `settings.yml` **and** `allowlist.txt` — both files must change, by
design.

Pass `--disable-search` (or set `CLAUDE_AGENT_DISABLE_SEARCH=1`) to skip
SearXNG entirely. This also disables OpenCode websearch. The agent container
stays on `claude-agent-net` either way, so the flag can be toggled against an
existing container.

See `ADR.md` §ADR-014 for the threat model and design rationale.

## Updating OpenCode, Pi and SearXNG

**OpenCode and Pi** are installed at `@latest`, so the version is whatever npm
served when the image was built. The cached image keeps it until you refresh.
OpenCode 2.x is the `@opencode/cli` package; `opencode-ai`, installed before
ADR-056, stops at 1.x, so an image built from the older Dockerfile stays on 1.x
however often it is refreshed.

```bash
start-agent.sh --refresh-tools          # rebuild the npm layer, keep the old image as claude-agent:previous, recreate containers
start-agent.sh --restore-previous-image # undo: latest → previous, recreate containers
```

The image is built in two parts. `claude-agent:base`
(`dockerfiles/claude-agent.Dockerfile`) holds the OS packages, LibreOffice,
Node, uv, Claude Code and the Python venvs. `claude-agent:latest`
(`dockerfiles/claude-agent-tools.Dockerfile`) is `FROM claude-agent:base` plus
one npm layer with OpenCode and Pi. `--refresh-tools` rebuilds only
`claude-agent:latest`, with a fresh `TOOLS_CACHE_BUST` build arg so the npm
layer reruns, on top of the existing `claude-agent:base`. It never rebuilds the
base and does not depend on docker's build cache (a single-Dockerfile version
that relied on the cache rebuilt nearly every layer on the host; see
`ADR.md` §ADR-054). It builds the base first only when it is missing, as on the
first refresh after updating from the single-Dockerfile layout. So updating
the OS packages, Node, uv or Claude Code takes `--rebuild`, which removes
`claude-agent:base` along with `latest` and `previous` and builds both again.
The replaced image
becomes `claude-agent:previous` only after the build succeeds, so a failed
refresh leaves both tags as they were. It prints the old and new versions and
records them, keyed by image ID, in `$SANDBOX_ROOT/.sandbox_config/tool-versions`.
It removes the agent container, so exit any session running in it first. Only
one previous image is kept: the image that drops out (the older `previous`
after a refresh, the replaced `latest` after a restore) is removed once it has
no tag, unless something still uses it. After the first refresh on the
two-image layout, `previous` is the old single-Dockerfile image, on no
`claude-agent:base` at all; it is still a complete image, so restoring it works.
`--rebuild` passes no cache-bust value. With the legacy builder (no buildx
plugin on the host, the case as of 2026-11-04) the build cache is the local
images' own layers, so once all three tags are gone `--rebuild` reruns every
layer and also picks up new tool versions. Keeping `previous` would keep its
layers as cache, and a rebuild could then reproduce the older image, tools
included. The probe in `tests/verify-tool-refresh.sh` observed that the cache
did not survive `docker image rm`. `tests/probe-build-cache.sh` confirmed why: with
the containerd image store, removing the image that first built a layer drops its
cache. Under BuildKit the cache can outlive the images and
`--rebuild` may reuse the npm layer. Use `--refresh-tools` to update the tools
either way.

**SearXNG** is pinned by digest (`SEARXNG_IMAGE` in `start-agent.sh`). To move
to the current upstream image:

```bash
scripts/bump-searxng.sh                 # pull latest in the VM, rewrite the pin and its date; no commit
start-agent.sh                          # any launch replaces a searxng container on another pin
tests/verify-tool-refresh.sh            # in a second terminal; commit start-agent.sh if it passes
```

To roll back, `git checkout start-agent.sh` (or revert the commit) and launch
again.

`tests/verify-tool-refresh.sh` is the acceptance check for both: SearXNG JSON
results from at least two engines, `opencode debug config` and `opencode mcp
list`, and an `opencode run` on omlx `gemma-4-e4b-it-6bit` that calls the
SearXNG tool. After a refresh it also checks that `claude-agent:latest` is
`claude-agent:base` plus the npm layer, and that `previous` shares the base when
it was built on the same one. See `ADR.md` §ADR-054.

### Update warnings

On launch, `start-agent.sh` prints one line per component whose newest
upstream release is more than 30 days newer than the one in use, naming the
command above that updates it:

```
warning: OpenCode 2.2.5 is 43 days behind npm latest 2.4.0 (released 2026-12-13). Update: start-agent.sh --refresh-tools
warning: the SearXNG pin (current on 2026-11-03) is 35 days behind Docker Hub latest (pushed 2026-12-08). Update: scripts/bump-searxng.sh, then tests/verify-tool-refresh.sh
```

"In use" is the version recorded in `.sandbox_config/tool-versions` for
OpenCode and Pi, and the "known good on" date in the `SEARXNG_IMAGE` comment
for SearXNG (skipped under `--disable-search`). The comparison is by publish
date, not "anything newer": SearXNG's `latest` is rebuilt several times a week
and OpenCode ships every few days, so a plain "newer exists" check would fire
on nearly every launch. The check runs on the host through the shared helper
`check_versions.py`, never through the agent's proxy, so the allowlist is
unchanged. A detached background fetch asks npm and Docker Hub at most once a
day and caches the answers in `$SANDBOX_ROOT/.sandbox_config/version-check.json`;
the lines come from that cache just before attaching, so the check never
delays a launch and a release shows up on the launch after the fetch that saw
it. Any error prints nothing. Set `CLAUDE_AGENT_VERSION_LAG_DAYS` to change
the threshold, or `CLAUDE_AGENT_DISABLE_VERSION_CHECK=1` to turn it off.
`tests/verify-version-check.sh` runs the helper against the real registries.
See `ADR.md` §ADR-055.

For a browser-accessible AI research UI (Vane), see the `research.py` section
below — it runs in a separate VM with its own SearXNG instance and a denylist
egress model suited to scraping arbitrary URLs.

---

# research.py

Spins up a dedicated Colima VM (`research` profile) with an isolated egress
firewall and two containers: **SearXNG** (meta-search) and **Vane** (AI
research UI at `http://localhost:3000`). State lives in `~/.research/`.

Running `research.py` and `start-agent.sh` simultaneously is supported — they use
separate Colima profiles (`research` vs `claude-agent`) with independent VMs and
docker networks. The only host-level resource they could conflict on is port 3000,
which `research.py` binds for Vane. `start-agent.sh` no longer uses that port.

## Requirements

- macOS with Colima and docker installed
- Local inference server optional (Ollama or omlx), same as `start-agent.sh`

## Usage

```bash
./research.py                          # bring up the environment
./research.py --reload-denylist        # recompose denylist from local files (no network)
./research.py --refresh-denylist       # re-fetch upstream feeds, then reload
./research.py --reseed-denylist        # overwrite sources/additions templates from repo
./research.py --rebuild                # recreate containers (prompts for VM deletion)
./research.py --backend=omlx           # use omlx instead of Ollama
```

On first run, seeds `~/.research/denylist-sources.txt` and `denylist-additions.txt`
from `templates/`. Edit the on-disk files and run `--reload-denylist` to apply
changes. To pick up upstream template updates, run `--reseed-denylist`.

**Existing users:** if you ran `research.py` before the denylist migration, you
have a `~/.research/allowlist.txt`. On next launch `research.py` will print the
required steps and exit:

```bash
rm -rf ~/.research/
./research.py --rebuild
```

## Egress denylist

research.py uses a **denylist** (default-allow) so Vane can scrape arbitrary
search-result URLs. The composed denylist is:

    (cached upstream feeds ∪ denylist-additions.txt) − denylist-overrides.txt

All three files live in `~/.research/` on the macOS host. Egress is enforced by
**Squid** + the iptables RESEARCH chain inside the Colima VM. Squid listens on
port 8888 and performs O(1) hash-table domain lookups — supporting million-entry
denylists without OOM. (start-agent.sh uses tinyproxy for its ~280-entry allowlist;
the asymmetry is intentional — see ADR-021.)

### Threat model

**Primary motivation: research quality.** The upstream hagezi feeds (`multi.pro`,
`fake`, `tif`) block misinformation sites, content farms, AI SEO slop, and
malicious infrastructure. This is load-bearing for Vane's usefulness — unfiltered
search results degrade research quality faster than they create security risk.

**Secondary motivation: exfil hygiene.** `denylist-additions.txt` blocks
legitimate-but-weaponizable services that upstream feeds won't cover: anonymous
paste/upload sites, webhook capture endpoints, reverse tunnels, messaging APIs,
code-hosting write paths. This limits what a prompt-injection payload in a
search result could reach.

**Acknowledged limitation:** an adversary who controls their own domain (or
registers a fresh one) bypasses both layers. Human supervision of Vane is the
actual exfil control, not the proxy. See ADR-023 for the full threat-model
framing.

### Feed contents and refresh cadence

| Feed | Purpose | Refresh cadence |
|------|---------|-----------------|
| `pro-onlydomains` | Broad coverage — malware, tracking, content farms, AI slop | Monthly |
| `fake-onlydomains` | Misinformation and propaganda sites | Monthly |
| `tif-onlydomains` | Active threat intel — entries rotate as threats are taken down | Daily or weekly |

All three are hagezi's `wildcard/<list>-onlydomains.txt` variants — one
apex/registrable domain per line, with subdomain hierarchies pre-rolled-up.
research.py prepends `.` at compose time so a single `.foo.com` entry covers
the apex *and* all subdomains via Squid's `dstdomain` suffix-match. The
older `domains/<list>.txt` form lists subdomains exhaustively but omits the
apex, leaking `https://foo.com/` while blocking `https://www.foo.com/`. See
ADR-025.

A handful of canonical Google ad apexes (`doubleclick.net`,
`googleadservices.com`, etc.) are deliberately omitted by hagezi to avoid
breaking legitimate Google services. They're added back in
`templates/research-denylist-additions.txt`.

The `tif` feed degrades fastest when stale. Run `--refresh-denylist` weekly at
minimum; daily is ideal if you use research.py regularly.

### Cache hygiene

`refresh_denylist_cache()` and `reload_denylist_fast_path()` both call
`prune_orphan_cache_files()`, which deletes any `.txt` in
`~/.research/denylist-cache/` whose basename doesn't match a current URL in
`denylist-sources.txt`. Editing `sources.txt` (a template SHA bump, switching
feed paths, or commenting out a feed) and running `--reload-denylist` or
`--refresh-denylist` is therefore self-healing — no manual `rm` needed. Pruned
filenames print as `==> Pruned orphan cache file: <name>`. See ADR-026.

### Editing the denylist

To update the denylist without restarting containers:

```bash
$EDITOR ~/.research/denylist-additions.txt   # add domains to block
$EDITOR ~/.research/denylist-overrides.txt   # or remove false positives
./research.py --reload-denylist
```

To refresh upstream feeds and reload:

```bash
./research.py --refresh-denylist
```

To pick up template updates after `git pull`:

```bash
./research.py --reseed-denylist --reload-denylist
```

## Research-quality eval harness (archived)

An exploratory OFAT sweep harness for grading single-turn research output
across model / prompt / temperature / thinking-mode lived under
`tests/vane-eval/` on the `test-vane-models` branch. The findings
("retrieval ceilings dominated model variation") motivated the
`plans/local-research-harness.md` design.

Only `experiments/vane-eval/run_thinking.py` and `queries.md` are kept;
both are unmaintained. Do not extend this harness — start from
`plans/local-research-harness.md` for the next iteration.

---

# Infrastructure tests

The `tests/` directory holds automated tests for the scripts themselves (separate
from the research-quality eval harness above). All run from the macOS host unless
noted.

| Script | What it tests | How to run |
|--------|--------------|------------|
| `test-agent-firewall.sh` | Firewall smoke tests from inside `claude-agent` (default-deny, proxy allow/deny, inference proxy allowed and refused routes, no direct host path, real key absent, env wiring, inter-container port isolation, DNS blackhole, container-name DNS, VM-local services closed) | Run from inside the container: `bash tests/test-agent-firewall.sh` |
| `test-cross-vm-isolation.sh` | Cross-VM isolation: `claude-agent` and `research` containers cannot reach each other by name, port, or via `host.docker.internal:3000`. Positive tests confirm `claude-agent` reaches the in-VM inference proxy and `research` reaches the host inference port. Skips if either VM is not running. | `./tests/test-cross-vm-isolation.sh` |
| `test_agent_sh.py` | Static invariants for `start-agent.sh`: no host-port publish, sandbox trust boundary, Pi and skills wiring, warm-reattach batching, atomic firewall apply, secret handling, flag hygiene | `uv run --with pytest pytest tests/test_agent_sh.py` |
| `test_dockerfile.py` | Static checks: pi and opencode CLI install lines in `claude-agent-tools.Dockerfile`, built `FROM claude-agent:base` with the `TOOLS_CACHE_BUST` arg right before them and the npm layer last; `claude-agent.Dockerfile` (the base) installs neither | `uv run --with pytest pytest tests/test_dockerfile.py` |
| `test_tool_refresh.py` | `--refresh-tools` / `--restore-previous-image` (flag exclusions, and the image lifecycle run against a fake `docker`), the tool-version record, and `scripts/bump-searxng.sh` offline | `uv run --with pytest pytest tests/test_tool_refresh.py` |
| `test_bump_searxng.py` | `scripts/bump-searxng.sh` against a fake `colima`/`docker`: picks the `searxng/searxng` RepoDigest, refuses a stopped VM, no false "not running" under `pipefail` | `uv run --with pytest pytest tests/test_bump_searxng.py` |
| `test_check_versions.py` | Unit tests for `check_versions.py` (lag computation, once-a-day cache, malformed and missing registry responses, silence on errors and timeouts, against fixture JSON and a local HTTP server) and its wiring in both launchers | `uv run --with pytest pytest tests/test_check_versions.py` |
| `verify-version-check.sh` | Live run of `check_versions.py` against npm and Docker Hub: the Docker Hub tags-API fields, the helper's real output, the once-a-day throttle, a forced-old report naming every update command. Log in `.verify/verify-version-check.log` | `tests/verify-version-check.sh` |
| `verify-tool-refresh.sh` | Live acceptance check after a SearXNG bump or `--refresh-tools`: SearXNG results, OpenCode config and MCP, `opencode run` on omlx calling the SearXNG tool, `latest` = `base` + npm layer, build-cache behaviour. Needs the container running. Log in `.verify/verify-tool-refresh-<time>.log` | `tests/verify-tool-refresh.sh` |
| `probe-build-cache.sh` | Host-driven probe of the legacy builder's cache: records the image store and builder, then checks whether a layer stays cached after the image that first built it is removed while another image still uses it. Log in `.verify/probe-build-cache.log` | `tests/probe-build-cache.sh` |
| `test_research.py` | Unit tests for `research.py` pure helpers (`compose_denylist`, `denylist_to_squid_acl`, `_prune_subdomains`, etc.) | `uv run --with pytest pytest tests/test_research.py` |
| `test_settings_template.py` | Validates `templates/global-claude-settings.json` is well-formed JSON, has required keys, and `git push` is absent from the allow list | `uv run --with pytest pytest tests/test_settings_template.py` |
| `test_global_settings_helper.py` | Unit tests for `merge_global_settings.py`: CLI contract, forced/merged/seeded rules, and the push/publish deny rules matched against the commands they must and must not block | `uv run --with pytest pytest tests/test_global_settings_helper.py` |
| `probe-deny-rules.py` | Runs the real `claude -p` CLI against a mock Messages API on 127.0.0.1, with PATH shims, to confirm the deny rules block what the unit tests expect. Needs no account. Log in `.verify/probe-deny-rules.log` | `tests/probe-deny-rules.py [--claude PATH]` |
| `test_sandbox_allowlist.py` | Validates `templates/sandbox-allowlist.txt` is well-formed and that `start-claude.sh` injects `sandbox.network.allowedDomains` with correct `*.d` expansion | `uv run --with pytest pytest tests/test_sandbox_allowlist.py` |
| `probe-denylist.sh` | End-to-end Squid denylist probe (allow and deny URLs) from inside `research-searxng` | `bash tests/probe-denylist.sh` |
| `probe-vane-egress.sh` | Verifies `research-vane` has correct `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` env vars and that a sidecar HTTPS round-trip through Squid succeeds | `bash tests/probe-vane-egress.sh` |

The pytest suites can be run together:

```bash
uv run --with pytest pytest tests/
```

## Environment variable reference (research.py)

| Variable | Default | Description |
|----------|---------|-------------|
| `RESEARCH_BACKEND` | `ollama` | Inference backend: `ollama` or `omlx` (overridden by `--backend`) |
| `RESEARCH_MEMORY` | `2` | VM memory in GiB (overridden by `--memory`) |
| `RESEARCH_CPUS` | `2` | VM CPU count (overridden by `--cpus`) |
| `OMLX_API_KEY` | *(unset)* | API key for omlx |

---

## Environment variable reference (start-agent-specific)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_AGENT_BACKEND` | the backend the inference proxy already serves, else `ollama` | Inference backend: `ollama` or `omlx` (overridden by `--backend`). Only an explicit choice switches the proxy |
| `CLAUDE_AGENT_MEMORY` | `8` | VM memory in GiB (overridden by `--memory`) |
| `CLAUDE_AGENT_CPUS` | `6` | VM CPU count (overridden by `--cpus`) |
| `OMLX_API_KEY` | *(unset)* | API key for omlx with `--backend=omlx`. Held by the in-VM inference proxy; not passed into the container. A later run without it keeps the key the proxy already holds; a new value replaces it |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | *(unset)* | Forwarded into the container when set on the host (Pi cloud providers) |
| `CLAUDE_AGENT_DISABLE_SEARCH` | *(unset)* | Set to `1` to disable SearXNG (also disables OpenCode websearch; overridden by `--disable-search`) |
| `CLAUDE_AGENT_VERSION_LAG_DAYS` | `30` | Warn at launch when SearXNG, OpenCode or Pi has an upstream release more than this many days newer than the one in use |
| `CLAUDE_AGENT_DISABLE_VERSION_CHECK` | *(unset)* | Set to `1` to turn the launch-time update check off |
| `CLAUDE_AGENT_DEFAULT_MODEL` | *(unset)* | Default model for both OpenCode and Pi (env var only — no CLI flag). Written to `opencode.json` and `~/.pi/agent/settings.json` on each run |
| `CLAUDE_AGENT_PLAN_MODEL` | *(unset)* | OpenCode model for plan-mode agent (overridden by `--plan-model`) |
| `CLAUDE_AGENT_EXEC_MODEL` | *(unset)* | OpenCode model for execution/build agent (overridden by `--exec-model`) |
| `CLAUDE_AGENT_SMALL_MODEL` | *(unset)* | OpenCode small model (overridden by `--small-model`) |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | `Dev` / `dev@localhost` | Git identity (overridden by `--git-name` / `--git-email`) |
| `CLAUDE_SKILLS_ARCHIVE_URL` | upstream `main` tarball | Override skills source archive |
