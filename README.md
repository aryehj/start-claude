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

On subsequent runs, the script merges `showThinkingSummaries`, `coauthorTag`,
`theme`, and `permissions` non-destructively into the existing file — any key
already present is left unchanged (except `theme: "auto"`, which is promoted to
`dark-ansi` to avoid OSC 11 background-detection misreads). Your customizations
survive `--rebuild`.

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
running on the macOS host.

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
    omlx's API-key auth eliminates the need for a host-side pf firewall.

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

# With omlx instead of Ollama
export OMLX_API_KEY=your-secret-key
start-agent.sh --backend=omlx

# Rebuild image + container (prompts before deleting the Colima VM)
start-agent.sh --rebuild

# Reset container state only (keeps image + VM; no network egress)
start-agent.sh --reset-container

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
`claude-agent:latest` from `dockerfiles/claude-agent.Dockerfile`, seeds
the allowlist at `$SANDBOX_ROOT/.sandbox_config/allowlist.txt` from
`templates/sandbox-allowlist.txt`, applies
iptables rules, and drops you into a bash shell inside the container.

Subsequent runs from the same sandbox reattach in a few seconds. Switching
to a different sandbox restarts the VM with the new `--mount` (~10s).

## What's inside

| Tool | Notes |
|------|-------|
| `claude` | Claude Code CLI |
| `opencode` | [OpenCode](https://opencode.ai) CLI (installed via `opencode-ai` npm) |
| `pi` | [Pi](https://pi.dev) CLI (installed via `@earendil-works/pi-coding-agent` npm) |
| `uv` / `uvx` | Python package manager |
| `node` / `npm` | Node.js LTS |
| `git`, `ripgrep`, `fd`, `jq` | Dev tooling |

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
the environment — path layout, proxy allowlist, local-inference host, `$TMPDIR`
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

On each subsequent run, the always-run injection block merges `showThinkingSummaries`,
`coauthorTag`, `theme`, and `permissions` non-destructively — any key already
present is left unchanged (except `theme: "auto"`, which is promoted to
`dark-ansi`). User customizations survive `--rebuild` and `--reset-container`
because those flags do not touch existing files in `.sandbox_config/claude/`.

## Egress allowlist

Everything outbound from the container is denied by default. Two egress
paths are open:

1. `HTTP(S)_PROXY` → in-VM tinyproxy, which enforces a regex filter generated
   from a human-editable domain allowlist.
2. Inference server → the macOS host on the backend's port (`11434` for
   Ollama, `8000` for omlx).

With search enabled, the agent can also reach `searxng:8080` on
`claude-agent-net`. External DNS is blackholed: the agent container's
resolver is `192.0.2.1` (never routed), so names like `example.com` do not
resolve locally. Tools that honor the proxy hand the hostname to tinyproxy,
and container names (`searxng`) still resolve through Docker's embedded DNS.
Services on the VM itself (its sshd, its dnsmasq) are closed to containers.

The enforcement lives in two iptables chains inside the Colima VM:
`CLAUDE_AGENT` (from `DOCKER-USER`, for traffic leaving the VM) and
`CLAUDE_AGENT_IN` (from `INPUT`, for traffic to the VM's own addresses). The
container has no `CAP_NET_ADMIN` and cannot touch the rules even if fully
compromised. See `ADR.md` §ADR-010 for the threat model.

A container created before the DNS blackhole existed keeps its old resolver;
the script warns on reattach. Recreate it with `start-agent.sh --reset-container`.

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
default-deny, allowlisted host, denied host, Ollama carve-out, env wiring,
inter-container port isolation, the external DNS blackhole, container-name
DNS, and that VM-local services (sshd) are closed while the proxy port is
open:

```bash
bash tests/test-agent-firewall.sh
```

Exit 0 means all tests passed (the Ollama carve-out test is skipped, not
failed, when Ollama isn't running on the host; the search tests are skipped
under `--disable-search`).

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

### Ollama (default)

```bash
echo $OLLAMA_HOST              # http://<host-ip>:11434
curl -s $OLLAMA_HOST/api/tags  # lists models
```

OpenCode is pre-configured with an `ollama` provider entry in
`~/.config/opencode/opencode.json` pointing at the host Ollama's
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
echo $OMLX_HOST                # http://<host-ip>:8000
curl -s -H "Authorization: Bearer $OMLX_API_KEY" $OMLX_HOST/v1/models
```

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
The SearXNG config (including a generated `secret_key`) is seeded on first run at
`$SANDBOX_ROOT/.sandbox_config/searxng/settings.yml` and survives `--rebuild`.
To reset it, delete that directory and re-run.

Enabled engines by default: Google, Bing, DuckDuckGo, Brave, Qwant, Wikipedia,
arXiv, GitHub code search, Stack Exchange. Add an engine by editing
`settings.yml` **and** `allowlist.txt` — both files must change, by design.

Pass `--disable-search` (or set `CLAUDE_AGENT_DISABLE_SEARCH=1`) to skip
SearXNG entirely. This also disables OpenCode websearch. The agent container
stays on `claude-agent-net` either way, so the flag can be toggled against an
existing container.

See `ADR.md` §ADR-014 for the threat model and design rationale.

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

# offline-agent.py

Runs opencode and pi against a local model with **no egress at all**, over a
copy-on-write view of the directory you run it from. It has its own Colima VM
(`offline-agent` profile). Host-side state lives in `~/.offline-agent/`, and
the image is built from `dockerfiles/offline-agent.Dockerfile`.

- **Network:** the container can reach only the local inference server on the
  macOS host (`$HOST_IP:8000` for omlx, the default, or `:11434` for Ollama).
  There is no proxy, no DNS (external lookups go to a blackhole resolver), no
  SearXNG and no other VM service. Claude Code is not installed, because it
  needs the Anthropic API.
- **Writes:** the project directory is the read-only lower layer of an
  overlayfs volume, mounted at its real path. Everything the agent writes,
  `git commit` included, lands in the VM. The host directory is unchanged
  until you run `--apply`.
- **Storage:** each session has a sparse ext4 image (default 5 GiB) that holds
  the overlay's upper layer and the harnesses' history. Past that cap the agent
  gets `ENOSPC`. The container root is read-only, with small tmpfs mounts for
  `/tmp` and `~/.cache`. Memory and process count are capped. No container
  mount can write to the Mac.

It runs alongside the `claude-agent` and `research` VMs. It publishes no host
ports, and its firewall chains only match its own docker networks.

## Requirements

- macOS with Colima, docker and git
- A local inference server: omlx (default) or Ollama
- The project directory must be under `$HOME`, because Colima mounts only
  `$HOME` into the VM. It must not be `$HOME` itself.

## Usage

```bash
cd ~/some/project && offline-agent.py   # start (or attach to) this directory's session
offline-agent.py --backend=ollama       # use Ollama instead of omlx

# Review and keep the agent's work (run from the same directory)
offline-agent.py --diff                 # what changed
offline-agent.py --export ~/out.patch   # save it without touching the project
offline-agent.py --apply --dry-run      # show what --apply would do
offline-agent.py --apply                # copy it onto the host directory (asks first)
offline-agent.py --discard              # delete the session (refuses if work is unexported)
offline-agent.py --list-sessions        # every session in the VM, with used/cap

offline-agent.py --rebuild              # rebuild the image (prompts before deleting the VM)
```

Each session is keyed by the project's real path. It survives container exits,
re-runs and `colima stop`. Running the script again in the same directory while
a session is open attaches a second shell. Inside, run `opencode` or `pi`. Both
are pre-wired to the local model, with web fetch and web search turned off.

## Getting changes out

`--diff`, `--export`, `--apply` and `--discard` all read the **merged** view
of the overlay, never its upper layer (whiteouts and copy-ups make the upper
layer a poor diff). They read it in a throwaway helper container with no
network and a read-only overlay mount. The baseline (the host's HEAD, or a
manifest of every file) is recorded when the session is created. It is stored
beside the session image, where the agent cannot reach it.

**Git repos** (a `.git` directory at the project root):

| Command | Result |
|---|---|
| `--diff` | Commits since the session started, then uncommitted changes (untracked files included) |
| `--export PATH` | `PATH`: a `git format-patch` mbox of the commits (apply with `git am`). `PATH.wip.diff`: uncommitted changes, if any (apply with `git apply`) |
| `--apply` | Fast-forwards the host's current branch to the session's HEAD, using **your git on the Mac** with a bundle the helper wrote (objects are fsck'd on fetch) |

`--apply` refuses if the host HEAD moved since the session started, if the
session has uncommitted changes, or if the session rewrote history. Commit
inside the session, or fall back to `--export`. Only commits are applied;
ignored files stay behind.

**Other directories:**

| Command | Result |
|---|---|
| `--diff` | List of added, modified and deleted paths, then a unified diff of each changed file |
| `--export PATH` | `PATH`: a tarball of added and changed files. `PATH.deleted`: the paths the agent deleted |
| `--apply` | `rsync --delete` from the merged view onto the host directory |

`--apply` refuses if any path it would write or delete changed on the host
since the session started. Without that check, a host edit to a file the
agent also edited would be overwritten silently. Host edits to files the
agent never touched are fine, because the lower layer is live.

`--apply` and `--discard` refuse while the session container is running.
After `--apply`, the session still exists. Once the host has everything,
`--discard` removes it without `--force`. `--export` and `--discard` take
`--session ID` (from `--list-sessions`) to act on a session other than the
current directory's.

Sessions created before this release have no baseline. `--apply` refuses
them. Export by hand (the script prints a `git bundle` / `tar` recipe), then
run `--discard --force`.

## Threat model

- The agent can **read everything in the project directory**, including
  secrets such as `.env`, and can send it to the inference endpoint. That is
  the one channel left open by design. Don't run it in a directory holding
  secrets you wouldn't hand to the model server.
- It cannot reach the network, other VM services, other sessions, or the Mac
  filesystem outside its overlay. Mac sync clients (iCloud, Dropbox) never see
  agent output, and planted hooks (`.git/hooks`, `.envrc`,
  `.vscode/tasks.json`) never run on the Mac unless you apply them.
- A prompt-injection payload in the project can at most corrupt the overlay.
  The helpers that read the overlay turn off git's fsmonitor, hooks, pager,
  external diff drivers and textconv. Agent-written repo config can't run code
  or change what `--diff` shows. Git work never shares a container with a
  writable host mount.
- **Review `--diff` before `--apply`.** An applied commit can still add a git
  hook path, a CI file or a build script that your tools run later.
- The Colima VM is trusted, as in the sibling scripts: its docker daemon runs
  as root and can see `$HOME`.

## Adding packages

The container cannot `apt`, `pip` or `npm install` anything at runtime. Add
tools in the `# ── user packages ──` block at the end of
`dockerfiles/offline-agent.Dockerfile`, then run `offline-agent.py --rebuild`.
Every layer above that block stays cached, so a rebuild costs about as much
as the new packages. Superseded layers stay on the VM's data disk until you
run `docker --context colima-offline-agent builder prune`.

## Storage caps

| What | Default | Set with | Applies |
|---|---|---|---|
| Per-session image | 5 GiB | `--session-disk` / `OFFLINE_AGENT_SESSION_DISK` | When the session is created |
| VM root disk (OS + session images) | 10 GiB | `--root-disk` / `OFFLINE_AGENT_ROOT_DISK` | When the VM is created |
| VM data disk (image, build cache) | 10 GiB | `--disk` / `OFFLINE_AGENT_DISK` | When the VM is created |
| Container memory / pids / `/tmp` / `~/.cache` | 1536 MiB / 512 / 512 MiB / 256 MiB | constants in `offline-agent.py` | Every start |

Session images are sparse, so most sessions cost megabytes, and the per-session
caps can add up to more than the root disk. The script warns when the root
disk has less than one session cap free. To grow an existing session, run
`truncate -s <new> disk.img` and then `resize2fs` on the loop device inside the
VM. To change VM disk sizes, run `--rebuild` and delete the VM; the prompt
lists sessions with unexported work first.

opencode and pi history lives in the session image and goes away with the
session. Their config (`~/.offline-agent/opencode/config/opencode.json`,
`~/.offline-agent/pi/agent/{models,settings}.json`) is rewritten on every run
and mounted read-only. Use `OFFLINE_AGENT_DEFAULT_MODEL` rather than `/model`
to pick a model that sticks.

No `AGENTS.md` is seeded. `templates/global-claude.md` describes the proxy,
apt and uv workflows of the sibling environments, none of which work here.

---

# Infrastructure tests

The `tests/` directory holds automated tests for the scripts themselves (separate
from the research-quality eval harness above). All run from the macOS host unless
noted.

| Script | What it tests | How to run |
|--------|--------------|------------|
| `test-agent-firewall.sh` | Firewall smoke tests from inside `claude-agent` (default-deny, proxy allow/deny, Ollama carve-out, env wiring, inter-container port isolation, DNS blackhole, container-name DNS, VM-local services closed) | Run from inside the container: `bash tests/test-agent-firewall.sh` |
| `test-cross-vm-isolation.sh` | Cross-VM isolation: `claude-agent` and `research` containers cannot reach each other by name, port, or via `host.docker.internal:3000`. Positive test confirms inference carve-out still works. Skips if either VM is not running. | `./tests/test-cross-vm-isolation.sh` |
| `test_agent_sh.py` | Static invariants for `start-agent.sh`: no host-port publish, sandbox trust boundary, Pi and skills wiring, warm-reattach batching, atomic firewall apply, secret handling, flag hygiene | `uv run --with pytest pytest tests/test_agent_sh.py` |
| `test_dockerfile.py` | Static checks: pi and opencode CLI install lines in `claude-agent.Dockerfile`; offline-agent image contents | `uv run --with pytest pytest tests/test_dockerfile.py` |
| `test_research.py` | Unit tests for `research.py` pure helpers (`compose_denylist`, `denylist_to_squid_acl`, `_prune_subdomains`, etc.) | `uv run --with pytest pytest tests/test_research.py` |
| `test_settings_template.py` | Validates `templates/global-claude-settings.json` is well-formed JSON, has required keys, and `git push` is absent from the allow list | `uv run --with pytest pytest tests/test_settings_template.py` |
| `test_sandbox_allowlist.py` | Validates `templates/sandbox-allowlist.txt` is well-formed and that `start-claude.sh` injects `sandbox.network.allowedDomains` with correct `*.d` expansion | `uv run --with pytest pytest tests/test_sandbox_allowlist.py` |
| `test_offline_agent.py` | Unit tests for `offline-agent.py`: iptables render, session layout and baselines, harness config merge, `docker run` invariants, and the export/apply logic (itemize parsing, host-conflict check, git apply decisions). Runs the real git and rsync helper scripts against temp repos, including one with hostile repo config | `uv run --with pytest pytest tests/test_offline_agent.py` |
| `test-offline-agent-firewall.sh` | No-egress firewall from inside an offline-agent session: inference reachable; external HTTP, VM-local services and DNS all fail fast | Inside the session, or from the host via `docker exec` (see the script header) |
| `test-offline-agent-cross-vm.sh` | offline-agent runs beside `claude-agent` and `research` without either firewall affecting the other, and cannot reach them | `./tests/test-offline-agent-cross-vm.sh` with a session open |
| `test-offline-agent-limits.sh` | Storage and resource caps from inside a session: read-only root, `ENOSPC` at the session cap, tmpfs sizes, no writable Mac mount, cgroup pids/memory | In a scratch session created with `OFFLINE_AGENT_SESSION_DISK=256M` (see the script header) |
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

## Environment variable reference (offline-agent.py)

| Variable | Default | Description |
|----------|---------|-------------|
| `OFFLINE_AGENT_BACKEND` | `omlx` | Inference backend: `omlx` or `ollama` (overridden by `--backend`) |
| `OFFLINE_AGENT_MEMORY` | `2` | VM memory in GiB (overridden by `--memory`) |
| `OFFLINE_AGENT_CPUS` | `2` | VM CPU count (overridden by `--cpus`) |
| `OFFLINE_AGENT_DISK` | `10` | VM data disk GiB, at VM creation (overridden by `--disk`) |
| `OFFLINE_AGENT_ROOT_DISK` | `10` | VM root disk GiB, at VM creation (overridden by `--root-disk`) |
| `OFFLINE_AGENT_SESSION_DISK` | `5G` | Per-session cap, e.g. `5G` or `256M`, at session creation (overridden by `--session-disk`) |
| `OFFLINE_AGENT_DEFAULT_MODEL` | *(unset)* | Default model for opencode and pi |
| `OMLX_API_KEY` | *(unset)* | API key for omlx; passed into the container value-less |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | `Dev` / `dev@localhost` | Commit identity inside the container |

---

## Environment variable reference (start-agent-specific)

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_AGENT_BACKEND` | `ollama` | Inference backend: `ollama` or `omlx` (overridden by `--backend`) |
| `CLAUDE_AGENT_MEMORY` | `8` | VM memory in GiB (overridden by `--memory`) |
| `CLAUDE_AGENT_CPUS` | `6` | VM CPU count (overridden by `--cpus`) |
| `OMLX_API_KEY` | *(unset)* | API key for omlx; passed into the container when `--backend=omlx` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | *(unset)* | Forwarded into the container when set on the host (Pi cloud providers) |
| `CLAUDE_AGENT_DISABLE_SEARCH` | *(unset)* | Set to `1` to disable SearXNG (also disables OpenCode websearch; overridden by `--disable-search`) |
| `CLAUDE_AGENT_DEFAULT_MODEL` | *(unset)* | Default model for both OpenCode and Pi (env var only — no CLI flag). Written to `opencode.json` and `~/.pi/agent/settings.json` on each run |
| `CLAUDE_AGENT_PLAN_MODEL` | *(unset)* | OpenCode model for plan-mode agent (overridden by `--plan-model`) |
| `CLAUDE_AGENT_EXEC_MODEL` | *(unset)* | OpenCode model for execution/build agent (overridden by `--exec-model`) |
| `CLAUDE_AGENT_SMALL_MODEL` | *(unset)* | OpenCode small model (overridden by `--small-model`) |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | `Dev` / `dev@localhost` | Git identity (overridden by `--git-name` / `--git-email`) |
| `CLAUDE_SKILLS_ARCHIVE_URL` | upstream `main` tarball | Override skills source archive |
