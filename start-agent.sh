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
#                  [--refresh-tools] [--restore-previous-image]
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
REFRESH_TOOLS=false
RESTORE_PREVIOUS=false
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
  --refresh-tools        Pull the current OpenCode and Pi (@latest) into the
                         image: tag it as claude-agent:previous, rebuild from
                         the npm layer onward (OS packages, Node, uv and
                         Claude Code stay cached), and recreate the
                         containers like --reset-container. Prints the old
                         and new versions. --rebuild alone may reuse the
                         cached npm layer.
  --restore-previous-image  Point claude-agent:latest back at the image saved
                         by the last --refresh-tools and recreate the
                         containers. --refresh-tools and
                         --restore-previous-image cannot be combined with
                         each other, --rebuild, --reset-container or the
                         allowlist flags.
  --reload-allowlist     Regenerate tinyproxy's filter file from the sandbox's
                         allowlist.txt and reload tinyproxy. Fast path; does
                         not restart the container.
  --reseed-allowlist     Overwrite the sandbox's allowlist.txt with the
                         repo template templates/sandbox-allowlist.txt
                         (previous file kept as allowlist.txt.bak), then
                         reload. Use after pulling repo
                         updates that added new entries to the default list.
                         --reload-allowlist and --reseed-allowlist cannot be
                         combined with --rebuild or --reset-container.
  --reseed-global-claudemd  Overwrite the sandbox's CLAUDE.md with the repo
                         template (default is seed-if-missing).
  --memory=VALUE         VM memory (e.g. 8, 8G, 8GB). Default: 8 GiB.
  --cpus=N               VM CPU count. Default: 6.
  --backend=BACKEND      Local inference backend: ollama or omlx. Default: the
                         one the VM's inference proxy already serves, else
                         ollama.
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
  OMLX_API_KEY           API key for omlx with --backend=omlx. Held by the
                         in-VM inference proxy; not passed into the container.
                         A run without it keeps the key the proxy holds.
  ANTHROPIC_API_KEY      Forwarded into the container when set (Pi cloud
  OPENAI_API_KEY         providers).
  CLAUDE_SKILLS_ARCHIVE_URL  Tarball that skills are synced from on a fresh
                         container. Default: upstream main.
  CLAUDE_AGENT_DEFAULT_MODEL      Default OpenCode model (set via env var; no CLI flag).
  CLAUDE_AGENT_PLAN_MODEL         OpenCode plan-agent model (overridden by --plan-model).
  CLAUDE_AGENT_EXEC_MODEL         OpenCode exec/build-agent model (overridden by --exec-model).
  CLAUDE_AGENT_SMALL_MODEL        OpenCode small model (overridden by --small-model).
  CLAUDE_AGENT_DISABLE_SEARCH=1  Disable SearXNG container, also disabling OpenCode websearch (overridden by --disable-search).
  CLAUDE_AGENT_VERSION_LAG_DAYS  Warn at launch when SearXNG, OpenCode or Pi has an
                         upstream release more than this many days newer than
                         the one in use. Default: 30. Checked at most once a day.
  CLAUDE_AGENT_DISABLE_VERSION_CHECK=1  Turn that launch-time check off.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rebuild)           REBUILD=true ;;
    --reset-container)   RESET_CONTAINER=true ;;
    --refresh-tools)     REFRESH_TOOLS=true ;;
    --restore-previous-image) RESTORE_PREVIOUS=true ;;
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
    # Reject unknown flags here; otherwise a typo (e.g. --rebuilt) lands in
    # POSITIONAL, becomes PROJECT_DIR, and dies later with a cryptic cd error.
    -*)                  echo "error: unknown option '$1'" >&2; usage >&2; exit 2 ;;
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
if $RELOAD_ALLOWLIST && ($REBUILD || $RESET_CONTAINER); then
  echo "error: --reload-allowlist / --reseed-allowlist cannot be combined with --rebuild or --reset-container." >&2
  echo "  Run them separately. Every normal run already pushes allowlist edits to tinyproxy." >&2
  exit 1
fi
if $REFRESH_TOOLS && $RESTORE_PREVIOUS; then
  echo "error: --refresh-tools and --restore-previous-image are mutually exclusive." >&2
  exit 1
fi
if $REFRESH_TOOLS || $RESTORE_PREVIOUS; then
  image_flag="--refresh-tools"; $RESTORE_PREVIOUS && image_flag="--restore-previous-image"
  other_flag=""
  if $REBUILD; then other_flag="--rebuild"
  elif $RESET_CONTAINER; then other_flag="--reset-container"
  elif $RELOAD_ALLOWLIST; then other_flag="--reload-allowlist / --reseed-allowlist"
  fi
  if [[ -n "$other_flag" ]]; then
    echo "error: $image_flag and $other_flag are mutually exclusive." >&2
    echo "  $image_flag already recreates the containers; it keeps the VM and the allowlist as they are." >&2
    exit 1
  fi
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

# The backend is resolved after the VM probe (resolve_backend): a run that
# names none keeps the one the inference proxy already serves. A named one is
# validated here, before any VM work.
REQUESTED_BACKEND="${CLI_BACKEND:-${CLAUDE_AGENT_BACKEND:-}}"
case "$REQUESTED_BACKEND" in
  ""|ollama|omlx) ;;
  *)
    echo "error: unknown --backend value '$REQUESTED_BACKEND'. Must be 'ollama' or 'omlx'." >&2
    exit 1
    ;;
esac

# Echo the backend: the named one, else the one the installed inference proxy
# serves (from its port), else ollama. Switching backends rewrites the proxy
# and the firewall for every attached session, so only an explicit choice may.
resolve_backend() {
  local requested="$1" proxy_port="$2"
  if [[ -n "$requested" ]]; then
    echo "$requested"
    return
  fi
  case "$proxy_port" in
    8000) echo omlx ;;
    *)    echo ollama ;;
  esac
}

# Succeed when the proxy push must carry over the key the installed proxy
# already sends: an omlx proxy staying omlx, on a run with no OMLX_API_KEY.
# Otherwise a run from a shell without the key would strip it, and every omlx
# request from a live session would get a 401. A run with a key still replaces
# it. Takes "set" or "" for the key, never the key itself.
keep_proxy_key() {
  local backend="$1" key_set="$2" proxy_port="$3" proxy_has_key="$4"
  [[ "$backend" == omlx && -z "$key_set" && "$proxy_port" == 8000 \
     && "$proxy_has_key" == true ]]
}

# The key is written into a double-quoted nginx value. Refuse anything nginx
# would interpret rather than trying to escape it. Never echoes the key.
check_omlx_key_safe() {
  case "${OMLX_API_KEY:-}" in
    *[[:space:][:cntrl:]\"\'\$\\\;]*)
      echo "error: OMLX_API_KEY contains a character the inference proxy cannot carry (quote, \$, whitespace, backslash or ;). Use a key without them." >&2
      return 1
      ;;
  esac
}

# Unnamed may still resolve to omlx, so check then too.
if [[ "$REQUESTED_BACKEND" != "ollama" ]]; then
  OMLX_API_KEY="${OMLX_API_KEY:-}"
  check_omlx_key_safe
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
# --refresh-tools keeps the image it replaces here; --restore-previous-image
# moves latest back to it. One generation only.
PREVIOUS_IMAGE_TAG="claude-agent:previous"
DOCKERFILE_PATH="$SCRIPT_DIR/dockerfiles/claude-agent.Dockerfile"
DOCKERFILE_DIR="$(dirname "$DOCKERFILE_PATH")"
CLAUDE_CONFIG_DIR="$SANDBOX_ROOT/.sandbox_config/claude"
CLAUDE_JSON_FILE="$SANDBOX_ROOT/.sandbox_config/claude.json"
OPENCODE_CONFIG_DIR="$SANDBOX_ROOT/.sandbox_config/opencode/config"
OPENCODE_DATA_DIR="$SANDBOX_ROOT/.sandbox_config/opencode/data"
PI_CONFIG_DIR="$SANDBOX_ROOT/.sandbox_config/pi"
# OpenCode/Pi versions in the current image, keyed by image ID. The Dockerfile
# says @latest, so the build is the only place the real versions are known.
TOOL_VERSIONS_FILE="$SANDBOX_ROOT/.sandbox_config/tool-versions"
# Upstream release data for the launch-time update check (check_versions.py,
# ADR-055): fetched at most once a day, host-side only.
VERSION_CHECK_STATE="$SANDBOX_ROOT/.sandbox_config/version-check.json"
AGENTS_SKILLS_DIR="$SANDBOX_ROOT/.sandbox_config/agents/skills"
ALLOWLIST_FILE="$SANDBOX_ROOT/.sandbox_config/allowlist.txt"
# Shared with start-claude.sh, which turns it into sandbox.network.allowedDomains.
ALLOWLIST_TEMPLATE="$SCRIPT_DIR/templates/sandbox-allowlist.txt"
TINYPROXY_PORT=8888
SEARXNG_CONTAINER="searxng"
# Pinned by digest (SearXNG 2026.11.2-cccbc9e15, upstream latest and known good on 2026-11-03). Bump it
# with scripts/bump-searxng.sh, verify with tests/verify-tool-refresh.sh, then
# commit. Any launch replaces a searxng container on another pin (ADR-054).
SEARXNG_IMAGE="docker.io/searxng/searxng@sha256:892ed5710603e38f9dc201e6a0b2f3f14dbdb8108edcadac30e55b165c979fb7"
SEARXNG_DIR="$SANDBOX_ROOT/.sandbox_config/searxng"
SEARXNG_SETTINGS_FILE="$SEARXNG_DIR/settings.yml"
AGENT_NET_NAME="claude-agent-net"
# RFC 5737 TEST-NET-1, never routed. The agent container resolves external names
# through this, so queries leave from the container's namespace and hit the
# CLAUDE_AGENT REJECT. Everything on the proxy path hands hostnames to tinyproxy.
DNS_BLACKHOLE="192.0.2.1"

# ── preflight ────────────────────────────────────────────────────────────────
if ! command -v colima &>/dev/null; then
  echo "error: 'colima' not found. Install with: brew install colima docker" >&2
  exit 1
fi
if ! command -v docker &>/dev/null; then
  echo "error: 'docker' not found. Install with: brew install colima docker" >&2
  exit 1
fi
if ! command -v python3 &>/dev/null; then
  echo "error: 'python3' not found. Install the Xcode Command Line Tools: xcode-select --install" >&2
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
if [[ ! -f "$ALLOWLIST_TEMPLATE" ]]; then
  echo "error: allowlist template not found at $ALLOWLIST_TEMPLATE" >&2
  exit 1
fi

# ── launch-time update check (ADR-055) ───────────────────────────────────────
# One line per pinned component whose newest upstream release is more than
# CLAUDE_AGENT_VERSION_LAG_DAYS (default 30) newer than the one in use. The
# fetch runs detached from this script, so it never delays a launch; the
# report just before attaching reads only the cached state, so a release
# shows up on the launch after the daily fetch that saw it. Silent on any
# error. CLAUDE_AGENT_DISABLE_VERSION_CHECK=1 turns it off.
version_check_enabled() {
  [[ ! "${CLAUDE_AGENT_DISABLE_VERSION_CHECK:-}" =~ ^(1|true|yes)$ ]]
}

version_check_components() {
  $LOCAL_SEARCH_ENABLED && printf '%s\n' searxng
  printf '%s\n' opencode pi
}

# The date in the "known good on YYYY-MM-DD" comment that bump-searxng.sh
# writes above SEARXNG_IMAGE: the last day the pin was upstream latest.
searxng_pinned_on() {
  sed -n 's/^# Pinned by digest (.*known good on \([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\)).*/\1/p' "$1" 2>/dev/null | head -n 1 || true
}

# The subshell exits at once, so the fetch is not this script's child and
# outlives the exec into the container without holding the terminal.
start_version_check() {
  version_check_enabled || return 0
  local components
  components=$(version_check_components)
  # shellcheck disable=SC2086 # one component name per word
  ( python3 "$SCRIPT_DIR/check_versions.py" --fetch-only --state "$VERSION_CHECK_STATE" \
      $components </dev/null >/dev/null 2>&1 & )
  return 0
}

report_version_check() {
  version_check_enabled || return 0
  local specs=() name
  for name in $(version_check_components); do
    case "$name" in
      searxng) specs+=("searxng=$(searxng_pinned_on "$SCRIPT_DIR/start-agent.sh")") ;;
      *)       specs+=("$name=$(tool_version "$name")") ;;
    esac
  done
  python3 "$SCRIPT_DIR/check_versions.py" --cached --state "$VERSION_CHECK_STATE" \
    --lag-days "${CLAUDE_AGENT_VERSION_LAG_DAYS:-30}" "${specs[@]}" </dev/null >&2 2>/dev/null || true
  return 0
}

start_version_check

# ── seed allowlist (first run only) ──────────────────────────────────────────
mkdir -p "$(dirname "$ALLOWLIST_FILE")"
if [[ ! -f "$ALLOWLIST_FILE" ]] || $RESEED_ALLOWLIST; then
  if [[ -f "$ALLOWLIST_FILE" ]]; then
    cp "$ALLOWLIST_FILE" "$ALLOWLIST_FILE.bak"
    echo "==> Backed up existing allowlist to $ALLOWLIST_FILE.bak"
  fi
  # Write into the existing file, never mv over it: allowlist.txt is a
  # single-file bind mount, and a new inode would leave the container on the old one.
  cat "$ALLOWLIST_TEMPLATE" > "$ALLOWLIST_FILE"
  if $RESEED_ALLOWLIST; then
    echo "==> Reseeded allowlist at $ALLOWLIST_FILE (sandbox: $SANDBOX_NAME)"
  else
    echo "==> Seeded allowlist at $ALLOWLIST_FILE (sandbox: $SANDBOX_NAME)"
  fi
fi

# Generate a tinyproxy filter file on the host from the allowlist.
# Each non-comment domain becomes an anchored regex: (^|\.)domain\.tld$
# which matches the domain itself and any subdomain. A leading '*.' and a
# trailing '.' are dropped; anything that is still not a bare domain (a URL, a
# path, a port, a single label like 'com') is skipped with a warning, since it
# could never match a hostname or would allow a whole TLD.
generate_filter_file() {
  local out="$1"
  python3 - "$ALLOWLIST_FILE" > "$out" <<'PYEOF'
import re, sys
label = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
bare_domain = re.compile(rf"^{label}(\.{label})+$")
with open(sys.argv[1]) as f:
    for n, raw in enumerate(f, 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        host = line.lower()
        if host.startswith('*.'):
            host = host[2:]
        if host.endswith('.'):
            host = host[:-1]
        if not bare_domain.match(host):
            print(f"warning: allowlist.txt:{n}: '{line}' is not a bare domain, skipped",
                  file=sys.stderr)
            continue
        # Escape only '.': re.escape emits '\-', which POSIX ERE leaves undefined.
        escaped = host.replace('.', '\\.')
        print(f"(^|\\.){escaped}$")
PYEOF
}

# ── Colima VM bring-up ───────────────────────────────────────────────────────
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
if $REBUILD; then
  echo
  read -r -p "Also delete and recreate the Colima VM '$COLIMA_PROFILE'? This is NOT reversible. [y/N] " confirm
  if [[ "${confirm:-}" =~ ^[Yy]$ ]]; then
    destroy_colima_vm
  else
    echo "==> Keeping Colima VM; only image/container will be rebuilt."
  fi
fi

# ── ensure the VM is running ─────────────────────────────────────────────────
# One `colima list` serves both the running check and the sizing warning.
# Taken after --rebuild's optional VM delete, so it is never stale.
COLIMA_LIST_JSON=$(colima list -p "$COLIMA_PROFILE" --json 2>/dev/null || true)
if ! grep -q '"Running"' <<<"$COLIMA_LIST_JSON"; then
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
    if [[ -n "$COLIMA_LIST_JSON" ]]; then
      # One parse for both fields; colima reports memory in bytes.
      running_sizing=$(printf '%s' "$COLIMA_LIST_JSON" | python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
cpus = d.get("cpus", "")
mem = d.get("memory") or 0
gib = int(mem) // 2**30 if mem else ""
print(f"{cpus}|{gib}")
' 2>/dev/null || true)
      IFS='|' read -r running_cpu running_mem_gib <<<"$running_sizing"
      if [[ -n "$running_cpu" && "$running_cpu" != "$CLAUDE_AGENT_CPUS" ]]; then
        echo "warning: running VM has $running_cpu CPUs; requested $CLAUDE_AGENT_CPUS. Use --rebuild to resize." >&2
      fi
      if [[ -n "$running_mem_gib" && "$running_mem_gib" -ne "$CLAUDE_AGENT_MEMORY_GB" ]]; then
        echo "warning: running VM memory is ${running_mem_gib}GiB; requested ${CLAUDE_AGENT_MEMORY_GB}GiB. Use --rebuild to resize." >&2
      fi
    fi
  fi
fi

# Point this script's docker CLI calls (and the final exec) at Colima's socket
# for this profile. DOCKER_CONTEXT is per-process; switching contexts globally would
# rewrite currentContext in ~/.docker/config.json and redirect every later
# docker command on the Mac.
if docker context inspect "colima-$COLIMA_PROFILE" &>/dev/null; then
  export DOCKER_CONTEXT="colima-$COLIMA_PROFILE"
else
  echo "warning: docker context colima-$COLIMA_PROFILE not found; assuming current context talks to the right daemon." >&2
fi

# Now that docker is reachable, honor --rebuild / --reset-container by removing
# containers (and the image, for --rebuild) from the VM's docker runtime.
# The VM itself was handled earlier (--rebuild only).
if $REBUILD; then
  remove_containers "--rebuild"
  if docker image inspect "$IMAGE_TAG" &>/dev/null; then
    echo "==> --rebuild: removing image '$IMAGE_TAG'"
    docker image rm "$IMAGE_TAG" >/dev/null
  fi
elif $RESET_CONTAINER; then
  remove_containers "--reset-container"
  echo "==> --reset-container: image '$IMAGE_TAG' kept intact"
fi

# ── temp work dir (probe script, config files, firewall script) ───────────────
TMP_WORK=$(mktemp -d)
PROBE_PID=""
# Also reap the backgrounded inference probe (launched below): it is a
# subshell, so kill its `colima ssh` children first, then the subshell. The
# silenced wait reaps it here; otherwise macOS bash prints a "Terminated: 15"
# job notice, with the probe's source, when it reaps it later.
on_exit() {
  if [[ -n "${PROBE_PID:-}" ]]; then
    pkill -P "$PROBE_PID" 2>/dev/null || true
    kill "$PROBE_PID" 2>/dev/null || true
    wait "$PROBE_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP_WORK"
}
trap on_exit EXIT

# ── batch VM probe: all read-only facts in one SSH round-trip ─────────────────
# Replaces separate vm_ssh calls for bridge, host IP, agent-net CIDR,
# tinyproxy install/active status, and the stored config hash. The script is
# written to a host temp file and piped in as stdin — one SSH handshake total.
cat > "$TMP_WORK/vm-probe.sh" <<PROBE_EOF
#!/bin/sh
AGENT_NET_NAME="$AGENT_NET_NAME"

# Bridge gateway + subnet in one inspect (eliminates the former duplicate call).
bridge_info=\$(docker network inspect bridge \\
  -f '{{(index .IPAM.Config 0).Gateway}} {{(index .IPAM.Config 0).Subnet}}' \\
  2>/dev/null || true)
echo "BRIDGE_IP=\${bridge_info%% *}"
echo "BRIDGE_CIDR=\${bridge_info##* }"

# Default-route host IP; fall back to getent if the route table is empty.
host_ip=\$(ip route show default 2>/dev/null | awk '/^default/ {print \$3; exit}')
if [ -z "\$host_ip" ]; then
  for candidate in host.lima.internal host.docker.internal; do
    host_ip=\$(getent hosts "\$candidate" 2>/dev/null | awk '{print \$1; exit}')
    [ -n "\$host_ip" ] && break
  done
fi
echo "HOST_IP=\$host_ip"

# Agent-net CIDR whenever the network exists, whatever the search flag says:
# a container created with search on stays on claude-agent-net, and without
# this the firewall would install no DOCKER-USER jump for its source CIDR.
agent_cidr=\$(docker network inspect "\$AGENT_NET_NAME" \\
  -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)
echo "AGENT_NET_CIDR=\$agent_cidr"

# Tinyproxy package + service status.
tp_status=\$(dpkg-query -W -f='\${Status}' tinyproxy 2>/dev/null || true)
if echo "\$tp_status" | grep -q "install ok installed"; then
  echo "TINYPROXY_INSTALLED=true"
else
  echo "TINYPROXY_INSTALLED=false"
fi
if systemctl is-active --quiet tinyproxy 2>/dev/null; then
  echo "TINYPROXY_ACTIVE=true"
else
  echo "TINYPROXY_ACTIVE=false"
fi

# Stored config hash (used to skip push when nothing changed).
stored_hash=\$(sudo cat /etc/tinyproxy/filter.hash 2>/dev/null || true)
echo "TINYPROXY_STORED_HASH=\$stored_hash"

# Inference proxy (nginx) package + service status and stored config hash.
ngx_status=\$(dpkg-query -W -f='\${Status}' nginx 2>/dev/null || true)
if echo "\$ngx_status" | grep -q "install ok installed"; then
  echo "NGINX_INSTALLED=true"
else
  echo "NGINX_INSTALLED=false"
fi
if systemctl is-active --quiet nginx 2>/dev/null; then
  echo "NGINX_ACTIVE=true"
else
  echo "NGINX_ACTIVE=false"
fi
ngx_hash=\$(sudo cat /etc/nginx/claude-agent-inference.hash 2>/dev/null || true)
echo "NGINX_STORED_HASH=\$ngx_hash"

# What the installed proxy serves: its port (so its backend), and whether it
# sends an omlx key. Yes or no only; the key itself never leaves the VM.
ngx_conf=/etc/nginx/conf.d/claude-agent-inference.conf
ngx_port=\$(sudo sed -n 's/^  listen [^ ]*:\([0-9][0-9]*\) default_server;\$/\1/p' "\$ngx_conf" 2>/dev/null | head -n 1)
echo "NGINX_PORT=\$ngx_port"
if sudo grep -q '^  proxy_set_header Authorization "Bearer ' "\$ngx_conf" 2>/dev/null; then
  echo "NGINX_HAS_KEY=true"
else
  echo "NGINX_HAS_KEY=false"
fi
PROBE_EOF

if ! PROBE_OUTPUT=$(colima ssh -p "$COLIMA_PROFILE" -- sh < "$TMP_WORK/vm-probe.sh" 2>"$TMP_WORK/probe-stderr"); then
  echo "error: could not run the network probe inside Colima VM '$COLIMA_PROFILE':" >&2
  cat "$TMP_WORK/probe-stderr" >&2
  exit 1
fi
PROBE_OUTPUT=$(printf '%s' "$PROBE_OUTPUT" | tr -d '\r')

# Parse KEY=value lines emitted by the probe.
_probe_val() { printf '%s\n' "$PROBE_OUTPUT" | grep "^$1=" | cut -d= -f2-; }

BRIDGE_IP=$(          _probe_val BRIDGE_IP)
BRIDGE_CIDR=$(        _probe_val BRIDGE_CIDR)
HOST_IP=$(            _probe_val HOST_IP)
AGENT_NET_CIDR=$(     _probe_val AGENT_NET_CIDR)
TINYPROXY_INSTALLED=$(_probe_val TINYPROXY_INSTALLED)
TINYPROXY_ACTIVE=$(   _probe_val TINYPROXY_ACTIVE)
TINYPROXY_STORED_HASH=$(_probe_val TINYPROXY_STORED_HASH)
NGINX_INSTALLED=$(    _probe_val NGINX_INSTALLED)
NGINX_ACTIVE=$(       _probe_val NGINX_ACTIVE)
NGINX_STORED_HASH=$(  _probe_val NGINX_STORED_HASH)
NGINX_PORT=$(         _probe_val NGINX_PORT)
NGINX_HAS_KEY=$(      _probe_val NGINX_HAS_KEY)

# Resolved here, the first point that knows what the proxy serves, and before
# anything below reads the backend, its port or its label.
BACKEND=$(resolve_backend "$REQUESTED_BACKEND" "$NGINX_PORT")
case "$BACKEND" in
  ollama)
    INFERENCE_PORT=11434
    INFERENCE_LABEL="Ollama"
    ;;
  omlx)
    INFERENCE_PORT=8000
    INFERENCE_LABEL="omlx"
    ;;
esac
if [[ -z "$REQUESTED_BACKEND" && -n "$NGINX_PORT" ]]; then
  echo "==> Inference backend: $INFERENCE_LABEL (the one the inference proxy already serves; --backend switches it)"
fi

KEEP_PROXY_KEY=false
if keep_proxy_key "$BACKEND" "${OMLX_API_KEY:+set}" "$NGINX_PORT" "$NGINX_HAS_KEY"; then
  KEEP_PROXY_KEY=true
fi
if [[ "$BACKEND" == "omlx" && -z "${OMLX_API_KEY:-}" ]]; then
  if $KEEP_PROXY_KEY; then
    echo "==> OMLX_API_KEY not set; keeping the key the inference proxy already holds"
  else
    echo "warning: OMLX_API_KEY not set. omlx requests from the container will fail if the server requires auth." >&2
  fi
fi

# Fallbacks — same behaviour as the former individual vm_ssh calls.
if [[ -z "$BRIDGE_IP" ]]; then
  BRIDGE_IP="172.17.0.1"
  echo "warning: could not discover docker bridge gateway; falling back to $BRIDGE_IP" >&2
fi
if [[ -z "$BRIDGE_CIDR" ]]; then
  BRIDGE_CIDR="172.17.0.0/16"
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
# Created whatever the search flag says: the agent container is always on it,
# so toggling --disable-search never leaves it on the wrong network.
if [[ -z "$AGENT_NET_CIDR" ]]; then
  vm_ssh docker network create "$AGENT_NET_NAME" >/dev/null
  AGENT_NET_CIDR=$(vm_ssh docker network inspect "$AGENT_NET_NAME" -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null | tr -d '\r' || true)
fi
if [[ -z "$AGENT_NET_CIDR" ]]; then
  AGENT_NET_CIDR="172.20.0.0/24"
  echo "warning: could not discover $AGENT_NET_NAME CIDR; falling back to $AGENT_NET_CIDR" >&2
fi
echo "==> Agent network: $AGENT_NET_NAME cidr=$AGENT_NET_CIDR"

# ── inference backend preflight (non-fatal, overlaps later work) ──────────────
# Launched here — right after HOST_IP is known — as a background job so the
# up-to-3s curl timeout overlaps tinyproxy push, firewall apply, and host-side
# Python config injection. wait_inference_probe() collects the result before any
# exec call so no orphaned process survives exec.
# Up to 3 attempts, 1s apart: a single 3s curl false-fails when the server is
# momentarily slow (starting up, loading a model). Still backgrounded, so the
# extra time overlaps other work.
probe_retry() {
  local i
  for i in 1 2 3; do
    "$@" >/dev/null 2>&1 && return 0
    [[ $i -lt 3 ]] && sleep 1
  done
  return 1
}
# Skipped under --reload-allowlist, which exits before reading the result.
if ! $RELOAD_ALLOWLIST; then
  echo "==> Probing $INFERENCE_LABEL at http://$HOST_IP:$INFERENCE_PORT from inside VM"
  case "$BACKEND" in
    ollama)
      {
        if ! probe_retry vm_ssh curl -sf --max-time 3 "http://$HOST_IP:$INFERENCE_PORT/api/tags"; then
          cat <<WARN
warning: Ollama not reachable at http://$HOST_IP:$INFERENCE_PORT from inside the
Colima VM. Ensure Ollama is running on the macOS host and bound to 0.0.0.0.
On the host, run once:
    launchctl setenv OLLAMA_HOST 0.0.0.0:$INFERENCE_PORT
and restart the Ollama app. Continuing without local inference.
WARN
        fi
      } >"$TMP_WORK/probe-warning" 2>&1 &
      PROBE_PID=$!
      ;;
    omlx)
      # No Authorization header: vm_ssh would put the key on curl's argv in the
      # VM, where ps shows it. This only checks reachability, so no -f: any
      # HTTP answer, 401 included, means omlx is up; only no answer fails.
      {
        if ! probe_retry vm_ssh curl -s -o /dev/null --max-time 3 "http://$HOST_IP:$INFERENCE_PORT/v1/models"; then
          cat <<WARN
warning: omlx not reachable at http://$HOST_IP:$INFERENCE_PORT from inside the
Colima VM. Ensure omlx is running on the host with:
    omlx serve --model-dir ~/models
or via: brew services start omlx
Continuing without local inference.
WARN
        fi
      } >"$TMP_WORK/probe-warning" 2>&1 &
      PROBE_PID=$!
      ;;
  esac
fi

# ── SearXNG settings.yml seed (first run only, local-search path) ────────────
# keep_only keeps an engine but does not enable it: upstream ships bing (and
# now google and qwant) disabled, which left duckduckgo and brave as the only
# web engines, and both rate-limit. The engines: entry turns bing on.
SEARXNG_SETTINGS_CHANGED=false
SEARXNG_BING_BLOCK='engines:
  - name: bing
    disabled: false'
if $LOCAL_SEARCH_ENABLED; then
  mkdir -p "$SEARXNG_DIR"
  if ! $RELOAD_ALLOWLIST && [[ -f "$SEARXNG_SETTINGS_FILE" ]] \
      && ! grep -q '^engines:' "$SEARXNG_SETTINGS_FILE"; then
    # Migration for files seeded before the engines: entry. Only when bing is
    # still in keep_only: an engines: entry for a filtered-out engine has no
    # engine module and breaks SearXNG's startup. Append (>>) keeps the inode
    # the container's bind mount points at. Not under --reload-allowlist: it
    # exits before the lifecycle below, which restarts SearXNG on the change.
    if grep -qE '^[[:space:]]+- bing[[:space:]]*$' "$SEARXNG_SETTINGS_FILE"; then
      printf '\n%s\n' "$SEARXNG_BING_BLOCK" >> "$SEARXNG_SETTINGS_FILE"
      SEARXNG_SETTINGS_CHANGED=true
      echo "==> $SEARXNG_SETTINGS_FILE: enabled the bing engine"
    fi
  fi
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

$SEARXNG_BING_BLOCK
SXNG
    echo "==> Seeded $SEARXNG_SETTINGS_FILE (secret_key generated, proxy=$BRIDGE_IP:$TINYPROXY_PORT)"
  fi
fi

# ── tinyproxy install in VM (idempotent) ─────────────────────────────────────
# Install check uses TINYPROXY_INSTALLED from the batch probe — no extra SSH.
# Check the `tinyproxy` package, not the binary: on Ubuntu 24.04 the package is
# split (`tinyproxy-bin` ships /usr/bin/tinyproxy; `tinyproxy` ships the systemd
# unit + default config). A prior purge of `tinyproxy` can leave the binary
# behind, so a `command -v` check would pass while `systemctl` has no unit.
if [[ "$TINYPROXY_INSTALLED" != "true" ]]; then
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

# Build tinyproxy config and filter on the host.
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

# Push config only when the content changed, tinyproxy is not yet active, or
# --reload-allowlist was passed. Hash covers both tinyproxy.conf and filter so
# any allowlist edit (which changes filter) triggers a push automatically.
# The stored hash is read by the batch probe from /etc/tinyproxy/filter.hash.
NEW_PROXY_HASH=$(cat "$TMP_WORK/tinyproxy.conf" "$TMP_WORK/filter" | shasum -a 256 | awk '{print $1}')
# Write the VM-side push script: both files and the hash ride in it as base64
# (only [A-Za-z0-9+/=], so safe inside single quotes), followed by the
# reload-or-restart, so the whole push costs one colima ssh handshake.
# Encoded up front, not inside the heredoc: a failed $(...) there would
# silently ship an empty tinyproxy.conf, which has no FilterDefaultDeny.
write_tinyproxy_push() {
  local conf_b64 filter_b64
  if ! conf_b64=$(base64 < "$TMP_WORK/tinyproxy.conf" | tr -d '\n') \
     || ! filter_b64=$(base64 < "$TMP_WORK/filter" | tr -d '\n') \
     || [[ -z "$conf_b64" ]]; then
    echo "error: could not encode the tinyproxy config for the VM; not pushing it" >&2
    return 1
  fi
  cat > "$TMP_WORK/tinyproxy-push.sh" <<PUSHEOF
set -e
printf '%s' '$conf_b64' | base64 -d > /etc/tinyproxy/tinyproxy.conf.new
# Never install a config without default-deny: tinyproxy would proxy anywhere.
# Exiting here leaves the old config loaded and the old hash, so the next run
# pushes again.
if ! grep -qx 'FilterDefaultDeny Yes' /etc/tinyproxy/tinyproxy.conf.new; then
  rm -f /etc/tinyproxy/tinyproxy.conf.new
  echo "error: pushed tinyproxy.conf lacks FilterDefaultDeny Yes; refusing it" >&2
  exit 1
fi
mv /etc/tinyproxy/tinyproxy.conf.new /etc/tinyproxy/tinyproxy.conf
printf '%s' '$filter_b64' | base64 -d > /etc/tinyproxy/filter
chmod 644 /etc/tinyproxy/tinyproxy.conf /etc/tinyproxy/filter
# Persist the new hash so the next warm reattach can skip the push.
printf '%s\n' '$NEW_PROXY_HASH' > /etc/tinyproxy/filter.hash
# Reload (no connection drop) when tinyproxy is already running; start it otherwise.
if [ "$TINYPROXY_ACTIVE" = "true" ]; then
  systemctl reload tinyproxy 2>/dev/null || systemctl restart tinyproxy
else
  # restart, not start: on a freshly recreated VM dpkg's postinst auto-starts
  # tinyproxy with its default config *before* the probe-derived TINYPROXY_ACTIVE
  # (read pre-install, so "false") is consulted here. start/enable --now are
  # no-ops against that already-running unit and never load the config pushed
  # above, leaving egress broken until --reload-allowlist. restart loads the
  # pushed config whether the unit was auto-started or not yet running.
  systemctl enable tinyproxy >/dev/null 2>&1 || true
  systemctl restart tinyproxy
fi
PUSHEOF
}

if $RELOAD_ALLOWLIST || [[ "$NEW_PROXY_HASH" != "$TINYPROXY_STORED_HASH" || "$TINYPROXY_ACTIVE" != "true" ]]; then
  write_tinyproxy_push || exit 1
  colima ssh -p "$COLIMA_PROFILE" -- sudo sh < "$TMP_WORK/tinyproxy-push.sh"
fi

# ── inference proxy: nginx in the VM (idempotent) ────────────────────────────
# Default-deny reverse proxy to the inference server: one exact-match location
# per allowed route, a catch-all 403, and credentials overwritten on every
# request. Runs before the firewall so a healthy setup always has a path.
if [[ "$NGINX_INSTALLED" != "true" ]]; then
  echo "==> Installing nginx in Colima VM"
  vm_ssh sudo apt-get update -qq
  vm_ssh sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    -o Dpkg::Options::=--force-confnew nginx
  vm_ssh sudo systemctl daemon-reload
fi

# Built with bash builtins only, so the key never appears on an argv.
#   - `location =` plus an explicit method check, because a prefix location
#     would admit /v1/models/{id}/load, and limit_except GET would admit HEAD.
#   - proxy_pass names a fixed path: nginx matches on the normalized URI but
#     would otherwise forward the raw one, so a traversal or encoded variant
#     could match one route and be forwarded as another.
generate_inference_proxy_conf() {
  local out="$1" auth='""' routes route method path
  case "$BACKEND" in
    omlx)
      routes="POST:/v1/chat/completions GET:/v1/models POST:/v1/messages POST:/v1/messages/count_tokens"
      [[ -n "${OMLX_API_KEY:-}" ]] && auth="\"Bearer $OMLX_API_KEY\""
      ;;
    ollama)
      routes="POST:/v1/chat/completions GET:/v1/models GET:/api/tags"
      ;;
  esac
  {
    cat <<NGINX
# Generated by start-agent.sh. Overwritten when the config changes.
server {
  listen $BRIDGE_IP:$INFERENCE_PORT default_server;
  server_name _;

  # Base64 images are large; the inference server enforces its own limit.
  client_max_body_size 0;

  # Long-running streams: unbuffered, and at least tinyproxy's 600s timeout.
  proxy_http_version 1.1;
  proxy_buffering off;
  proxy_request_buffering off;
  proxy_read_timeout 600s;
  proxy_send_timeout 600s;
  send_timeout 600s;

  # Empty values are not forwarded: no WebSocket upgrades, no client credentials.
  proxy_set_header Connection "";
  proxy_set_header Upgrade "";
  proxy_set_header Authorization $auth;
  proxy_set_header x-api-key "";

NGINX
    for route in $routes; do
      method="${route%%:*}"
      path="${route#*:}"
      cat <<NGINX
  location = $path {
    if (\$request_method != $method) { return 403; }
    proxy_pass http://$HOST_IP:$INFERENCE_PORT$path\$is_args\$args;
  }

NGINX
    done
    # The VM-side guard greps for this exact line.
    cat <<'NGINX'
  location / { return 403; }
}
NGINX
  } > "$out"
}

generate_inference_proxy_conf "$TMP_WORK/inference-proxy.conf"
# The hash covers the key, so rotating it triggers a push.
NEW_INFERENCE_PROXY_HASH=$(shasum -a 256 < "$TMP_WORK/inference-proxy.conf" | awk '{print $1}')

# VM-side push script, sent over stdin so the key is never on an argv. Encoded
# up front for the same reason as write_tinyproxy_push. Fails closed: a config
# lacking the catch-all or failing `nginx -t` is never installed, and the hash
# only advances once nginx has loaded the config, so the next run pushes again.
write_inference_proxy_push() {
  local conf_b64
  if ! conf_b64=$(base64 < "$TMP_WORK/inference-proxy.conf" | tr -d '\n') \
     || [[ -z "$conf_b64" ]]; then
    echo "error: could not encode the inference proxy config for the VM; not pushing it" >&2
    return 1
  fi
  cat > "$TMP_WORK/inference-proxy-push.sh" <<PUSHEOF
set -e
# The config holds the inference key: root-only from the moment it exists.
umask 077
conf=/etc/nginx/conf.d/claude-agent-inference.conf
check=/etc/nginx/claude-agent-inference.check.conf
printf '%s' '$conf_b64' | base64 -d > "\$conf.new"
# KEEP_PROXY_KEY: this run had no OMLX_API_KEY, so copy the installed config's
# Authorization line into the new one. awk reads it from the file; the key is
# never on an argv and never leaves the VM.
if [ "$KEEP_PROXY_KEY" = "true" ] && [ -f "\$conf" ]; then
  awk 'NR == FNR { if (index(\$0, "  proxy_set_header Authorization \"Bearer ") == 1) keep = \$0; next }
       \$0 == "  proxy_set_header Authorization \"\";" && keep != "" { print keep; next }
       { print }' "\$conf" "\$conf.new" > "\$conf.keep"
  mv "\$conf.keep" "\$conf.new"
fi
if ! grep -qxF '  location / { return 403; }' "\$conf.new"; then
  rm -f "\$conf.new"
  echo "error: pushed inference proxy config lacks its catch-all; refusing it" >&2
  exit 1
fi
# Test the candidate on its own, so a bad one is never put where nginx loads it.
printf 'events {}\nhttp { include %s.new; }\n' "\$conf" > "\$check"
if ! nginx -t -q -c "\$check"; then
  rm -f "\$conf.new" "\$check"
  echo "error: pushed inference proxy config failed nginx -t; refusing it" >&2
  exit 1
fi
rm -f "\$check"
mv "\$conf.new" "\$conf"
chmod 600 "\$conf"
# Nothing else may listen, including the package's default site on port 80.
rm -f /etc/nginx/sites-enabled/default
if [ "$NGINX_ACTIVE" = "true" ]; then
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
else
  # restart, not start: dpkg's postinst has already started nginx with its
  # default config, as for tinyproxy above.
  systemctl enable nginx >/dev/null 2>&1 || true
  systemctl restart nginx
fi
# reload exits 0 even when nginx keeps the old config (a new listen address that
# cannot bind; nginx -t never binds), and the hash below would then stop every
# later run from retrying. Confirm the listener; restart once; else fail.
listening() {
  n=0
  while [ "\$n" -lt 10 ]; do
    ss -ltn 2>/dev/null | awk '{print \$4}' | grep -qxF '$BRIDGE_IP:$INFERENCE_PORT' && return 0
    n=\$((n + 1))
    sleep 0.5
  done
  return 1
}
if ! listening; then
  systemctl restart nginx || true
  if ! listening; then
    echo "error: the inference proxy is not listening on $BRIDGE_IP:$INFERENCE_PORT after a restart; see 'journalctl -u nginx' in the VM" >&2
    exit 1
  fi
fi
printf '%s\n' '$NEW_INFERENCE_PROXY_HASH' > /etc/nginx/claude-agent-inference.hash
chmod 600 /etc/nginx/claude-agent-inference.hash
PUSHEOF
}

if [[ "$NEW_INFERENCE_PROXY_HASH" != "$NGINX_STORED_HASH" || "$NGINX_ACTIVE" != "true" ]]; then
  write_inference_proxy_push || exit 1
  if ! colima ssh -p "$COLIMA_PROFILE" -- sudo sh < "$TMP_WORK/inference-proxy-push.sh"; then
    echo "error: could not install the inference proxy in the VM; stopping" >&2
    exit 1
  fi
fi

# ── iptables rules in the VM (CLAUDE_AGENT, _IN and _OUT chains) ─────────────
# Container traffic leaving the VM traverses FORWARD, where DOCKER-USER jumps
# to CLAUDE_AGENT. Container traffic to a VM-local address (either bridge
# gateway, 192.168.5.1, col0) traverses INPUT instead, where CLAUDE_AGENT_IN
# governs it: only tinyproxy and the inference proxy are reachable, not the VM's
# sshd or dnsmasq. No container traffic may reach the host directly; inference
# goes through the proxy. tinyproxy runs in the VM, so its traffic crosses
# neither chain and CLAUDE_AGENT_OUT (from OUTPUT) keeps it off the host.
# All are replaced in a single `iptables-restore --noflush` commit: declaring a
# chain flushes and repopulates only that chain, so there is never a window
# where one is empty (an empty chain falls through and allows all).
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

# Checked up front: inside the pipeline below, this would race iptables-restore
# for the xtables lock. The chain may not exist yet, which also means no jump.
OUTPUT_JUMP_PRESENT=false
sudo iptables -C OUTPUT -j CLAUDE_AGENT_OUT 2>/dev/null && OUTPUT_JUMP_PRESENT=true

# Create-or-replace all three chains atomically, rules in order.
{
  echo '*filter'
  echo ':CLAUDE_AGENT - [0:0]'
  echo ':CLAUDE_AGENT_IN - [0:0]'
  echo ':CLAUDE_AGENT_OUT - [0:0]'
  echo "-A CLAUDE_AGENT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
  # Allow inter-container traffic on port 8080 for claude-agent → searxng MCP.
  # Both containers are on AGENT_NET_CIDR (claude-agent-net user-defined network).
  # SearXNG → tinyproxy at BRIDGE_IP:8888 is INPUT traffic (CLAUDE_AGENT_IN).
  if [ "\$LOCAL_SEARCH_ENABLED" = "true" ] && [ -n "\$AGENT_NET_CIDR" ]; then
    echo "-A CLAUDE_AGENT -s \$AGENT_NET_CIDR -d \$AGENT_NET_CIDR -p tcp --dport 8080 -j RETURN"
  fi
  echo "-A CLAUDE_AGENT -j REJECT --reject-with icmp-admin-prohibited"
  # INPUT side: agent and SearXNG reach tinyproxy and the inference proxy on the
  # docker0 gateway; every other VM-local service (sshd on all addresses, dnsmasq
  # on 192.168.5.1:53) is refused.
  echo "-A CLAUDE_AGENT_IN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
  echo "-A CLAUDE_AGENT_IN -d \$BRIDGE_IP -p tcp --dport \$TINYPROXY_PORT -j RETURN"
  echo "-A CLAUDE_AGENT_IN -d \$BRIDGE_IP -p tcp --dport \$INFERENCE_PORT -j RETURN"
  echo "-A CLAUDE_AGENT_IN -j REJECT --reject-with icmp-admin-prohibited"
  # OUTPUT side: tinyproxy filters by hostname only (FilterURLs No), so an
  # allowlisted name resolving to the host would be an unfiltered path to the
  # inference port. TCP only: it proxies nothing else, and its DNS must keep
  # working if the VM's resolver lives on the host IP.
  # Replies must pass first: tinyproxy's answers to containers also leave through
  # OUTPUT as its user, and container subnets sit inside 172.16.0.0/12. Only
  # connections tinyproxy opens itself are refused.
  echo "-A CLAUDE_AGENT_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
  echo "-A CLAUDE_AGENT_OUT -d \$HOST_IP -p tcp -m owner --uid-owner tinyproxy -j REJECT --reject-with icmp-admin-prohibited"
  # The Mac has other addresses (LAN IP; Ollama binds 0.0.0.0) and the LAN has
  # other devices, so also refuse the private ranges. HOST_IP stays above because
  # it can fall back to 127.0.0.1, outside these. Loopback is deliberately not
  # blocked: DNS may fall back to TCP on 127.0.0.53, and it reaches only VM services.
  for range in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
    echo "-A CLAUDE_AGENT_OUT -d \$range -p tcp -m owner --uid-owner tinyproxy -j REJECT --reject-with icmp-admin-prohibited"
  done
  if [ "\$OUTPUT_JUMP_PRESENT" != "true" ]; then
    echo "-I OUTPUT 1 -j CLAUDE_AGENT_OUT"
  fi
  echo 'COMMIT'
} | sudo iptables-restore --noflush

# Jump into our chain from DOCKER-USER for default bridge traffic (idempotent;
# the jumps are never removed, so adding them separately opens no window).
sudo iptables -C DOCKER-USER -s "\$BRIDGE_CIDR" -j CLAUDE_AGENT 2>/dev/null \
  || sudo iptables -I DOCKER-USER 1 -s "\$BRIDGE_CIDR" -j CLAUDE_AGENT

# Also jump for user-defined agent network traffic. claude-agent-net → tinyproxy
# ($BRIDGE_IP:$TINYPROXY_PORT) does NOT go through FORWARD — $BRIDGE_IP is a
# local address on the VM's docker0 interface, so that traffic hits INPUT and
# CLAUDE_AGENT_IN below. This jump covers egress and the claude-agent →
# searxng:8080 intra-network path, which DOES traverse FORWARD.
if [ -n "\$AGENT_NET_CIDR" ]; then
  sudo iptables -C DOCKER-USER -s "\$AGENT_NET_CIDR" -j CLAUDE_AGENT 2>/dev/null \
    || sudo iptables -I DOCKER-USER 2 -s "\$AGENT_NET_CIDR" -j CLAUDE_AGENT
fi

# INPUT jumps match on source CIDR only, so one rule per container network
# covers every VM-local destination address.
sudo iptables -C INPUT -s "\$BRIDGE_CIDR" -j CLAUDE_AGENT_IN 2>/dev/null \
  || sudo iptables -I INPUT 1 -s "\$BRIDGE_CIDR" -j CLAUDE_AGENT_IN
if [ -n "\$AGENT_NET_CIDR" ]; then
  sudo iptables -C INPUT -s "\$AGENT_NET_CIDR" -j CLAUDE_AGENT_IN 2>/dev/null \
    || sudo iptables -I INPUT 1 -s "\$AGENT_NET_CIDR" -j CLAUDE_AGENT_IN
fi

# IPv6: the chains above are IPv4 only, and the VM shares a private IPv6 prefix
# with the Mac on col0. tinyproxy listens on IPv4 and tries a name's other
# addresses when a connect fails, so it needs no IPv6 beyond loopback: refuse all
# of it rather than list ranges, which would miss the Mac's global addresses. Last,
# so a failure here aborts the run with the IPv4 rules already in place.
OUTPUT6_JUMP_PRESENT=false
sudo ip6tables -C OUTPUT -j CLAUDE_AGENT_OUT6 2>/dev/null && OUTPUT6_JUMP_PRESENT=true
{
  echo '*filter'
  echo ':CLAUDE_AGENT_OUT6 - [0:0]'
  echo "-A CLAUDE_AGENT_OUT6 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
  echo "-A CLAUDE_AGENT_OUT6 -o lo -j RETURN"
  echo "-A CLAUDE_AGENT_OUT6 -p tcp -m owner --uid-owner tinyproxy -j REJECT --reject-with icmp6-adm-prohibited"
  if [ "\$OUTPUT6_JUMP_PRESENT" != "true" ]; then
    echo "-I OUTPUT 1 -j CLAUDE_AGENT_OUT6"
  fi
  echo 'COMMIT'
} | sudo ip6tables-restore --noflush
FWEOF

echo "==> Applying firewall rules in VM"
colima ssh -p "$COLIMA_PROFILE" -- sudo sh < "$TMP_WORK/firewall-apply.sh"

# ── --reload-allowlist: fast-path exit ───────────────────────────────────────
if $RELOAD_ALLOWLIST; then
  entry_count=$(wc -l < "$TMP_WORK/filter" | tr -d ' ')
  echo "==> Allowlist reloaded ($entry_count entries, sandbox: $SANDBOX_NAME)"
  exit 0  # on_exit removes TMP_WORK (no inference probe on this path)
fi

# ── image lifecycle: build if missing, --refresh-tools, --restore-previous-image ──
# Build uses --network=host so RUN steps share the VM's network namespace
# and bypass the DOCKER-USER bridge firewall. Without this, apt/curl/npm
# would hit "no route to host" because the default-deny rule rejects
# everything from the bridge CIDR except the tinyproxy allowlist path, and
# legacy-builder build ARGs don't reliably forward HTTP_PROXY to every tool
# (apt, for one, only consults lowercase http_proxy). Runtime containers
# still attach to the bridge and are firewalled normally.
build_image() {
  docker build --network=host "$@" \
    -t "$IMAGE_TAG" -f "$DOCKERFILE_PATH" "$DOCKERFILE_DIR"
}

# Write the image's OpenCode/Pi versions to $TOOL_VERSIONS_FILE, keyed by
# image ID. A no-op when the file already describes the current image, so it
# starts a throwaway container (no network) once per image, never on a warm
# reattach. Never fails the launch: on any error it warns and keeps the old file.
record_tool_versions() {
  local image_id out
  image_id=$(docker image inspect -f '{{.Id}}' "$IMAGE_TAG" 2>/dev/null) || return 0
  [[ -n "$image_id" ]] || return 0
  if [[ -f "$TOOL_VERSIONS_FILE" ]] && grep -qxF "image_id=$image_id" "$TOOL_VERSIONS_FILE"; then
    return 0
  fi
  out=$(docker run --rm --network none --entrypoint /bin/sh "$IMAGE_TAG" -c \
    'printf "opencode=%s\npi=%s\n" "$(timeout 30 opencode --version 2>/dev/null | head -n 1)" "$(timeout 30 pi --version 2>/dev/null | head -n 1)"' \
    2>/dev/null) || out=""
  if ! grep -q '^opencode=.' <<<"$out" || ! grep -q '^pi=.' <<<"$out"; then
    echo "warning: could not read the OpenCode/Pi versions in $IMAGE_TAG; launch-time version checks will skip them." >&2
    return 0
  fi
  { echo "image_id=$image_id"; printf '%s\n' "$out"; echo "recorded=$(date -u +%Y-%m-%dT%H:%M:%SZ)"; } \
    > "$TOOL_VERSIONS_FILE.tmp" && mv "$TOOL_VERSIONS_FILE.tmp" "$TOOL_VERSIONS_FILE"
  return 0
}

# Always returns 0: under pipefail, a missing file would otherwise abort the
# assignment it sits in.
tool_version() { sed -n "s/^$1=//p" "$TOOL_VERSIONS_FILE" 2>/dev/null | head -n 1 || true; }

# $1: label; $2, $3: the opencode and pi versions read before the image moved.
print_tool_versions_change() {
  echo "==> $1: opencode ${2:-?} → $(tool_version opencode), pi ${3:-?} → $(tool_version pi)"
}

if $REFRESH_TOOLS; then
  old_opencode=""; old_pi=""
  if docker image inspect "$IMAGE_TAG" &>/dev/null; then
    record_tool_versions
    old_opencode=$(tool_version opencode); old_pi=$(tool_version pi)
    docker tag "$IMAGE_TAG" "$PREVIOUS_IMAGE_TAG"
    echo "==> --refresh-tools: saved the current image as $PREVIOUS_IMAGE_TAG (undo with --restore-previous-image)"
  fi
  echo "==> --refresh-tools: rebuilding $IMAGE_TAG from the OpenCode/Pi npm layer onward"
  # A fresh value reruns the npm RUN and every later layer; the layers above
  # the ARG stay cached. A failed build exits here (set -e), leaving latest
  # and the containers as they were.
  build_image --build-arg TOOLS_CACHE_BUST="$(date +%s)"
  remove_containers "--refresh-tools"
  record_tool_versions
  print_tool_versions_change "--refresh-tools" "$old_opencode" "$old_pi"
elif $RESTORE_PREVIOUS; then
  if ! docker image inspect "$PREVIOUS_IMAGE_TAG" &>/dev/null; then
    echo "error: there is no $PREVIOUS_IMAGE_TAG image to restore; --refresh-tools saves one." >&2
    exit 1
  fi
  record_tool_versions
  old_opencode=$(tool_version opencode); old_pi=$(tool_version pi)
  remove_containers "--restore-previous-image"
  docker tag "$PREVIOUS_IMAGE_TAG" "$IMAGE_TAG"
  echo "==> --restore-previous-image: $IMAGE_TAG now points at $PREVIOUS_IMAGE_TAG"
  record_tool_versions
  print_tool_versions_change "--restore-previous-image" "$old_opencode" "$old_pi"
elif ! docker image inspect "$IMAGE_TAG" &>/dev/null; then
  echo "==> Building $IMAGE_TAG from $DOCKERFILE_PATH"
  build_image
  record_tool_versions
fi

# ── SearXNG container lifecycle ──────────────────────────────────────────────
# SearXNG is stateless (settings are bind-mounted), so a container that fails
# to start, or exits straight away, is removed and recreated. No restart
# policy: dockerd would start it at VM boot, before the firewall exists.
if $LOCAL_SEARCH_ENABLED; then
  echo "==> Starting SearXNG container"
  # A container from another pin (a bump or its git revert) is replaced, so
  # changing SEARXNG_IMAGE takes effect on the next launch of any kind.
  searxng_running_image=$(docker container inspect -f '{{.Config.Image}}' "$SEARXNG_CONTAINER" 2>/dev/null || true)
  if [[ -n "$searxng_running_image" && "$searxng_running_image" != "$SEARXNG_IMAGE" ]]; then
    echo "    searxng: pinned image changed; recreating the container"
    docker rm -f "$SEARXNG_CONTAINER" >/dev/null 2>&1 || true
  elif [[ -n "$searxng_running_image" ]] && $SEARXNG_SETTINGS_CHANGED; then
    # SearXNG reads settings.yml only at startup.
    echo "    searxng: settings.yml changed; recreating the container"
    docker rm -f "$SEARXNG_CONTAINER" >/dev/null 2>&1 || true
  fi
  if docker container inspect "$SEARXNG_CONTAINER" &>/dev/null; then
    searxng_err=""
    if searxng_err=$(docker start "$SEARXNG_CONTAINER" 2>&1 >/dev/null) \
        && [[ "$(docker container inspect -f '{{.State.Running}}' "$SEARXNG_CONTAINER" 2>/dev/null)" == "true" ]]; then
      echo "    searxng: started (existing container)"
    else
      echo "warning: SearXNG container failed to start; recreating it." >&2
      [[ -n "$searxng_err" ]] && printf '%s\n' "$searxng_err" | sed 's/^/    /' >&2
      docker rm -f "$SEARXNG_CONTAINER" >/dev/null 2>&1 || true
    fi
  fi
  if ! docker container inspect "$SEARXNG_CONTAINER" &>/dev/null; then
    docker run -d \
      --name "$SEARXNG_CONTAINER" \
      --network "$AGENT_NET_NAME" \
      -v "$SEARXNG_SETTINGS_FILE:/etc/searxng/settings.yml:ro" \
      "$SEARXNG_IMAGE" >/dev/null
    echo "    searxng: created"
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
GLOBAL_CLAUDEMD_TEMPLATE="$SCRIPT_DIR/templates/global-claude.md"
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
# The shared helper owns the merge rules (forced / merged / seeded keys); a
# failure warns, not aborts.
GLOBAL_SETTINGS_FILE="$CLAUDE_CONFIG_DIR/settings.json"
GLOBAL_SETTINGS_TEMPLATE="$SCRIPT_DIR/templates/global-claude-settings.json"
if ! python3 "$SCRIPT_DIR/merge_global_settings.py" "$GLOBAL_SETTINGS_FILE" "$GLOBAL_SETTINGS_TEMPLATE"; then
  echo "WARNING: could not update $GLOBAL_SETTINGS_FILE (see error above); continuing with it as-is" >&2
fi

# ── inject OpenCode config (inference provider) ─────────────────────────────
# Clients reach inference only through the in-VM proxy at $BRIDGE_IP, which
# overwrites client credentials on every request. Some clients need a non-empty
# apiKey, so both configs get this placeholder. The real key is for host-side
# model discovery only: it reaches the injectors through the environment (argv
# shows in host `ps`) and is never written to these bind-mounted files.
INFERENCE_PLACEHOLDER_KEY="not-a-real-key-inference-proxy-injects-credentials"
OPENCODE_CONFIG_FILE="$OPENCODE_CONFIG_DIR/opencode.json"
OMLX_API_KEY="${OMLX_API_KEY:-}" python3 - \
  "$OPENCODE_CONFIG_FILE" \
  "$BACKEND" \
  "http://$BRIDGE_IP:$INFERENCE_PORT/v1" \
  "http://127.0.0.1:$INFERENCE_PORT/v1,http://$HOST_IP:$INFERENCE_PORT/v1" \
  "$INFERENCE_PLACEHOLDER_KEY" \
  "${CLAUDE_AGENT_DEFAULT_MODEL:-}" \
  "${PLAN_MODEL:-}" \
  "${EXEC_MODEL:-}" \
  "${SMALL_MODEL:-}" \
  "$LOCAL_SEARCH_ENABLED" \
  << 'PYEOF'
import json, os, sys, urllib.request, urllib.error
path        = sys.argv[1]
backend     = sys.argv[2]
runtime_url = sys.argv[3]   # what the container will hit (the in-VM proxy)
probe_urls  = [u for u in sys.argv[4].split(',') if u]  # host-side discovery only
placeholder = sys.argv[5]   # the only apiKey ever written
api_key     = os.environ.get("OMLX_API_KEY", "")  # discovery only; never written
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

def scrub_key_copies(node):
    """Replace copies of the real key anywhere in the config, in place."""
    if isinstance(node, dict):
        items = node.items()
    elif isinstance(node, list):
        items = enumerate(node)
    else:
        return
    for k, v in list(items):
        if v in (api_key, f"Bearer {api_key}"):
            node[k] = placeholder
        else:
            scrub_key_copies(v)

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

    discovered, working_url = discover_models()
    if discovered:
        # Always refresh on success — stale model lists are worse than useless.
        entry['models'] = discovered
        print(f"[opencode-config] discovered {len(discovered)} {backend} models via {working_url}", file=sys.stderr)
    else:
        print(f"[opencode-config] no {backend} models discovered; preserving existing models dict ({len(entry.get('models', {}))} entries)", file=sys.stderr)

    # Assume every model takes images. Most local models served here do, and
    # discovery can't tell (/v1/models doesn't say). Without this OpenCode drops
    # attachments before the request. An entry that declares its own is kept,
    # which only matters when discovery failed and the old list was preserved.
    for m in entry.get('models', {}).values():
        if isinstance(m, dict):
            m.setdefault('modalities', {"input": ["text", "image"], "output": ["text"]})
            m.setdefault('attachment', True)

    def qualify(m):
        return m if '/' in m else f"{provider_key}/{m}"

    # Default model: env override wins; otherwise reset to a local model whenever
    # the currently-persisted `model` isn't from our active local provider_key
    # (e.g. a stale cloud selection opencode saved on a prior run).
    if default_model_override:
        data['model'] = qualify(default_model_override)
    elif discovered and (not data.get('model') or data['model'].split('/')[0] != provider_key):
        data['model'] = qualify(next(iter(discovered)))

    # The image pins what gets installed. OpenCode's own major upgrade swaps in a
    # package whose postinstall npm blocks, leaving no working binary.
    data['autoupdate'] = False

    if small_model:
        data['small_model'] = qualify(small_model)

    if plan_model:
        data.setdefault('agent', {}).setdefault('plan', {})['model'] = qualify(plan_model)
    if exec_model:
        data.setdefault('agent', {}).setdefault('build', {})['model'] = qualify(exec_model)

# Both local entries can coexist in this file, and an earlier run may have left
# the real key and the host URL in either. The active backend's proxy is the only
# inference path, so the inactive entry gets the same address.
# @ai-sdk/openai-compatible requires a non-empty apiKey in some versions.
for name in ("ollama", "omlx"):
    local_entry = providers.get(name)
    if isinstance(local_entry, dict):
        if not isinstance(local_entry.get('options'), dict):
            local_entry['options'] = {}
        local_entry['options']['baseURL'] = runtime_url
        local_entry['options']['apiKey'] = placeholder
if api_key:
    scrub_key_copies(data)

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
OMLX_API_KEY="${OMLX_API_KEY:-}" python3 - \
  "$PI_MODELS_FILE" \
  "$PI_SETTINGS_FILE" \
  "$BACKEND" \
  "http://$BRIDGE_IP:$INFERENCE_PORT/v1" \
  "http://127.0.0.1:$INFERENCE_PORT/v1,http://$HOST_IP:$INFERENCE_PORT/v1" \
  "$INFERENCE_PLACEHOLDER_KEY" \
  "${CLAUDE_AGENT_DEFAULT_MODEL:-}" \
  << 'PYEOF'
import json, os, sys, urllib.request, urllib.error
models_path   = sys.argv[1]
settings_path = sys.argv[2]
backend       = sys.argv[3]
runtime_url   = sys.argv[4]  # what the container will hit (the in-VM proxy)
probe_urls    = [u for u in sys.argv[5].split(',') if u]  # host-side discovery only
placeholder   = sys.argv[6]  # the only apiKey ever written
api_key       = os.environ.get("OMLX_API_KEY", "")  # discovery only; never written
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

def scrub_key_copies(node):
    """Replace copies of the real key anywhere in the config, in place."""
    if isinstance(node, dict):
        items = node.items()
    elif isinstance(node, list):
        items = enumerate(node)
    else:
        return
    for k, v in list(items):
        if v in (api_key, f"Bearer {api_key}"):
            node[k] = placeholder
        else:
            scrub_key_copies(v)

providers = models_data.setdefault("providers", {})
provider_key = "local"

entry = providers.setdefault(provider_key, {})
entry["name"] = "Local (Ollama)" if backend == "ollama" else "Local (omlx)"
entry["baseUrl"] = runtime_url
entry["api"] = "openai-completions"
entry["apiKey"] = placeholder
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

# Assume every model takes images, as for OpenCode: without "input", Pi treats
# a model as text-only and drops images. An entry that declares its own is kept
# when discovery failed and the old list was preserved.
for m in entry.get("models", []):
    if isinstance(m, dict):
        m.setdefault("input", ["text", "image"])

if api_key:
    scrub_key_copies(models_data)

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
  -e "NO_PROXY=localhost,127.0.0.1,$BRIDGE_IP,searxng"
  # Lowercase duplicates: apt (and curl/wget/git) only consult lowercase
  # http_proxy/https_proxy, ignoring the uppercase forms Node honors. Without
  # these, apt connects direct to the bridge and hits the CLAUDE_AGENT REJECT
  # ("no route to host"); with them it flows through tinyproxy and is gated by
  # the same hostname allowlist as everything else. No firewall change needed.
  -e "http_proxy=http://$BRIDGE_IP:$TINYPROXY_PORT"
  -e "https_proxy=http://$BRIDGE_IP:$TINYPROXY_PORT"
  -e "no_proxy=localhost,127.0.0.1,$BRIDGE_IP,searxng"
  -e "NODE_USE_ENV_PROXY=1"
  -e "TMPDIR=/tmp"
)
# The in-VM proxy, not the host: it holds the omlx key, so OMLX_API_KEY is never
# forwarded into the container.
case "$BACKEND" in
  ollama)
    DOCKER_ENV_ARGS+=(-e "OLLAMA_HOST=http://$BRIDGE_IP:$INFERENCE_PORT")
    ;;
  omlx)
    DOCKER_ENV_ARGS+=(-e "OMLX_HOST=http://$BRIDGE_IP:$INFERENCE_PORT")
    ;;
esac
# Cloud provider keys for Pi (ADR-036), forwarded only when set on the host.
# Value-less -e: docker reads the value from its own environment, keeping the
# secret out of the docker argv (visible in host `ps`).
for key in ANTHROPIC_API_KEY OPENAI_API_KEY; do
  if [[ -n "${!key:-}" ]]; then
    DOCKER_ENV_ARGS+=(-e "$key")
  fi
done

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
for key in ('theme', 'spinnerTipsEnabled', 'prefersReducedMotion'):
    if key in data:
        del data[key]
        changed = True
        print(f"==> Removed {key} from {path} (belongs in global settings.json)")
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

INFERENCE_STATUS=""
wait_inference_probe() {
  [[ -z "${PROBE_PID:-}" ]] && return
  wait "$PROBE_PID" 2>/dev/null || true
  if [[ -s "$TMP_WORK/probe-warning" ]]; then
    cat "$TMP_WORK/probe-warning" >&2
    INFERENCE_STATUS="  (NOT reachable at startup)"
  fi
  PROBE_PID=""
}

# A container created before the inference proxy keeps its create-time env:
# the old direct-to-host URLs and, for omlx, the real API key. `docker exec -e`
# overrides the URLs for the attached shell, but cannot unset a variable, so the
# key is still inherited by the shell. Report by variable name only; never print
# the value or the env. Warn, do not remove (ADR-048). An inspect failure must
# not abort the script.
warn_if_container_predates_proxy() {
  local env_list="" line has_key=0 has_stale_url=0
  env_list=$(docker container inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER_NAME" 2>/dev/null) || return 0
  while IFS= read -r line; do
    case "$line" in
      OMLX_API_KEY=?*) has_key=1 ;;
      "OLLAMA_HOST=http://$HOST_IP:"*|"OMLX_HOST=http://$HOST_IP:"*) has_stale_url=1 ;;
    esac
  done <<< "$env_list"
  if [[ "$has_key" == 1 ]]; then
    echo "warning: container '$CONTAINER_NAME' predates the inference proxy and still holds the real OMLX_API_KEY in its environment. The agent can read it. Remove it with: start-agent.sh --reset-container" >&2
  elif [[ "$has_stale_url" == 1 ]]; then
    echo "warning: container '$CONTAINER_NAME' predates the inference proxy. Its stale direct-to-host inference URL no longer works. Recreate it with: start-agent.sh --reset-container" >&2
  fi
  return 0
}

attach_existing() {
  echo "==> Attaching to existing container '$CONTAINER_NAME'"
  # Report a failed start instead of letting docker exec die with a bare
  # "container is not running". No automatic rm: the container's root
  # filesystem may hold tools the user installed.
  local start_err=""
  if ! start_err=$(docker start "$CONTAINER_NAME" 2>&1 >/dev/null) \
      || [[ "$(docker container inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null)" != "true" ]]; then
    echo "error: container '$CONTAINER_NAME' failed to start." >&2
    [[ -n "$start_err" ]] && printf '%s\n' "$start_err" | sed 's/^/    /' >&2
    echo "  If it keeps failing, recreate it (keeps the image): start-agent.sh --reset-container" >&2
    exit 1
  fi
  wait_inference_probe
  report_version_check
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
    # --dns is create-time only; a container from before the blackhole keeps
    # resolving external names until it is recreated.
    existing_dns=$(docker container inspect -f '{{json .HostConfig.Dns}}' "$CONTAINER_NAME" 2>/dev/null || true)
    if [[ "$existing_dns" != *"\"$DNS_BLACKHOLE\""* ]]; then
      echo "warning: container '$CONTAINER_NAME' predates the DNS blackhole and can still resolve external names. Recreate it with: start-agent.sh --reset-container" >&2
    fi
    warn_if_container_predates_proxy
    # Containers created with search disabled sat on the default bridge. Join
    # them to claude-agent-net (additive; works stopped or running). Never
    # disconnect: it can strand the container and cuts live shells.
    existing_networks=$(docker container inspect -f '{{json .NetworkSettings.Networks}}' "$CONTAINER_NAME" 2>/dev/null || true)
    if [[ "$existing_networks" != *"\"$AGENT_NET_NAME\""* ]]; then
      echo "==> Connecting '$CONTAINER_NAME' to $AGENT_NET_NAME"
      docker network connect "$AGENT_NET_NAME" "$CONTAINER_NAME" \
        || echo "warning: could not connect '$CONTAINER_NAME' to $AGENT_NET_NAME; search will not resolve. Recreate it with: start-agent.sh --reset-container" >&2
    fi
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
# Images built before the version record existed get one here (a no-op when
# the record already matches the image).
record_tool_versions

wait_inference_probe

echo "==> Creating container '$CONTAINER_NAME'"
echo "    sandbox  : $SANDBOX_NAME  ($SANDBOX_ROOT)"
echo "    project  : $PROJECT_DIR"
echo "    proxy    : http://$BRIDGE_IP:$TINYPROXY_PORT  (allowlist: $ALLOWLIST_FILE, ro in container)"
echo "    inference: $INFERENCE_LABEL via proxy at http://$BRIDGE_IP:$INFERENCE_PORT$INFERENCE_STATUS"
echo "    network  : $AGENT_NET_NAME"
$LOCAL_SEARCH_ENABLED && echo "    search   : SearXNG on $AGENT_NET_NAME"
report_version_check

rm -rf "$TMP_WORK"
trap - EXIT
exec docker run \
  --name "$CONTAINER_NAME" \
  -it \
  --add-host=host.docker.internal:host-gateway \
  --dns "$DNS_BLACKHOLE" \
  --network "$AGENT_NET_NAME" \
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
