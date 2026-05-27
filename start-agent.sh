#!/usr/bin/env bash
# start-agent.sh — spin up a Colima-backed agent container for a project.
#
# Sibling to start-claude.sh. Uses one shared Colima VM (profile:
# claude-agent), one shared docker container (claude-agent), with both the
# Claude Code and OpenCode CLIs installed. Enforces a VM-level egress
# allowlist via tinyproxy + iptables DOCKER-USER rules, and routes local
# inference to Ollama or omlx running on the macOS host.
#
# Projects must live inside a sandbox — a directory tree created by
# --init-sandbox. The Colima VM is launched with --mount $SANDBOX_ROOT:w so
# only that sandbox is visible to the VM and to the container. The allowlist
# is bind-mounted :ro inside the container at /etc/claude-agent/allowlist.txt.
#
# Usage:
#   start-agent.sh --init-sandbox PATH
#   start-agent.sh [--rebuild] [--reset-container] [--reload-allowlist]
#                  [--backend=ollama|omlx]
#                  [--disable-search]
#                  [--memory=VALUE] [--cpus=N]
#                  [--git-name=NAME] [--git-email=EMAIL]
#                  [--plan-model=MODEL] [--exec-model=MODEL] [--small-model=MODEL]
#                  [project-dir]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── args ──────────────────────────────────────────────────────────────────────
REBUILD=false
RESET_CONTAINER=false
RELOAD_ALLOWLIST=false
RESEED_ALLOWLIST=false
RESEED_GLOBAL_CLAUDEMD=false
CLI_MEMORY=""
CLI_CPUS=""
CLI_GIT_NAME=""
CLI_GIT_EMAIL=""
CLI_BACKEND=""
CLI_DISABLE_SEARCH=""
CLI_PLAN_MODEL=""
CLI_EXEC_MODEL=""
CLI_SMALL_MODEL=""
INIT_SANDBOX=""
POSITIONAL=()

usage() {
  cat <<'USAGE'
start-agent.sh — Colima-backed Claude Code + OpenCode dev container

Each project lives inside a sandbox — a directory tree with a .sandbox_config/
directory that defines the trust boundary. The single 'claude-agent' Colima VM is
launched with --mount $SANDBOX_ROOT:w so only that sandbox is visible to the
VM; $HOME and the rest of the filesystem are inaccessible. The allowlist is
mounted read-only inside the container so the agent can read but not rewrite
which URLs are permitted.

USAGE:
  start-agent.sh --init-sandbox PATH
  start-agent.sh [options] [project-dir]

OPTIONS:
  --init-sandbox PATH    Create a new sandbox at PATH with the required
                         directory layout. One-shot; exits immediately after
                         creation.
  --rebuild              Remove image + container and recreate. With
                         confirmation, also delete and recreate the Colima VM.
  --reset-container      Remove the project container (and SearXNG container)
                         but keep the image and VM. Cheaper than --rebuild
                         when you only need to reset container state.
                         Mutually exclusive with --rebuild.
  --reload-allowlist     Regenerate tinyproxy's filter file from the sandbox's
                         allowlist.txt and reload tinyproxy. Fast path; does
                         not restart the container.
  --reseed-allowlist     Overwrite the sandbox's allowlist.txt with the
                         built-in default, then reload. Use after pulling repo
                         updates that added new entries to the default list.
  --reseed-global-claudemd  Overwrite the sandbox's CLAUDE.md with the repo
                         template (default is seed-if-missing).
  --memory=VALUE         VM memory (e.g. 8, 8G, 8GB). Default: 8 GiB.
  --cpus=N               VM CPU count. Default: 6.
  --backend=BACKEND      Local inference backend: ollama (default) or omlx.
  --git-name=NAME        Git author/committer name inside the container.
  --git-email=EMAIL      Git author/committer email inside the container.
  --disable-search       Skip SearXNG container (also disables OpenCode
                         websearch). Env: CLAUDE_AGENT_DISABLE_SEARCH=1
  --plan-model=MODEL     OpenCode model for plan-mode agent (agent.plan).
                         Bare model ID (e.g. gemma3:27b) or provider/model.
  --exec-model=MODEL     OpenCode model for execution/build agent (agent.build).
  --small-model=MODEL    OpenCode small model for lightweight tasks (small_model).
  -h, --help             Show this help.

SANDBOX LAYOUT:
  $SANDBOX/
    .sandbox_config/          — marker dir (presence detected by start-agent.sh)
      allowlist.txt           — domain allowlist (mounted :ro at /etc/claude-agent/allowlist.txt)
      claude/                 — ~/.claude state (auth, settings, memory)
      claude.json             — ~/.claude.json OAuth state
      opencode/{config,data}/ — OpenCode config and data dirs
      pi/                     — Pi (~/.pi) state (auth, models, settings)
      searxng/                — SearXNG settings
    projects/                 — your work (the only RW path visible inside the container)

ALLOWLIST:
  Edit  $SANDBOX/.sandbox_config/allowlist.txt  on the macOS host to change which
  domains the container can reach. One domain per line; '#' for comments;
  suffix match (github.com covers api.github.com). Apply changes with:
      start-agent.sh --reload-allowlist
  To pick up new default entries after a repo pull:
      start-agent.sh --reseed-allowlist
  The allowlist is mounted :ro inside the container — the agent can read
  /etc/claude-agent/allowlist.txt but cannot modify the source file.

SWITCHING SANDBOXES:
  Only one sandbox can be active at a time. Switching sandboxes restarts the
  shared 'claude-agent' Colima VM with the new --mount (~10 s). Projects
  within a sandbox share auth/memory state in .sandbox_config/claude/.

ENVIRONMENT:
  CLAUDE_AGENT_MEMORY    Default VM memory (overridden by --memory).
  CLAUDE_AGENT_CPUS      Default VM CPU count (overridden by --cpus).
  CLAUDE_AGENT_BACKEND   Default backend (overridden by --backend).
  GIT_USER_NAME          Default git name (overridden by --git-name).
  GIT_USER_EMAIL         Default git email (overridden by --git-email).
  OMLX_API_KEY           API key for omlx (passed into the container when
                         --backend=omlx).
  CLAUDE_AGENT_DEFAULT_MODEL      Default OpenCode model (set via env var; no CLI flag).
  CLAUDE_AGENT_PLAN_MODEL         OpenCode plan-agent model (overridden by --plan-model).
  CLAUDE_AGENT_EXEC_MODEL         OpenCode exec/build-agent model (overridden by --exec-model).
  CLAUDE_AGENT_SMALL_MODEL        OpenCode small model (overridden by --small-model).
  CLAUDE_AGENT_DISABLE_SEARCH=1  Disable SearXNG container, also disabling OpenCode websearch (overridden by --disable-search).
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rebuild)           REBUILD=true ;;
    --reset-container)   RESET_CONTAINER=true ;;
    --reload-allowlist)  RELOAD_ALLOWLIST=true ;;
    --reseed-allowlist)  RESEED_ALLOWLIST=true; RELOAD_ALLOWLIST=true ;;
    --reseed-global-claudemd) RESEED_GLOBAL_CLAUDEMD=true ;;
    --memory=*)          CLI_MEMORY="${1#--memory=}" ;;
    --memory)            CLI_MEMORY="${2:?--memory requires a value}"; shift ;;
    --cpus=*)            CLI_CPUS="${1#--cpus=}" ;;
    --cpus)              CLI_CPUS="${2:?--cpus requires a value}"; shift ;;
    --git-name=*)        CLI_GIT_NAME="${1#--git-name=}" ;;
    --git-name)          CLI_GIT_NAME="${2:?--git-name requires a value}"; shift ;;
    --git-email=*)       CLI_GIT_EMAIL="${1#--git-email=}" ;;
    --git-email)         CLI_GIT_EMAIL="${2:?--git-email requires a value}"; shift ;;
    --backend=*)         CLI_BACKEND="${1#--backend=}" ;;
    --backend)           CLI_BACKEND="${2:?--backend requires a value}"; shift ;;
    --enable-local-search) echo "warning: --enable-local-search is deprecated (search is now enabled by default). Use --disable-search to disable." >&2 ;;
    --disable-search)     CLI_DISABLE_SEARCH="true" ;;
    --plan-model=*)      CLI_PLAN_MODEL="${1#--plan-model=}" ;;
    --plan-model)        CLI_PLAN_MODEL="${2:?--plan-model requires a value}"; shift ;;
    --exec-model=*)      CLI_EXEC_MODEL="${1#--exec-model=}" ;;
    --exec-model)        CLI_EXEC_MODEL="${2:?--exec-model requires a value}"; shift ;;
    --small-model=*)     CLI_SMALL_MODEL="${1#--small-model=}" ;;
    --small-model)       CLI_SMALL_MODEL="${2:?--small-model requires a value}"; shift ;;
    --init-sandbox=*)    INIT_SANDBOX="${1#--init-sandbox=}" ;;
    --init-sandbox)      INIT_SANDBOX="${2:?--init-sandbox requires a PATH}"; shift ;;
    -h|--help)           usage; exit 0 ;;
    *)                   POSITIONAL+=("$1") ;;
  esac
  shift
done

if $RESET_CONTAINER && $REBUILD; then
  echo "error: --reset-container and --rebuild are mutually exclusive." >&2
  echo "  --reset-container removes the container but keeps the image (cheap)." >&2
  echo "  --rebuild removes both the container and image (full rebuild)." >&2
  exit 1
fi

# ── sandbox helpers ───────────────────────────────────────────────────────────

# Create a new sandbox directory tree. The .sandbox_config/ directory itself
# is the marker that distinguishes a sandbox root.
init_sandbox() {
  local target="$1"
  if [[ -d "$target/.sandbox_config" ]]; then
    echo "error: '$target' is already initialized as a sandbox (.sandbox_config/ present)." >&2
    exit 1
  fi
  if [[ ! -d "$target" ]]; then
    mkdir -m 0700 -p "$target"
  fi
  mkdir -p \
    "$target/.sandbox_config/claude" \
    "$target/.sandbox_config/opencode/config" \
    "$target/.sandbox_config/opencode/data" \
    "$target/.sandbox_config/pi" \
    "$target/.sandbox_config/agents/skills" \
    "$target/.sandbox_config/searxng" \
    "$target/projects"
  local settings_tmpl="$SCRIPT_DIR/templates/global-claude-settings.json"
  local settings_dest="$target/.sandbox_config/claude/settings.json"
  if [[ -f "$settings_tmpl" ]] && [[ ! -f "$settings_dest" ]]; then
    cp "$settings_tmpl" "$settings_dest"
    echo "==> Seeded $settings_dest from template"
  fi
  echo "==> Sandbox created at $target"
  echo ""
  echo "Next steps:"
  echo "  cd $target/projects"
  echo "  git clone <your-repo>   # or: mkdir <project> && cd <project>"
  echo "  start-agent.sh"
}

# Walk up from the current directory until a .sandbox_config/ dir is found.
# Prints the sandbox root path and returns 0; returns 1 if none found.
find_sandbox_root() {
  local dir
  dir="$(pwd)"
  while [[ "$dir" != "/" ]]; do
    if [[ -d "$dir/.sandbox_config" ]]; then
      echo "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

# ── --init-sandbox: one-shot operation ───────────────────────────────────────
if [[ -n "$INIT_SANDBOX" ]]; then
  init_sandbox "$INIT_SANDBOX"
  exit 0
fi

PROJECT_DIR="${POSITIONAL[0]:-$(pwd)}"
PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

# ── sandbox detection ─────────────────────────────────────────────────────────
SANDBOX_ROOT="$(find_sandbox_root)" || {
  echo "error: no .sandbox_config/ directory found in the current directory or any parent." >&2
  echo "  To create a sandbox, run:" >&2
  echo "    start-agent.sh --init-sandbox PATH" >&2
  exit 1
}
SANDBOX_NAME="$(basename "$SANDBOX_ROOT")"

# PROJECT_DIR must be inside $SANDBOX_ROOT/projects/ to stay within the trust boundary.
if [[ "$PROJECT_DIR" != "$SANDBOX_ROOT/projects" && "$PROJECT_DIR" != "$SANDBOX_ROOT/projects/"* ]]; then
  echo "error: PROJECT_DIR ($PROJECT_DIR) must be a subdirectory of $SANDBOX_ROOT/projects/." >&2
  echo "  The sandbox's projects directory is: $SANDBOX_ROOT/projects/" >&2
  exit 1
fi

# Accept "8", "8G", "8GB" (any case); normalize to an integer GiB that
# `colima start --memory` expects.
normalize_gib() {
  local raw
  raw="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  raw="${raw// /}"
  raw="${raw%gib}"
  raw="${raw%gb}"
  raw="${raw%g}"
  if [[ ! "$raw" =~ ^[0-9]+$ ]]; then
    echo "error: invalid memory value '$1' — use integer GiB (e.g. 8, 8G, 8GB)" >&2
    exit 1
  fi
  echo "$raw"
}

MEMORY_RAW="${CLI_MEMORY:-${CLAUDE_AGENT_MEMORY:-8}}"
CLAUDE_AGENT_MEMORY_GB="$(normalize_gib "$MEMORY_RAW")"
CLAUDE_AGENT_CPUS="${CLI_CPUS:-${CLAUDE_AGENT_CPUS:-6}}"
if [[ ! "$CLAUDE_AGENT_CPUS" =~ ^[0-9]+$ ]]; then
  echo "error: invalid --cpus value '$CLAUDE_AGENT_CPUS'" >&2
  exit 1
fi

GIT_USER_NAME="${CLI_GIT_NAME:-${GIT_USER_NAME:-Dev}}"
GIT_USER_EMAIL="${CLI_GIT_EMAIL:-${GIT_USER_EMAIL:-dev@localhost}}"

BACKEND="${CLI_BACKEND:-${CLAUDE_AGENT_BACKEND:-ollama}}"
case "$BACKEND" in
  ollama)
    INFERENCE_PORT=11434
    INFERENCE_LABEL="Ollama"
    ;;
  omlx)
    INFERENCE_PORT=8000
    INFERENCE_LABEL="omlx"
    ;;
  *)
    echo "error: unknown --backend value '$BACKEND'. Must be 'ollama' or 'omlx'." >&2
    exit 1
    ;;
esac

if [[ "$BACKEND" == "omlx" ]]; then
  OMLX_API_KEY="${OMLX_API_KEY:-}"
  if [[ -z "$OMLX_API_KEY" ]]; then
    echo "warning: OMLX_API_KEY not set. omlx requests from the container will fail if the server requires auth." >&2
  fi
fi

PLAN_MODEL="${CLI_PLAN_MODEL:-${CLAUDE_AGENT_PLAN_MODEL:-}}"
EXEC_MODEL="${CLI_EXEC_MODEL:-${CLAUDE_AGENT_EXEC_MODEL:-}}"
SMALL_MODEL="${CLI_SMALL_MODEL:-${CLAUDE_AGENT_SMALL_MODEL:-}}"

if [[ "${CLI_DISABLE_SEARCH:-}" == "true" || "${CLAUDE_AGENT_DISABLE_SEARCH:-}" =~ ^(1|true|yes)$ ]]; then
  LOCAL_SEARCH_ENABLED=false
else
  LOCAL_SEARCH_ENABLED=true
fi

# ── constants ────────────────────────────────────────────────────────────────
COLIMA_PROFILE="claude-agent"
CONTAINER_NAME="claude-agent"
IMAGE_TAG="claude-agent:latest"
DOCKERFILE_PATH="$(cd "$(dirname "$0")" && pwd)/dockerfiles/claude-agent.Dockerfile"
DOCKERFILE_DIR="$(dirname "$DOCKERFILE_PATH")"
CLAUDE_CONFIG_DIR="$SANDBOX_ROOT/.sandbox_config/claude"
CLAUDE_JSON_FILE="$SANDBOX_ROOT/.sandbox_config/claude.json"
OPENCODE_CONFIG_DIR="$SANDBOX_ROOT/.sandbox_config/opencode/config"
OPENCODE_DATA_DIR="$SANDBOX_ROOT/.sandbox_config/opencode/data"
PI_CONFIG_DIR="$SANDBOX_ROOT/.sandbox_config/pi"
AGENTS_SKILLS_DIR="$SANDBOX_ROOT/.sandbox_config/agents/skills"
ALLOWLIST_FILE="$SANDBOX_ROOT/.sandbox_config/allowlist.txt"
TINYPROXY_PORT=8888
SEARXNG_CONTAINER="searxng"
SEARXNG_DIR="$SANDBOX_ROOT/.sandbox_config/searxng"
SEARXNG_SETTINGS_FILE="$SEARXNG_DIR/settings.yml"
AGENT_NET_NAME="claude-agent-net"

# ── preflight ────────────────────────────────────────────────────────────────
if ! command -v colima &>/dev/null; then
  echo "error: 'colima' not found. Install with: brew install colima docker" >&2
  exit 1
fi
if ! command -v docker &>/dev/null; then
  echo "error: 'docker' not found. Install with: brew install colima docker" >&2
  exit 1
fi
if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "error: project dir '$PROJECT_DIR' does not exist." >&2
  exit 1
fi
if [[ ! -f "$DOCKERFILE_PATH" ]]; then
  echo "error: Dockerfile not found at $DOCKERFILE_PATH" >&2
  exit 1
fi

# ── seed allowlist (first run only) ──────────────────────────────────────────
mkdir -p "$(dirname "$ALLOWLIST_FILE")"
if [[ ! -f "$ALLOWLIST_FILE" ]] || $RESEED_ALLOWLIST; then
  cat > "$ALLOWLIST_FILE" <<'ALLOWLIST'
# start-agent allowlist — edit on the macOS host.
# Apply changes:  start-agent.sh --reload-allowlist
# Suffix match:   'github.com' also matches 'api.github.com'.
# Comments start with '#'.

# === Anthropic / AI coding agents ===
anthropic.com
claude.ai
claude.com
opencode.ai

# === Source control & code hosting ===
# Read-only hosts only. github.com / gitlab.com / bitbucket.org are
# intentionally omitted because the HTTP-proxy allowlist can't distinguish
# reads from writes (PRs, pushes, issue comments). Tarball/raw fetches go
# through codeload/githubusercontent. If you need `gh` or HTTPS push from
# inside the container, add the write host explicitly.
githubusercontent.com
githubassets.com
codeload.github.com
git-scm.com

# === Package registries ===
npmjs.org
npmjs.com
yarnpkg.com
nodejs.org
pypi.org
pythonhosted.org
crates.io
rust-lang.org
rubygems.org
ruby-lang.org
packagist.org
php.net
pkg.go.dev
proxy.golang.org
golang.org
go.dev
hex.pm

# === Container / image registries ===
# Omitted by default (docker.io / quay.io / ghcr.io) — writes (image push)
# aren't distinguishable from reads at the proxy. Add back if you pull
# images from inside the container.

# === OS package repos ===
debian.org
ubuntu.com
deb.nodesource.com
astral.sh

# === Model & dataset hubs ===
# huggingface.co omitted — supports model/dataset uploads. Add back if
# you need HF downloads from inside the container.
ollama.com

# === General web & reference ===
wikipedia.org
wikimedia.org
wikidata.org
britannica.com
mozilla.org
developer.mozilla.org
stackoverflow.com
stackexchange.com
archive.org
archive.ph
archive.today

# === Search engines ===
google.com
duckduckgo.com
bing.com
search.brave.com
api.qwant.com
kagi.com
# api.github.com — targeted entry for SearXNG's GitHub code-search engine.
# Does NOT enable github.com web writes (push, PR, issue comments).
api.github.com

# === Biomedical / life sciences ===
ncbi.nlm.nih.gov
nlm.nih.gov
nih.gov
europepmc.org
biorxiv.org
medrxiv.org
chemrxiv.org
eartharxiv.org
plos.org
frontiersin.org
mdpi.com
biomedcentral.com
elifesciences.org
nature.com
cell.com
science.org
sciencemag.org
mayoclinic.org
medlineplus.gov
nice.org.uk

# === Physical sciences, engineering, math, CS ===
arxiv.org
semanticscholar.org
openalex.org
dimensions.ai
oa.mg
core.ac.uk
base-search.net
doaj.org
dblp.org
acm.org
ieee.org
aps.org
aip.org
acs.org
rsc.org
iop.org
ams.org
pnas.org
royalsocietypublishing.org
jamanetwork.com
nejm.org
bmj.com
thelancet.com
paperswithcode.com
openreview.net
distill.pub

# === Social sciences, humanities, general journals ===
jstor.org
ssrn.com
crossref.org
doi.org
orcid.org
sciencedirect.com
springer.com
link.springer.com
springernature.com
wiley.com
onlinelibrary.wiley.com
tandfonline.com
sagepub.com
cambridge.org
oup.com
muse.jhu.edu

# === Data repositories & open science ===
# Omitted by default — zenodo/figshare/osf/dataverse/datadryad/kaggle all
# support authenticated uploads. Add back per-host if you need reads.

# === Government / statistical / standards ===
nasa.gov
cdc.gov
fda.gov
nsf.gov
census.gov
noaa.gov
usgs.gov
bls.gov
bea.gov
loc.gov
gov.uk
who.int
europa.eu
sec.gov
supremecourt.gov
federalregister.gov
congress.gov
uscourts.gov
govinfo.gov
cfpb.gov
ftc.gov
gao.gov
usda.gov
epa.gov
data.gov
clinicaltrials.gov
icpsr.umich.edu
federalreserve.gov
fred.stlouisfed.org
worldbank.org
imf.org
oecd.org
un.org
unicef.org
wto.org

# === News & periodicals for research context ===
nytimes.com
economist.com
ft.com
reuters.com
apnews.com
bbc.com
bbc.co.uk
washingtonpost.com
theatlantic.com
newyorker.com
theguardian.com
wsj.com
bloomberg.com
npr.org
politico.com
propublica.org

# === Science publications & magazines ===
quantamagazine.org
scientificamerican.com
technologyreview.com
arstechnica.com
wired.com
newscientist.com
nautil.us
aeon.co

# === Community & discussion ===
reddit.com
news.ycombinator.com
lesswrong.com
lobste.rs
metafilter.com
slashdot.org

# === Software & language docs ===
readthedocs.io
readthedocs.org
hexdocs.pm
elixir-lang.org
kotlinlang.org
scala-lang.org
clojure.org
haskell.org
kubernetes.io
docs.docker.com
docs.kernel.org
man7.org
gnu.org
devdocs.io

# === Standards & specs ===
w3.org
whatwg.org
ietf.org
rfc-editor.org
iso.org
unicode.org
schema.org
ecma-international.org
khronos.org

# === Legal & regulatory ===
courtlistener.com
law.cornell.edu
justia.com
oyez.org

# === Major universities ===
# Starter set; add institutions to the sandbox's allowlist.txt as needed.
mit.edu
stanford.edu
harvard.edu
berkeley.edu
princeton.edu
yale.edu
columbia.edu
cmu.edu
caltech.edu
uchicago.edu
cornell.edu
upenn.edu
northwestern.edu
ucla.edu
umich.edu
ox.ac.uk
cam.ac.uk
ucl.ac.uk
lse.ac.uk
imperial.ac.uk
ed.ac.uk
ethz.ch
epfl.ch
mpg.de
utoronto.ca
ALLOWLIST
  if $RESEED_ALLOWLIST; then
    echo "==> Reseeded allowlist at $ALLOWLIST_FILE (sandbox: $SANDBOX_NAME)"
  else
    echo "==> Seeded allowlist at $ALLOWLIST_FILE (sandbox: $SANDBOX_NAME)"
  fi
fi

# Generate a tinyproxy filter file on the host from the allowlist.
# Each non-comment domain becomes an anchored regex: (^|\.)domain\.tld$
# which matches the domain itself and any subdomain.
generate_filter_file() {
  local out="$1"
  python3 - "$ALLOWLIST_FILE" > "$out" <<'PYEOF'
import re, sys
with open(sys.argv[1]) as f:
    for raw in f:
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        escaped = re.escape(line)
        print(f"(^|\\.){escaped}$")
PYEOF
}

# ── Colima VM bring-up ───────────────────────────────────────────────────────
colima_profile_running() {
  colima list -p "$COLIMA_PROFILE" --json 2>/dev/null | grep -q '"Running"'
}

destroy_colima_vm() {
  echo "==> Destroying Colima VM '$COLIMA_PROFILE'"
  colima delete -p "$COLIMA_PROFILE" --force 2>/dev/null || true
}

start_colima_vm() {
  echo "==> Starting Colima VM '$COLIMA_PROFILE' ($CLAUDE_AGENT_MEMORY_GB GiB RAM, $CLAUDE_AGENT_CPUS CPUs)"
  echo "    sandbox mount: $SANDBOX_ROOT"
  colima start -p "$COLIMA_PROFILE" \
    --vm-type vz \
    --runtime docker \
    --cpu "$CLAUDE_AGENT_CPUS" \
    --memory "$CLAUDE_AGENT_MEMORY_GB" \
    --mount "$SANDBOX_ROOT:w" \
    --mount-type virtiofs \
    --network-address
}

# Run a command inside the Colima VM.
#
# SSH passes the remote command as a single string (the server re-tokenizes
# it), and additionally re-quotes whatever single string we hand it, so any
# attempt to pre-join with printf %q ends up double-quoted into a single
# literal word on the remote side. Bypass colima's argv handling entirely by
# piping the command line in via stdin — colima ssh hands stdin straight to
# the remote shell with no extra quoting.
vm_ssh() {
  local cmd="" a
  for a in "$@"; do
    cmd+=" $(printf '%q' "$a")"
  done
  vm_sh "$cmd"
}

# Variant that takes a complete shell command line as a single argument and
# runs it in the VM. Use when you want shell pipelines, redirections, or
# heredocs evaluated remotely.
vm_sh() {
  printf '%s\n' "$1" | colima ssh -p "$COLIMA_PROFILE" -- bash
}

# Copy a host-side file into the VM at $2 with mode $3 (default 644).
# colima ssh re-quotes argv, but the command itself has no shell
# metacharacters, and the file payload rides on stdin — so no quoting games.
vm_put_file() {
  local src="$1" dest="$2" mode="${3:-644}"
  colima ssh -p "$COLIMA_PROFILE" -- sudo tee "$dest" >/dev/null < "$src"
  colima ssh -p "$COLIMA_PROFILE" -- sudo chmod "$mode" "$dest"
}

remove_containers() {
  local label="$1"
  if docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "==> $label: removed container '$CONTAINER_NAME'"
  fi
  if $LOCAL_SEARCH_ENABLED && docker rm -f "$SEARXNG_CONTAINER" >/dev/null 2>&1; then
    echo "==> $label: removed container '$SEARXNG_CONTAINER'"
  fi
}

# ── --rebuild: optionally destroy the Colima VM before starting it ───────────
# Container + image removal happens AFTER the VM is up so that `docker`
# actually has something to talk to; deleting the VM itself must happen BEFORE
# `colima start` so the start-up path recreates a clean VM.
if $REBUILD && ! $RELOAD_ALLOWLIST; then
  echo
  read -r -p "Also delete and recreate the Colima VM '$COLIMA_PROFILE'? This is NOT reversible. [y/N] " confirm
  if [[ "${confirm:-}" =~ ^[Yy]$ ]]; then
    destroy_colima_vm
  else
    echo "==> Keeping Colima VM; only image/container will be rebuilt."
  fi
fi

# ── ensure the VM is running ─────────────────────────────────────────────────
if ! colima_profile_running; then
  start_colima_vm
else
  # Detect sandbox switch by checking whether $SANDBOX_ROOT appears in the
  # VM's virtiofs mount list. Colima mounts $HOME and /tmp/colima by default
  # in addition to our --mount, so picking the first entry was wrong and
  # caused a spurious VM restart on every re-attach.
  current_vm_mounts=$(vm_sh 'mount -t virtiofs 2>/dev/null | awk "{print \$3}"' 2>/dev/null | tr -d '\r' || true)
  if [[ -n "$current_vm_mounts" ]] && ! grep -Fxq "$SANDBOX_ROOT" <<<"$current_vm_mounts"; then
    echo "==> Sandbox '$SANDBOX_ROOT' not mounted in running VM — restarting with new mount"
    colima stop -p "$COLIMA_PROFILE"
    start_colima_vm
  else
    # Warn if the running VM sizing differs from what was requested.
    running_json=$(colima list -p "$COLIMA_PROFILE" --json 2>/dev/null || true)
    if [[ -n "$running_json" ]]; then
      running_cpu=$(printf '%s' "$running_json" | python3 -c 'import json,sys;d=json.loads(sys.stdin.read());print(d.get("cpus",""))' 2>/dev/null || echo "")
      running_mem=$(printf '%s' "$running_json" | python3 -c 'import json,sys;d=json.loads(sys.stdin.read());m=d.get("memory","");print(m)' 2>/dev/null || echo "")
      if [[ -n "$running_cpu" && "$running_cpu" != "$CLAUDE_AGENT_CPUS" ]]; then
        echo "warning: running VM has $running_cpu CPUs; requested $CLAUDE_AGENT_CPUS. Use --rebuild to resize." >&2
      fi
      if [[ -n "$running_mem" && "$running_mem" != *"${CLAUDE_AGENT_MEMORY_GB}"* ]]; then
        echo "warning: running VM memory is $running_mem; requested ${CLAUDE_AGENT_MEMORY_GB}GiB. Use --rebuild to resize." >&2
      fi
    fi
  fi
fi

# Point the local docker CLI at Colima's socket for this profile.
if ! docker context use "colima-$COLIMA_PROFILE" &>/dev/null; then
  echo "warning: could not switch docker context to colima-$COLIMA_PROFILE; assuming current context talks to the right daemon." >&2
fi

# Now that docker is reachable, honor --rebuild / --reset-container by removing
# containers (and the image, for --rebuild) from the VM's docker runtime.
# The VM itself was handled earlier (--rebuild only).
if $REBUILD && ! $RELOAD_ALLOWLIST; then
  remove_containers "--rebuild"
  if docker image inspect "$IMAGE_TAG" &>/dev/null; then
    echo "==> --rebuild: removing image '$IMAGE_TAG'"
    docker image rm "$IMAGE_TAG" >/dev/null
  fi
elif $RESET_CONTAINER && ! $RELOAD_ALLOWLIST; then
  remove_containers "--reset-container"
  echo "==> --reset-container: image '$IMAGE_TAG' kept intact"
fi

# ── discover bridge IP, host IP, bridge CIDR inside the VM ───────────────────
BRIDGE_IP=$(vm_ssh docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null | tr -d '\r' || true)
if [[ -z "$BRIDGE_IP" ]]; then
  BRIDGE_IP="172.17.0.1"
  echo "warning: could not discover docker bridge gateway; falling back to $BRIDGE_IP" >&2
fi
BRIDGE_CIDR=$(vm_ssh docker network inspect bridge -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null | tr -d '\r' || true)
if [[ -z "$BRIDGE_CIDR" ]]; then
  BRIDGE_CIDR="172.17.0.0/16"
fi

# HOST_IP from inside the VM: default gateway under Colima's vmnet points at
# the macOS host when `--network-address` is set. Run `ip route` remotely and
# parse the output locally so we don't have to fight with quoting awk's $3
# through the SSH argv-join round trip.
HOST_IP=$(vm_ssh ip route show default 2>/dev/null | tr -d '\r' | awk '/^default/ {print $3; exit}' || true)
if [[ -z "$HOST_IP" ]]; then
  for candidate in host.lima.internal host.docker.internal; do
    hosts_line=$(vm_ssh getent hosts "$candidate" 2>/dev/null | tr -d '\r' || true)
    if [[ -n "$hosts_line" ]]; then
      HOST_IP=$(printf '%s\n' "$hosts_line" | awk '{print $1; exit}')
      [[ -n "$HOST_IP" ]] && break
    fi
  done
fi
if [[ -z "$HOST_IP" ]]; then
  echo "warning: could not determine the macOS host IP from inside the VM; local inference ($INFERENCE_LABEL) will not work." >&2
  HOST_IP="127.0.0.1"
fi

echo "==> VM network: bridge=$BRIDGE_IP cidr=$BRIDGE_CIDR host=$HOST_IP"

# ── user-defined network for inter-container DNS (agent + searxng) ────────────
# Docker embedded DNS resolves container names only on user-defined networks,
# not on the default bridge. claude-agent-net provides this without changing
# tinyproxy's Listen address — VM routing lets containers on this network reach
# $BRIDGE_IP:$TINYPROXY_PORT through the default bridge interface.
AGENT_NET_CIDR=""
if $LOCAL_SEARCH_ENABLED; then
  AGENT_NET_CIDR=$(vm_ssh docker network inspect "$AGENT_NET_NAME" -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null | tr -d '\r' || true)
  if [[ -z "$AGENT_NET_CIDR" ]]; then
    vm_ssh docker network create "$AGENT_NET_NAME" >/dev/null
    AGENT_NET_CIDR=$(vm_ssh docker network inspect "$AGENT_NET_NAME" -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null | tr -d '\r' || true)
  fi
  if [[ -z "$AGENT_NET_CIDR" ]]; then
    AGENT_NET_CIDR="172.20.0.0/24"
    echo "warning: could not discover $AGENT_NET_NAME CIDR; falling back to $AGENT_NET_CIDR" >&2
  fi
  echo "==> Agent network: $AGENT_NET_NAME cidr=$AGENT_NET_CIDR"
fi

# ── SearXNG settings.yml seed (first run only, local-search path) ────────────
if $LOCAL_SEARCH_ENABLED; then
  mkdir -p "$SEARXNG_DIR"
  if [[ ! -f "$SEARXNG_SETTINGS_FILE" ]]; then
    SECRET_KEY="$(openssl rand -hex 32)"
    cat > "$SEARXNG_SETTINGS_FILE" <<SXNG
use_default_settings:
  engines:
    keep_only:
      - google
      - bing
      - duckduckgo
      - brave
      - qwant
      - wikipedia
      - arxiv
      - github
      - stack overflow

server:
  secret_key: "$SECRET_KEY"
  base_url: "http://searxng:8080/"
  limiter: false

search:
  formats:
    - html
    - json

outgoing:
  proxies:
    all://: "http://$BRIDGE_IP:$TINYPROXY_PORT"
SXNG
    echo "==> Seeded $SEARXNG_SETTINGS_FILE (secret_key generated, proxy=$BRIDGE_IP:$TINYPROXY_PORT)"
  fi
fi

# ── tinyproxy install in VM (idempotent) ─────────────────────────────────────
# Check the `tinyproxy` package, not the binary: on Ubuntu 24.04 the package is
# split (`tinyproxy-bin` ships /usr/bin/tinyproxy; `tinyproxy` ships the systemd
# unit + default config). A prior purge of `tinyproxy` can leave the binary
# behind, so a `command -v` check would pass while `systemctl` has no unit.
if ! vm_ssh dpkg-query -W -f='${Status}' tinyproxy 2>/dev/null | grep -q "install ok installed"; then
  echo "==> Installing tinyproxy in Colima VM"
  vm_ssh sudo apt-get update -qq
  # DEBIAN_FRONTEND=noninteractive + --force-confnew handles the case where a
  # previous run wrote /etc/tinyproxy/tinyproxy.conf before the package was
  # installed: dpkg would otherwise prompt about the existing conffile and
  # fail over non-interactive ssh. We rewrite the conffile below anyway.
  vm_ssh sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    -o Dpkg::Options::=--force-confnew tinyproxy
  vm_ssh sudo systemctl daemon-reload
fi

# Build tinyproxy config and filter on the host, then push into the VM.
TMP_WORK=$(mktemp -d)
trap 'rm -rf "$TMP_WORK"' EXIT

cat > "$TMP_WORK/tinyproxy.conf" <<CONF
User tinyproxy
Group tinyproxy
Port $TINYPROXY_PORT
Listen $BRIDGE_IP
Timeout 600
DefaultErrorFile "/usr/share/tinyproxy/default.html"
StatFile "/usr/share/tinyproxy/stats.html"
LogFile "/var/log/tinyproxy/tinyproxy.log"
LogLevel Info
MaxClients 100
FilterDefaultDeny Yes
Filter "/etc/tinyproxy/filter"
FilterExtended Yes
FilterURLs No
ConnectPort 443
ConnectPort 80
CONF

generate_filter_file "$TMP_WORK/filter"

# Ship both files directly into /etc/tinyproxy/ via `sudo tee` over colima ssh.
vm_put_file "$TMP_WORK/tinyproxy.conf" /etc/tinyproxy/tinyproxy.conf
vm_put_file "$TMP_WORK/filter"         /etc/tinyproxy/filter

# Enable and (re)start/reload tinyproxy. On first run, enable+start; otherwise
# reload to pick up filter changes without interrupting in-flight connections.
if $RELOAD_ALLOWLIST; then
  vm_ssh sudo systemctl reload tinyproxy 2>/dev/null || vm_ssh sudo systemctl restart tinyproxy
else
  vm_ssh sudo systemctl enable --now tinyproxy >/dev/null 2>&1 || true
  vm_ssh sudo systemctl restart tinyproxy
fi

# ── iptables rules in the VM (CLAUDE_AGENT child chain of DOCKER-USER) ───────
# All rules live in a dedicated CLAUDE_AGENT chain, which DOCKER-USER jumps to
# for bridge-sourced traffic. This gives atomic flush-and-repopulate without
# walking line numbers or matching rules by comment text.
cat > "$TMP_WORK/firewall-apply.sh" <<FWEOF
#!/bin/sh
set -e
BRIDGE_IP="$BRIDGE_IP"
BRIDGE_CIDR="$BRIDGE_CIDR"
AGENT_NET_CIDR="$AGENT_NET_CIDR"
HOST_IP="$HOST_IP"
TINYPROXY_PORT="$TINYPROXY_PORT"
INFERENCE_PORT="$INFERENCE_PORT"
LOCAL_SEARCH_ENABLED="$LOCAL_SEARCH_ENABLED"

# Ensure DOCKER-USER exists (docker creates it on fresh daemons, but be safe).
sudo iptables -N DOCKER-USER 2>/dev/null || true
sudo iptables -C FORWARD -j DOCKER-USER 2>/dev/null || sudo iptables -I FORWARD 1 -j DOCKER-USER

# Our own chain. Create if absent, then flush to start clean.
sudo iptables -N CLAUDE_AGENT 2>/dev/null || true
sudo iptables -F CLAUDE_AGENT

# Jump into our chain from DOCKER-USER for default bridge traffic (idempotent).
sudo iptables -C DOCKER-USER -s "\$BRIDGE_CIDR" -j CLAUDE_AGENT 2>/dev/null \
  || sudo iptables -I DOCKER-USER 1 -s "\$BRIDGE_CIDR" -j CLAUDE_AGENT

# Also jump for user-defined agent network traffic. Note: claude-agent-net →
# tinyproxy ($BRIDGE_IP:$TINYPROXY_PORT) does NOT go through FORWARD — $BRIDGE_IP
# is a local address on the VM's docker0 interface, so that traffic hits the INPUT
# chain instead and is not controlled here. This jump is load-bearing for the
# claude-agent → searxng:8080 intra-network path, which DOES traverse FORWARD.
if [ -n "\$AGENT_NET_CIDR" ]; then
  sudo iptables -C DOCKER-USER -s "\$AGENT_NET_CIDR" -j CLAUDE_AGENT 2>/dev/null \
    || sudo iptables -I DOCKER-USER 2 -s "\$AGENT_NET_CIDR" -j CLAUDE_AGENT
fi

# Populate rules in order.
sudo iptables -A CLAUDE_AGENT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
sudo iptables -A CLAUDE_AGENT -d "\$BRIDGE_IP" -p tcp --dport "\$TINYPROXY_PORT" -j RETURN
sudo iptables -A CLAUDE_AGENT -d "\$HOST_IP"   -p tcp --dport "\$INFERENCE_PORT" -j RETURN
sudo iptables -A CLAUDE_AGENT -d "\$BRIDGE_IP" -p udp --dport 53 -j RETURN
sudo iptables -A CLAUDE_AGENT -d "\$BRIDGE_IP" -p tcp --dport 53 -j RETURN
# Allow inter-container traffic on port 8080 for claude-agent → searxng MCP.
# Both containers are on AGENT_NET_CIDR (claude-agent-net user-defined network).
# SearXNG → tinyproxy at BRIDGE_IP:8888 is already covered by the rule above.
if [ "\$LOCAL_SEARCH_ENABLED" = "true" ] && [ -n "\$AGENT_NET_CIDR" ]; then
  sudo iptables -A CLAUDE_AGENT -s "\$AGENT_NET_CIDR" -d "\$AGENT_NET_CIDR" -p tcp --dport 8080 -j RETURN
fi
sudo iptables -A CLAUDE_AGENT -j REJECT --reject-with icmp-admin-prohibited
FWEOF

echo "==> Applying firewall rules in VM"
colima ssh -p "$COLIMA_PROFILE" -- sudo sh < "$TMP_WORK/firewall-apply.sh"

# ── --reload-allowlist: fast-path exit ───────────────────────────────────────
if $RELOAD_ALLOWLIST; then
  entry_count=$(grep -cv -E '^\s*(#|$)' "$ALLOWLIST_FILE" || echo 0)
  echo "==> Allowlist reloaded ($entry_count entries, sandbox: $SANDBOX_NAME)"
  rm -rf "$TMP_WORK"
  trap - EXIT
  exit 0
fi

# ── inference backend preflight (non-fatal) ──────────────────────────────────
echo "==> Probing $INFERENCE_LABEL at http://$HOST_IP:$INFERENCE_PORT from inside VM"
case "$BACKEND" in
  ollama)
    if ! vm_ssh curl -sf --max-time 3 "http://$HOST_IP:$INFERENCE_PORT/api/tags" >/dev/null 2>&1; then
      cat >&2 <<WARN
warning: Ollama not reachable at http://$HOST_IP:$INFERENCE_PORT from inside the
Colima VM. Ensure Ollama is running on the macOS host and bound to 0.0.0.0.
On the host, run once:
    launchctl setenv OLLAMA_HOST 0.0.0.0:$INFERENCE_PORT
and restart the Ollama app. Continuing without local inference.
WARN
    fi
    ;;
  omlx)
    OMLX_CURL_ARGS=(-sf --max-time 3)
    if [[ -n "${OMLX_API_KEY:-}" ]]; then
      OMLX_CURL_ARGS+=(-H "Authorization: Bearer $OMLX_API_KEY")
    fi
    if ! vm_ssh curl "${OMLX_CURL_ARGS[@]}" "http://$HOST_IP:$INFERENCE_PORT/v1/models" >/dev/null 2>&1; then
      cat >&2 <<WARN
warning: omlx not reachable at http://$HOST_IP:$INFERENCE_PORT from inside the
Colima VM. Ensure omlx is running on the host with:
    omlx serve --model-dir ~/models
or via: brew services start omlx
Continuing without local inference.
WARN
    fi
    ;;
esac

# ── build the image if missing ───────────────────────────────────────────────
if ! docker image inspect "$IMAGE_TAG" &>/dev/null; then
  echo "==> Building $IMAGE_TAG from $DOCKERFILE_PATH"
  # Build uses --network=host so RUN steps share the VM's network namespace
  # and bypass the DOCKER-USER bridge firewall. Without this, apt/curl/npm
  # would hit "no route to host" because the default-deny rule rejects
  # everything from the bridge CIDR except the tinyproxy allowlist path, and
  # legacy-builder build ARGs don't reliably forward HTTP_PROXY to every tool
  # (apt, for one, only consults lowercase http_proxy). Runtime containers
  # still attach to the bridge and are firewalled normally.
  docker build --network=host \
    -t "$IMAGE_TAG" -f "$DOCKERFILE_PATH" "$DOCKERFILE_DIR"
fi

# ── SearXNG container lifecycle ──────────────────────────────────────────────
if $LOCAL_SEARCH_ENABLED; then
  echo "==> Starting SearXNG container"
  if ! docker container inspect "$SEARXNG_CONTAINER" &>/dev/null; then
    docker run -d \
      --name "$SEARXNG_CONTAINER" \
      --network "$AGENT_NET_NAME" \
      -v "$SEARXNG_SETTINGS_FILE:/etc/searxng/settings.yml:ro" \
      docker.io/searxng/searxng >/dev/null
    echo "    searxng: created"
  else
    docker start "$SEARXNG_CONTAINER" >/dev/null || true
    echo "    searxng: started (existing container)"
  fi
else
  if docker container inspect "$SEARXNG_CONTAINER" &>/dev/null; then
    echo "==> Removing SearXNG container (--disable-search set)"
    docker rm -f "$SEARXNG_CONTAINER" >/dev/null
  fi
fi

# ── host-side persistent state dirs ──────────────────────────────────────────
mkdir -p "$CLAUDE_CONFIG_DIR" "$OPENCODE_CONFIG_DIR" "$OPENCODE_DATA_DIR" "$PI_CONFIG_DIR/agent" "$AGENTS_SKILLS_DIR"
[[ -f "$CLAUDE_JSON_FILE" ]] || echo '{}' > "$CLAUDE_JSON_FILE"

# ── seed global container CLAUDE.md ──────────────────────────────────────────
# Claude Code auto-injects ~/.claude/CLAUDE.md into every session; the shared
# mount puts this file in scope for all containers. Seed-if-missing;
# --reseed-global-claudemd forces overwrite to pick up template updates.
GLOBAL_CLAUDEMD_FILE="$CLAUDE_CONFIG_DIR/CLAUDE.md"
GLOBAL_CLAUDEMD_TEMPLATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/templates/global-claude.md"
if [[ -f "$GLOBAL_CLAUDEMD_TEMPLATE" ]]; then
  if $RESEED_GLOBAL_CLAUDEMD; then
    cp "$GLOBAL_CLAUDEMD_TEMPLATE" "$GLOBAL_CLAUDEMD_FILE"
    echo "==> Reseeded global CLAUDE.md from template"
  elif [[ ! -f "$GLOBAL_CLAUDEMD_FILE" ]]; then
    cp "$GLOBAL_CLAUDEMD_TEMPLATE" "$GLOBAL_CLAUDEMD_FILE"
    echo "==> Seeded global CLAUDE.md"
  else
    echo "==> Global CLAUDE.md already present, skipping"
  fi
else
  echo "==> Warning: $GLOBAL_CLAUDEMD_TEMPLATE not found; skipping global CLAUDE.md seed" >&2
fi

# ── seed OpenCode AGENTS.md from the same template ───────────────────────────
# OpenCode loads its system instructions via the `instructions` field in
# opencode.json (written below). The claude-dev exceptions at the end of the
# template don't apply inside claude-agent, so strip them when seeding here.
GLOBAL_AGENTSMD_FILE="$OPENCODE_CONFIG_DIR/AGENTS.md"
seed_agentsmd() {
  awk '/^## Differences in claude-dev/{exit} {print}' \
    "$GLOBAL_CLAUDEMD_TEMPLATE" > "$GLOBAL_AGENTSMD_FILE"
}
if [[ -f "$GLOBAL_CLAUDEMD_TEMPLATE" ]]; then
  if $RESEED_GLOBAL_CLAUDEMD; then
    seed_agentsmd
    echo "==> Reseeded OpenCode AGENTS.md from template"
  elif [[ ! -f "$GLOBAL_AGENTSMD_FILE" ]]; then
    seed_agentsmd
    echo "==> Seeded OpenCode AGENTS.md"
  else
    echo "==> OpenCode AGENTS.md already present, skipping"
  fi
fi

# ── inject global ~/.claude/settings.json ────────────────────────────────────
GLOBAL_SETTINGS_FILE="$CLAUDE_CONFIG_DIR/settings.json"
GLOBAL_SETTINGS_TEMPLATE="$SCRIPT_DIR/templates/global-claude-settings.json"
if [[ -f "$GLOBAL_SETTINGS_FILE" ]]; then
  python3 - "$GLOBAL_SETTINGS_FILE" "$GLOBAL_SETTINGS_TEMPLATE" << 'PYEOF'
import json, sys
path = sys.argv[1]
tmpl_path = sys.argv[2] if len(sys.argv) > 2 else None
with open(path) as f:
    data = json.load(f)
changed = False
if data.get('showThinkingSummaries') is not True:
    data['showThinkingSummaries'] = True
    changed = True
if data.get('coauthorTag') != 'none':
    data['coauthorTag'] = 'none'
    changed = True
if 'effortLevel' in data:
    del data['effortLevel']
    changed = True
if 'permissions' not in data and tmpl_path:
    import os
    if os.path.exists(tmpl_path):
        with open(tmpl_path) as tf:
            tmpl = json.load(tf)
        if 'permissions' in tmpl:
            data['permissions'] = tmpl['permissions']
            changed = True
if changed:
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
PYEOF
else
  if [[ -f "$GLOBAL_SETTINGS_TEMPLATE" ]]; then
    cp "$GLOBAL_SETTINGS_TEMPLATE" "$GLOBAL_SETTINGS_FILE"
  else
    echo '{"showThinkingSummaries": true, "coauthorTag": "none"}' > "$GLOBAL_SETTINGS_FILE"
  fi
fi

# ── inject OpenCode config (inference provider) ─────────────────────────────
OPENCODE_CONFIG_FILE="$OPENCODE_CONFIG_DIR/opencode.json"
python3 - \
  "$OPENCODE_CONFIG_FILE" \
  "$BACKEND" \
  "http://$HOST_IP:$INFERENCE_PORT/v1" \
  "http://127.0.0.1:$INFERENCE_PORT/v1,http://$HOST_IP:$INFERENCE_PORT/v1" \
  "${OMLX_API_KEY:-}" \
  "${CLAUDE_AGENT_DEFAULT_MODEL:-}" \
  "${PLAN_MODEL:-}" \
  "${EXEC_MODEL:-}" \
  "${SMALL_MODEL:-}" \
  "$LOCAL_SEARCH_ENABLED" \
  << 'PYEOF'
import json, os, sys, urllib.request, urllib.error
path        = sys.argv[1]
backend     = sys.argv[2]
runtime_url = sys.argv[3]   # what the container will hit (HOST_IP)
probe_urls  = [u for u in sys.argv[4].split(',') if u]
api_key     = sys.argv[5] if len(sys.argv) > 5 else ""
default_model_override = sys.argv[6] if len(sys.argv) > 6 else ""
plan_model  = sys.argv[7] if len(sys.argv) > 7 else ""
exec_model  = sys.argv[8] if len(sys.argv) > 8 else ""
small_model = sys.argv[9] if len(sys.argv) > 9 else ""
local_search_enabled = sys.argv[10].lower() in ("true", "1", "yes") if len(sys.argv) > 10 else False

if os.path.exists(path):
    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}
else:
    data = {}

def discover_models():
    """Try each probe URL in order; return (models_dict, working_url) or ({}, None)."""
    last_err = None
    for base in probe_urls:
        try:
            if backend == "omlx":
                url = base + "/models"
                req = urllib.request.Request(url)
                if api_key:
                    req.add_header("Authorization", f"Bearer {api_key}")
            elif backend == "ollama":
                url = base.replace("/v1", "/api/tags")
                req = urllib.request.Request(url)
            else:
                return {}, None
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read())
                if backend == "omlx":
                    return ({m["id"]: {"name": m["id"]} for m in body.get("data", [])}, base)
                if backend == "ollama":
                    return ({m["name"]: {"name": m["name"]} for m in body.get("models", [])}, base)
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        print(f"[opencode-config] discovery failed across {probe_urls}: {last_err}", file=sys.stderr)
    return {}, None

data.setdefault('$schema', 'https://opencode.ai/config.json')
providers = data.setdefault('provider', {})

if backend == "ollama":
    provider_key, provider_name = "ollama", "Ollama (host)"
elif backend == "omlx":
    provider_key, provider_name = "omlx", "omlx (host)"
else:
    provider_key = None

if provider_key:
    entry = providers.setdefault(provider_key, {
        "npm": "@ai-sdk/openai-compatible",
        "name": provider_name,
        "options": {},
        "models": {},
    })
    opts = entry.setdefault('options', {})
    opts['baseURL'] = runtime_url
    # @ai-sdk/openai-compatible requires a non-empty apiKey in some versions.
    # Ollama ignores the value; omlx uses it if the server enforces auth.
    if backend == "ollama":
        opts['apiKey'] = "ollama"
    elif backend == "omlx" and api_key:
        opts['apiKey'] = api_key
    elif backend == "omlx":
        opts.setdefault('apiKey', "omlx")

    discovered, working_url = discover_models()
    if discovered:
        # Always refresh on success — stale model lists are worse than useless.
        entry['models'] = discovered
        print(f"[opencode-config] discovered {len(discovered)} {backend} models via {working_url}", file=sys.stderr)
    else:
        print(f"[opencode-config] no {backend} models discovered; preserving existing models dict ({len(entry.get('models', {}))} entries)", file=sys.stderr)

    def qualify(m):
        return m if '/' in m else f"{provider_key}/{m}"

    # Default model: env override wins; otherwise reset to a local model whenever
    # the currently-persisted `model` isn't from our active local provider_key
    # (e.g. a stale cloud selection opencode saved on a prior run).
    if default_model_override:
        data['model'] = qualify(default_model_override)
    elif discovered and (not data.get('model') or data['model'].split('/')[0] != provider_key):
        data['model'] = qualify(next(iter(discovered)))

    if small_model:
        data['small_model'] = qualify(small_model)

    if plan_model:
        data.setdefault('agent', {}).setdefault('plan', {})['model'] = qualify(plan_model)
    if exec_model:
        data.setdefault('agent', {}).setdefault('build', {})['model'] = qualify(exec_model)

perms = data.setdefault('permission', {})
perms.setdefault('webfetch', 'allow')
if local_search_enabled:
    mcps = data.setdefault('mcp', {})
    mcps.setdefault('searxng', {
        'type': 'local',
        'command': ['/opt/searxng-mcp/venv/bin/python', '/opt/searxng-mcp/server.py'],
        'environment': {'SEARXNG_URL': 'http://searxng:8080'},
    })
    perms['websearch'] = 'allow'
else:
    perms.setdefault('websearch', 'deny')
    # Remove stale searxng MCP block if local search was previously enabled.
    data.get('mcp', {}).pop('searxng', None)
data['instructions'] = ['/root/.config/opencode/AGENTS.md']
data.setdefault('compaction', {})['auto'] = False
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
PYEOF

# ── inject pi config (inference provider) ────────────────────────────────────
# Re-runs every invocation so model lists stay fresh and existing sandboxes
# get backfilled without --init-sandbox.
PI_MODELS_FILE="$PI_CONFIG_DIR/agent/models.json"
PI_SETTINGS_FILE="$PI_CONFIG_DIR/agent/settings.json"
python3 - \
  "$PI_MODELS_FILE" \
  "$PI_SETTINGS_FILE" \
  "$BACKEND" \
  "http://$HOST_IP:$INFERENCE_PORT/v1" \
  "http://127.0.0.1:$INFERENCE_PORT/v1,http://$HOST_IP:$INFERENCE_PORT/v1" \
  "${OMLX_API_KEY:-}" \
  "${CLAUDE_AGENT_DEFAULT_MODEL:-}" \
  << 'PYEOF'
import json, os, sys, urllib.request, urllib.error
models_path   = sys.argv[1]
settings_path = sys.argv[2]
backend       = sys.argv[3]
runtime_url   = sys.argv[4]
probe_urls    = [u for u in sys.argv[5].split(',') if u]
api_key       = sys.argv[6] if len(sys.argv) > 6 else ""
default_model_override = sys.argv[7] if len(sys.argv) > 7 else ""

if os.path.exists(models_path):
    with open(models_path) as f:
        try:
            models_data = json.load(f)
        except json.JSONDecodeError:
            models_data = {}
else:
    models_data = {}

if os.path.exists(settings_path):
    with open(settings_path) as f:
        try:
            settings_data = json.load(f)
        except json.JSONDecodeError:
            settings_data = {}
else:
    settings_data = {}

def discover_models():
    """Try each probe URL in order; return (list_of_model_ids, working_url) or ([], None)."""
    last_err = None
    for base in probe_urls:
        try:
            if backend == "omlx":
                url = base + "/models"
                req = urllib.request.Request(url)
                if api_key:
                    req.add_header("Authorization", f"Bearer {api_key}")
            elif backend == "ollama":
                url = base.replace("/v1", "/api/tags")
                req = urllib.request.Request(url)
            else:
                return [], None
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = json.loads(resp.read())
                if backend == "omlx":
                    return ([m["id"] for m in body.get("data", [])], base)
                if backend == "ollama":
                    return ([m["name"] for m in body.get("models", [])], base)
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        print(f"[pi-config] discovery failed across {probe_urls}: {last_err}", file=sys.stderr)
    return [], None

providers = models_data.setdefault("providers", {})
provider_key = "local"

entry = providers.setdefault(provider_key, {})
entry["name"] = "Local (Ollama)" if backend == "ollama" else "Local (omlx)"
entry["baseUrl"] = runtime_url
entry["api"] = "openai-completions"
if backend == "ollama":
    entry["apiKey"] = "ollama"
elif backend == "omlx" and api_key:
    entry["apiKey"] = api_key
else:
    entry.setdefault("apiKey", "omlx")
entry["compat"] = {
    "supportsDeveloperRole": False,
    "supportsReasoningEffort": False,
}

discovered, working_url = discover_models()
if discovered:
    entry["models"] = [{"id": m} for m in discovered]
    print(f"[pi-config] discovered {len(discovered)} {backend} models via {working_url}", file=sys.stderr)
else:
    print(f"[pi-config] no {backend} models discovered; preserving existing models list "
          f"({len(entry.get('models', []))} entries)", file=sys.stderr)

with open(models_path, 'w') as f:
    json.dump(models_data, f, indent=2)
    f.write('\n')

# Env override wins; otherwise reset to local when the persisted selection
# isn't from the active local provider (stale cloud choice).
existing_provider = settings_data.get("defaultProvider", "")
existing_model    = settings_data.get("defaultModel", "")
current_model_ids = [m["id"] for m in entry.get("models", [])]

if default_model_override:
    chosen_provider = provider_key
    chosen_model    = default_model_override
elif current_model_ids and (not existing_model or existing_provider != provider_key):
    chosen_provider = provider_key
    chosen_model    = current_model_ids[0]
else:
    chosen_provider = existing_provider
    chosen_model    = existing_model

if chosen_provider or chosen_model:
    settings_data["defaultProvider"] = chosen_provider
    settings_data["defaultModel"]    = chosen_model
    with open(settings_path, 'w') as f:
        json.dump(settings_data, f, indent=2)
        f.write('\n')
PYEOF

# ── agent skills seed (new-container path only) ──────────────────────────────
seed_agent_skills() {
  local src="$SCRIPT_DIR/skills-agents"
  local dest="$AGENTS_SKILLS_DIR"
  if [[ -d "$src" ]]; then
    for skill_path in "$src"/*/; do
      [[ -d "$skill_path" ]] || continue
      local name
      name=$(basename "$skill_path")
      rm -rf "$dest/$name"
      cp -R "$skill_path" "$dest/$name"
      echo "    seeded agent skill: $name"
    done
  else
    echo "    warning: $src not found; skipping agent skill seeding" >&2
  fi
}

# ── skills sync (new-container path only) ────────────────────────────────────
sync_skills() {
  local url="${CLAUDE_SKILLS_ARCHIVE_URL:-https://github.com/aryehj/start-claude/archive/refs/heads/main.tar.gz}"
  local dest="$CLAUDE_CONFIG_DIR/skills"
  echo "==> Syncing skills from upstream into $dest"
  mkdir -p "$dest"
  local tmp
  tmp=$(mktemp -d)
  if curl -fsSL "$url" -o "$tmp/archive.tar.gz" \
     && tar -xzf "$tmp/archive.tar.gz" -C "$tmp"; then
    local src
    src=$(find "$tmp" -maxdepth 2 -type d -name skills | head -n1)
    if [[ -n "$src" && -d "$src" ]]; then
      for skill_path in "$src"/*/; do
        [[ -d "$skill_path" ]] || continue
        local name
        name=$(basename "$skill_path")
        rm -rf "$dest/$name"
        cp -R "$skill_path" "$dest/$name"
        echo "    injected skill: $name"
      done
    else
      echo "    warning: no skills/ directory in upstream archive" >&2
    fi
  else
    echo "    warning: failed to fetch skills from $url (continuing)" >&2
  fi
  rm -rf "$tmp"
}

# ── container run / reattach ─────────────────────────────────────────────────
DOCKER_ENV_ARGS=(
  -e "TERM=${TERM:-xterm-256color}"
  -e "COLORTERM=${COLORTERM:-}"
  -e "TERM_PROGRAM=${TERM_PROGRAM:-}"
  -e "GIT_AUTHOR_NAME=$GIT_USER_NAME"
  -e "GIT_AUTHOR_EMAIL=$GIT_USER_EMAIL"
  -e "GIT_COMMITTER_NAME=$GIT_USER_NAME"
  -e "GIT_COMMITTER_EMAIL=$GIT_USER_EMAIL"
  -e "HTTPS_PROXY=http://$BRIDGE_IP:$TINYPROXY_PORT"
  -e "HTTP_PROXY=http://$BRIDGE_IP:$TINYPROXY_PORT"
  -e "NO_PROXY=localhost,127.0.0.1,$BRIDGE_IP,$HOST_IP,searxng"
  -e "NODE_USE_ENV_PROXY=1"
  -e "TMPDIR=/tmp"
)
case "$BACKEND" in
  ollama)
    DOCKER_ENV_ARGS+=(-e "OLLAMA_HOST=http://$HOST_IP:$INFERENCE_PORT")
    ;;
  omlx)
    DOCKER_ENV_ARGS+=(-e "OMLX_HOST=http://$HOST_IP:$INFERENCE_PORT")
    if [[ -n "${OMLX_API_KEY:-}" ]]; then
      DOCKER_ENV_ARGS+=(-e "OMLX_API_KEY=$OMLX_API_KEY")
    fi
    ;;
esac

# ── inject project settings ───────────────────────────────────────────────────
# Force sandbox off — claude-agent.Dockerfile omits the sandbox deps
# (CAP_SYS_ADMIN would weaken the VM boundary, see Dockerfile header), but
# Claude Code reads settings before checking deps and would error out. The
# VM-level tinyproxy + iptables chain is the real security boundary here.
#
# Runs before the existing-container fast path so re-attaches to containers
# created prior to the bwrap-removal also pick up the migration. The settings
# file is on the host and bind-mounted into the container, so fixing it here
# is sufficient regardless of whether we exec straight into an existing
# container or build a fresh one below.
PROJECT_SETTINGS_FILE="$PROJECT_DIR/.claude/settings.local.json"
if [[ -f "$PROJECT_SETTINGS_FILE" ]]; then
  python3 - "$PROJECT_SETTINGS_FILE" << 'PYEOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)
changed = False
sb = data.setdefault('sandbox', {})
if sb.get('enabled') is not False:
    sb['enabled'] = False
    changed = True
    print(f"==> Set sandbox.enabled=false in {path}")
for key in ('failIfUnavailable', 'allowUnsandboxedCommands'):
    if key in sb:
        del sb[key]
        changed = True
        print(f"==> Removed sandbox.{key} from {path}")
if changed:
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
PYEOF
else
  mkdir -p "$PROJECT_DIR/.claude"
  cat > "$PROJECT_SETTINGS_FILE" << 'JSONEOF'
{
  "sandbox": {
    "enabled": false
  }
}
JSONEOF
  echo "==> Created $PROJECT_SETTINGS_FILE"
fi

attach_existing() {
  echo "==> Attaching to existing container '$CONTAINER_NAME'"
  docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true
  rm -rf "$TMP_WORK"
  trap - EXIT
  exec docker exec -it -w "$PROJECT_DIR" "${DOCKER_ENV_ARGS[@]}" "$CONTAINER_NAME" /bin/bash
}

if docker container inspect "$CONTAINER_NAME" &>/dev/null; then
  # All projects in a sandbox share the same projects mount. Check that the
  # existing container's projects mount matches the current sandbox. If not
  # (sandbox switched), recreate; otherwise just re-exec with the right -w.
  existing_projects_mount=$(docker container inspect -f '{{range .Mounts}}{{if eq .Destination "'"$SANDBOX_ROOT/projects"'"}}{{.Source}}{{end}}{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)
  if [[ "$existing_projects_mount" == "$SANDBOX_ROOT/projects" ]]; then
    running_state=$(docker container inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || echo "false")
    if [[ "$running_state" == "true" ]]; then
      if docker top "$CONTAINER_NAME" 2>/dev/null | grep -q '/bin/bash'; then
        echo "warning: container '$CONTAINER_NAME' already has an attached shell; attaching anyway. Two shells sharing state may be confusing." >&2
      fi
    fi
    attach_existing
  else
    echo "==> Sandbox changed; recreating container"
    docker rm -f "$CONTAINER_NAME" >/dev/null
  fi
fi

# Fresh container: sync skills first.
sync_skills
seed_agent_skills

NETWORK_ARGS=()
if $LOCAL_SEARCH_ENABLED; then
  NETWORK_ARGS=(--network "$AGENT_NET_NAME")
fi

echo "==> Creating container '$CONTAINER_NAME'"
echo "    sandbox  : $SANDBOX_NAME  ($SANDBOX_ROOT)"
echo "    project  : $PROJECT_DIR"
echo "    proxy    : http://$BRIDGE_IP:$TINYPROXY_PORT  (allowlist: $ALLOWLIST_FILE, ro in container)"
echo "    inference: $INFERENCE_LABEL at http://$HOST_IP:$INFERENCE_PORT"
$LOCAL_SEARCH_ENABLED && echo "    search   : SearXNG on $AGENT_NET_NAME"

rm -rf "$TMP_WORK"
trap - EXIT
exec docker run \
  --name "$CONTAINER_NAME" \
  -it \
  --memory "${CLAUDE_AGENT_MEMORY_GB}g" \
  --cpus "$CLAUDE_AGENT_CPUS" \
  --add-host=host.docker.internal:host-gateway \
  ${NETWORK_ARGS[@]+"${NETWORK_ARGS[@]}"} \
  -v "$SANDBOX_ROOT/projects:$SANDBOX_ROOT/projects" \
  -v "$CLAUDE_CONFIG_DIR:/root/.claude" \
  -v "$CLAUDE_JSON_FILE:/root/.claude.json" \
  -v "$OPENCODE_CONFIG_DIR:/root/.config/opencode" \
  -v "$OPENCODE_DATA_DIR:/root/.local/share/opencode" \
  -v "$PI_CONFIG_DIR:/root/.pi" \
  -v "$AGENTS_SKILLS_DIR:/root/.agents/skills" \
  -v "$ALLOWLIST_FILE:/etc/claude-agent/allowlist.txt:ro" \
  -w "$PROJECT_DIR" \
  "${DOCKER_ENV_ARGS[@]}" \
  "$IMAGE_TAG" \
  bash
