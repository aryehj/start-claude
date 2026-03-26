# start-claude.sh — Roadmap

## Done

- **Base container** — Debian bookworm-slim with git, curl, ripgrep, fd, jq, build tools
- **Node.js LTS + Claude Code CLI** — `claude` available on PATH inside container
- **uv + uvx** — installed via official installer, symlinked to `/usr/local/bin`
- **Volume mounts** — project dir → `/workspace`, `~/.claude` → `/root/.claude`
- **Idempotent runs** — re-attaches to existing container instead of recreating

---

## Known Issues

- **Inconsistent entry paths** — `container run` (new container) gives a direct interactive session, while `start` + `exec -it bash` (existing container) spawns a second shell. Environment and working directory may differ between the two paths.
- **`container inspect` swallows errors** — `2>/dev/null` hides daemon failures; a non-"[]" result is treated as "exists," which could misfire if the daemon is down or returns unexpected output.
- **Trap/cleanup overlap** — the EXIT trap and the explicit `container rm` + `trap -` on the happy path both try to remove the setup container. Works due to `|| true`, but the explicit rm and trap clear (lines 75-76) could be dropped in favor of letting the trap handle all cleanup.
- **No container name validation** — `basename` can produce names with spaces or special characters that `container` may reject.
- **`~/.claude` assumed to exist** — if the directory is missing, the bind mount may fail or create it as root-owned. A `mkdir -p` before mounting would be safer.
- **Duplicate `apt-get` cache cleanup** — `rm -rf /var/lib/apt/lists/*` runs twice (after system packages and after Node); the first is wasted since the Node setup re-runs apt.

---

## To Do

### Firewall of some sort
