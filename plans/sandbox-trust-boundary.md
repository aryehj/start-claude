# Redesign start-agent.sh around a per-sandbox trust boundary

## Status

- [x] Phase 1: Implementation (script changes in `start-agent.sh`)
- [ ] Phase 2: Documentation (CLAUDE.md, ADR-033, README)
- [ ] Phase 3: Validate (syntax, static test, reference sweep, smoke test)

## Context

`start-agent.sh` today scatters its host-side state across several `$HOME` paths and relies on Colima's default `$HOME` virtiofs mount. Concretely:

- `start-agent.sh:191-200` sets `CLAUDE_CONFIG_DIR=$HOME/.claude-containers/shared`, `CLAUDE_JSON_FILE=$HOME/.claude-containers/claude.json`, `OPENCODE_CONFIG_DIR=$HOME/.claude-agent/opencode-config`, `OPENCODE_DATA_DIR=$HOME/.claude-agent/opencode-data`, `ALLOWLIST_FILE=$HOME/.claude-agent/allowlist.txt`, `SEARXNG_DIR=$HOME/.claude-agent/searxng`.
- `start-agent.sh:546-555` calls `colima start` with no `--mount` flag, so the default mounts apply (`$HOME` and `/tmp/colima` into the VM).
- `start-agent.sh:1267-1271` bind-mounts five host paths into the container: the project dir, plus the four state dirs above. All RW.
- `start-agent.sh:186` uses a single shared Colima profile (`claude-agent`) for all projects.

This shape has three concrete consequences. (1) An agent process that escapes the container into the VM has read access to all of `$HOME` via Colima's default virtiofs mount — SSH keys, browser data, dotfiles, Keychain-adjacent state. (2) The `allowlist.txt` is RW from inside the container (tinyproxy reloads only on host-driven `--reload-allowlist`, but the on-disk file is reachable). (3) State is shared across every project, so `~/.claude-containers/shared/.credentials.json` has the same blast radius for every run.

The user wants a redesign where the entire host-side trust surface is exactly one directory. Multiple sandboxes give them a coarse-grained isolation knob; everything inside a sandbox is mutually trusting by design.

Design discussion settled the following before this plan was written:
- Single shared Colima profile (`claude-agent`); narrow VM mount via `colima start --mount $SANDBOX_ROOT:w`. Only one sandbox active at a time; switching restarts the VM with the new mount (`colima stop && colima start --mount $NEW:w`). Per-sandbox profiles were considered and rejected — simultaneous sandbox use is not a requirement, and a single profile keeps the image cache and Colima namespace simple.
- `.sandbox` marker file at the sandbox root — empty, basename-as-name. Detection walks up from `$(pwd)`.
- One layout (option B): `state/claude/` (dir) and `state/claude.json` (file) as siblings; `state/opencode/{config,data}` both RW; `state/allowlist.txt` mounted `:ro` at `/etc/claude-agent/allowlist.txt`; `state/searxng/settings.yml` (existing pattern).
- Source code lives under `$SANDBOX/repos/<repo>/`. PROJECT_DIR must resolve inside `$SANDBOX/repos/`.
- The container does **not** bind-mount `$SANDBOX` as a whole. That's what makes the `:ro` on the allowlist load-bearing — the agent has no other writable path to it.
- No automated migration from `~/.claude-containers/` and `~/.claude-agent/`. CLAUDE.md gets a manual `cp` recipe.
- `start-claude.sh` and `research.py` are out of scope for this plan.

## Goals

- `$SANDBOX_ROOT` is the only host-side path the VM or container can see. Everything outside it is invisible to both.
- The single `claude-agent` Colima profile is launched with `--mount $SANDBOX_ROOT:w` (no default `$HOME` mount). Only one sandbox can be active at a time; switching restarts the VM with the new mount.
- The container's allowlist is bind-mounted `:ro` at a stable in-container path; the agent can read but cannot rewrite which URLs are permitted.
- Sandboxes are detected by an explicit `.sandbox` marker, not by env vars or directory conventions. `--init-sandbox PATH` creates one.
- Running outside a sandbox is a hard error with a clear remediation message.
- No backwards-compatibility shims for the old `$HOME`-scattered layout. Legacy users follow a documented manual migration.

## Approach

The trust boundary moves from "scattered host paths under `$HOME`, plus whatever Colima's default mount drags along" to "exactly `$SANDBOX_ROOT`." Two architectural moves carry the change. First, `colima start --mount $SANDBOX_ROOT:w` replaces the default `$HOME` virtiofs mount, so the VM literally cannot see anything else on the host. Second, the `docker run` mount block stays granular — separate path-renamed bind-mounts for each in-container destination — so we can keep `:ro` on the allowlist meaningful. If we collapsed to a single `$SANDBOX:$SANDBOX` mount instead, the agent would have a writable path to the allowlist via the union view and the `:ro` line elsewhere wouldn't help.

Sandbox detection uses an explicit marker file rather than `$CLAUDE_SANDBOX` env or cwd-based heuristics. Marker files are easy to grep for, hard to forget, and let `cd` into any depth of the sandbox tree work transparently. Sandbox name is the basename of the marker's directory and is used only in log messages; no Colima profile suffix or other shell-unsafe context, so no name-validation regex is needed.

Switching sandboxes requires `colima stop && colima start --mount $NEW_SANDBOX_ROOT:w` because the VM's mount config has to change. That's a ~10s restart, acceptable for coarse-grained sandboxes you switch between rarely. The single shared profile preserves the image-build cache across all sandboxes and avoids any per-sandbox Colima namespacing.

## Unknowns / To Verify

1. **Does `colima start --mount` replace or augment the default `$HOME` mount?** The whole "narrow trust surface" claim depends on `--mount` being a replacement, not additive. If it augments, we'd need an explicit way to suppress defaults. Verify by reading `colima start --help` output for the version on the user's machine, or by post-launch inspection: `colima ssh -p claude-agent -- mount | grep virtiofs` should show only `$SANDBOX_ROOT`, not `$HOME` or `/Users/...`. *Affects: Phase 1, "Narrow VM mount" subsection; if defaults are additive, that subsection needs an extra `--mount-inherit=false` (or equivalent) flag, which may not exist on all Colima versions.*

2. **Does `docker run -v $SANDBOX/repos:$SANDBOX/repos` work without `$SANDBOX` itself being a mount target?** Docker normally creates intermediate parent dirs for mount targets, but verify behavior on Colima's daemon version specifically. Quick check: launch a throwaway container with `-v /a/b:/a/b` where `/a` doesn't exist on the host and is a fresh path; confirm `ls /a` inside the container shows only `b/`. *Affects: Phase 1, "Restructure docker run bind-mounts" subsection.*

3. **Is `/etc/claude-agent/` writable in the image as it stands?** The Dockerfile at `dockerfiles/claude-agent.Dockerfile` may or may not create this directory. Bind-mounting a file at `/etc/claude-agent/allowlist.txt` requires the parent dir to be writable enough that docker can create the mount point — usually fine on `tmpfs`-style overlays but worth checking. If not, add a `RUN mkdir -p /etc/claude-agent` to the Dockerfile. *Affects: Phase 1, "Restructure docker run bind-mounts" subsection; possibly Dockerfile.*

4. **Does OpenCode tolerate `state/opencode/config` being writable but containing a host-side path that may have been edited mid-session?** Pre-redesign behavior is identical (RW), so the answer is "yes, this is current behavior." Not a real unknown — flagged only to confirm we're not regressing anything. No verification needed.

---

## Phase 1: Implementation

All script changes in `start-agent.sh`. Five subsections grouped under one phase because they're inseparable — none ships independently of the others.

### Sandbox detection + `--init-sandbox PATH`

**Steps:**

1. Add `--init-sandbox PATH` to the argparse loop at `start-agent.sh:92-120`. Treat it as a one-shot operation: when set, perform the init and `exit 0` before any VM/container logic runs.

2. Implement `init_sandbox(target_path)`:
   - Reject if `$target_path/.sandbox` already exists (sandbox already initialized — refuse to clobber). An existing `$target_path` directory without a marker is fine: we layer the sandbox structure into it.
   - If `$target_path` does not exist: `mkdir -m 0700 -p "$target_path"`. If it does exist: leave its permissions alone.
   - Create subdirs (idempotent): `state/claude/`, `state/opencode/config/`, `state/opencode/data/`, `state/searxng/`, `repos/`. `mkdir -p` is safe over existing dirs.
   - `touch "$target_path/.sandbox"` (empty marker file).
   - Print a "next step" message: `cd "$target_path/repos" && git clone <repo>`, then `start-agent.sh` from inside the cloned repo.

   Init does **not** seed `state/allowlist.txt` or `state/claude.json`. After the repointing step below, the existing idempotent seed code at `start-agent.sh:234-236` (allowlist heredoc) and `start-agent.sh:928` (`echo '{}' > "$CLAUDE_JSON_FILE"`) seeds them on the first `start-agent.sh` invocation inside the sandbox. No helper extraction needed.

3. Implement `find_sandbox_root()`:
   - Walk up from `$(pwd)`: at each level, test for `-f .sandbox`. Stop at `/`.
   - On hit: echo the dir, return 0. On miss: return 1.

4. After arg parsing (around `start-agent.sh:122` where `PROJECT_DIR` is currently resolved), call `find_sandbox_root` unconditionally. If empty, print a clear error pointing at `--init-sandbox` and `exit 1`.

5. Set `SANDBOX_ROOT` and `SANDBOX_NAME=$(basename "$SANDBOX_ROOT")`. `SANDBOX_NAME` is used only in log messages, so no name-validation regex is needed.

**Acceptance:**

- Running `start-agent.sh` outside any sandbox prints the remediation message and exits non-zero, without starting Colima.
- `start-agent.sh --init-sandbox /tmp/sb-test` creates the directory tree and marker file; running it again refuses (marker already present).
- `start-agent.sh --init-sandbox /some/existing/empty-or-populated/dir` succeeds as long as the dir has no `.sandbox` marker; the structure is layered in alongside whatever is already there.

### Repoint host-state constants under `$SANDBOX/state/`

**Steps:**

1. In the constants block at `start-agent.sh:185-201`, replace:
   - `CLAUDE_CONFIG_DIR` → `$SANDBOX_ROOT/state/claude`
   - `CLAUDE_JSON_FILE` → `$SANDBOX_ROOT/state/claude.json` (sibling, not nested)
   - `OPENCODE_CONFIG_DIR` → `$SANDBOX_ROOT/state/opencode/config`
   - `OPENCODE_DATA_DIR` → `$SANDBOX_ROOT/state/opencode/data`
   - `ALLOWLIST_DIR` (drop)
   - `ALLOWLIST_FILE` → `$SANDBOX_ROOT/state/allowlist.txt`
   - `SEARXNG_DIR` → `$SANDBOX_ROOT/state/searxng`
   - `SEARXNG_SETTINGS_FILE` → `$SANDBOX_ROOT/state/searxng/settings.yml`

2. Audit downstream uses of these constants (state-dir creation at `start-agent.sh:907`, `mkdir -p` calls, the SearXNG seed at `start-agent.sh:695-728`, the global CLAUDE.md / AGENTS.md seed at `start-agent.sh:910-949`, the OpenCode config write at `start-agent.sh:978-1116`, the skills-sync `dest=$CLAUDE_CONFIG_DIR/skills` at `start-agent.sh:1121`). All should still resolve correctly with the new values; no logic changes needed beyond confirming the path strings.

3. Replace the old `mkdir -p "$ALLOWLIST_DIR"` at `start-agent.sh:222` with the new state-dir-creation block (most of which moves to `init_sandbox`; the runtime path can assume the dirs already exist and just `mkdir -p` defensively).

4. Validate `PROJECT_DIR` (resolved at `start-agent.sh:122-123` from `${POSITIONAL[0]:-$(pwd)}`):
   - After resolving to an absolute path, require `$PROJECT_DIR` to be inside `$SANDBOX_ROOT/repos/` (string-prefix check). Reject otherwise with: "PROJECT_DIR ($PROJECT_DIR) must be a subdirectory of $SANDBOX_ROOT/repos/."
   - This forecloses running from `$SANDBOX_ROOT` itself, from `$SANDBOX_ROOT/state/`, or from anywhere outside the sandbox tree.

**Acceptance:**

- All `$HOME/.claude-containers/` and `$HOME/.claude-agent/` references in `start-agent.sh` are gone (verify by grep).
- Running `start-agent.sh` from `$SANDBOX_ROOT/state/` rejects with a clear message; running from `$SANDBOX_ROOT/repos/foo` proceeds.

### Narrow VM mount per active sandbox

**Steps:**

1. `COLIMA_PROFILE` stays at `claude-agent` (`start-agent.sh:186`). Single shared profile. `CONTAINER_NAME` and `docker context use "colima-$COLIMA_PROFILE"` at `start-agent.sh:622` are unchanged.

2. Modify `start_colima_vm()` at `start-agent.sh:546-555` to add `--mount "$SANDBOX_ROOT:w"` to the `colima start` invocation. This replaces the default `$HOME` mount (assuming Unknown #1 resolves as expected; if not, also add whatever flag suppresses default mounts).

3. Detect sandbox-switch on entry. Before the existing "VM already running, no-op" path, query the active VM's mount config via `colima list -j claude-agent | jq …` (verify the JSON shape against the user's Colima version). If the active mount is anything other than `$SANDBOX_ROOT`, log "switching from <old> to $SANDBOX_ROOT", `colima stop`, then fall through to the start path with the new `--mount`. If the active mount already equals `$SANDBOX_ROOT`, keep the existing no-op behavior. If the VM isn't running, just start with the new `--mount`.

4. The `--rebuild` VM-deletion prompt at `start-agent.sh:618` already reads "Also delete and recreate the Colima VM '$COLIMA_PROFILE'?" which still describes the right action under single-profile. No copy change required.

**Acceptance:**

- After `start-agent.sh` from a fresh sandbox, `colima ssh -p claude-agent -- mount | grep virtiofs` shows only `$SANDBOX_ROOT` mounted; `$HOME` is not visible from inside the VM.
- After switching to a second sandbox, the VM restart picks up the new mount and the previous sandbox's path is no longer visible inside the VM. Running `start-agent.sh` again from inside the first sandbox flips back, with another VM restart.

### Restructure `docker run` bind-mounts (allowlist `:ro`)

**Steps:**

1. Replace the mount block at `start-agent.sh:1267-1271` with:
   ```
   -v "$SANDBOX_ROOT/repos:$SANDBOX_ROOT/repos"
   -v "$SANDBOX_ROOT/state/claude:/root/.claude"
   -v "$SANDBOX_ROOT/state/claude.json:/root/.claude.json"
   -v "$SANDBOX_ROOT/state/opencode/config:/root/.config/opencode"
   -v "$SANDBOX_ROOT/state/opencode/data:/root/.local/share/opencode"
   -v "$SANDBOX_ROOT/state/allowlist.txt:/etc/claude-agent/allowlist.txt:ro"
   ```
   The first mount covers all repos and is RW. The next four are state mounts at the paths Claude Code and OpenCode expect. The last is the `:ro` allowlist — the agent can `cat /etc/claude-agent/allowlist.txt` to know what's permitted but cannot rewrite the source of truth. Crucially, do **not** add `-v "$SANDBOX_ROOT:$SANDBOX_ROOT"` — that would expose the allowlist as RW via the union view and defeat the design.

2. Update `attach_existing()` at `start-agent.sh:1175-1181` and the existing-container check at `start-agent.sh:1183-1198`. The current logic recreates the container if `$existing_mount != $PROJECT_DIR` because the project dir was the only mount that varied. With the new architecture, all containers in a sandbox share the same `$SANDBOX_ROOT/repos` mount — a project-dir change means re-`exec`'ing with a new `-w`, not recreating. Simplify the existing-container check accordingly: if the container exists and its `$SANDBOX_ROOT/repos` mount matches, just `docker start` and `docker exec -w "$PROJECT_DIR" …`.

3. If Unknown #3 confirms `/etc/claude-agent/` does not exist in the image, add `RUN mkdir -p /etc/claude-agent` to `dockerfiles/claude-agent.Dockerfile`. Otherwise leave the Dockerfile untouched.

**Acceptance:**

- Inside the running container, `cat /etc/claude-agent/allowlist.txt` succeeds; `echo x >> /etc/claude-agent/allowlist.txt` fails with EROFS.
- Inside the running container, `ls $SANDBOX_ROOT` shows only `repos/` (not `state/` or `.sandbox`).

### Help text, log messages, `--rebuild` prompt

**Steps:**

1. Rewrite the `usage()` block at `start-agent.sh:37-90`:
   - Top-of-file comment: replace the "shared VM + shared container" framing with the per-sandbox model. Note the trust boundary explicitly.
   - USAGE: add the `start-agent.sh --init-sandbox PATH` line.
   - OPTIONS: add `--init-sandbox PATH`. Update the `--reload-allowlist` and `--reseed-allowlist` lines to reference `$SANDBOX_ROOT/state/allowlist.txt` (or a generic "the sandbox's allowlist file" since the path varies by sandbox).
   - ALLOWLIST: section: rewrite path references; the file is now sandbox-relative.
   - ENVIRONMENT: section: unchanged (none of these vars referenced the old paths).

2. Update the "Creating container" log block at `start-agent.sh:1252-1257` to include the sandbox name and root path:
   ```
   sandbox  : <name>  ($SANDBOX_ROOT)
   project  : $PROJECT_DIR
   proxy    : http://$BRIDGE_IP:$TINYPROXY_PORT  (allowlist: $ALLOWLIST_FILE, ro in container)
   inference: $INFERENCE_LABEL at http://$HOST_IP:$INFERENCE_PORT
   ```

3. Update the `--reload-allowlist` exit message at `start-agent.sh:832-838` to mention the sandbox name.

4. Update the seed/reseed messages around `start-agent.sh:512-517` (allowlist seed) — paths in the messages now reference the sandbox.

**Acceptance:**

- `start-agent.sh --help` mentions `--init-sandbox`, the trust boundary, and `$SANDBOX_ROOT/state/allowlist.txt`.
- No help text or log message references `~/.claude-containers/` or `~/.claude-agent/`.

---

## Phase 2: Documentation

### CLAUDE.md decisions block + ADR-033

**Steps:**

1. CLAUDE.md "start-agent.sh key decisions" block (`CLAUDE.md:69-89`):
   - Update the "Colima, one shared VM + one shared container" line: still a single shared `claude-agent` profile, but the VM is now launched with `--mount $SANDBOX_ROOT:w` per active sandbox, replacing the default `$HOME` mount. Only one sandbox can be active at a time; switching restarts the VM with the new mount.
   - Replace the "Allowlist file on the host, not in the repo" line; the allowlist now lives in `$SANDBOX_ROOT/state/allowlist.txt` and is RO from inside the container.
   - Replace the "Shared `~/.claude` state with `start-claude.sh`" line; sandboxes have their own `state/claude/` and there is no longer cross-script sharing. Note this is a deliberate trade-off (lose shared auth, gain trust boundary).
   - Add a one-liner for the `--init-sandbox` UX. Reference the new ADR.

2. Add ADR-033 to `ADR.md`. Title: "One-directory trust boundary for start-agent.sh". Body covers: the threat model (Colima default `$HOME` mount, scattered state, RW allowlist); the design (marker file, single shared profile with per-sandbox narrow `--mount`, granular bind-mounts with `:ro` allowlist); rejected alternatives (per-sandbox Colima profile — adds image-rebuild cost and Colima namespacing for a use case (simultaneous sandboxes) that isn't required; per-project state under `$HOME`; sandbox-as-single-mount; dedicated macOS user); the cost (only one sandbox active at a time, ~10s VM restart on switch, manual migration). Cross-link from ADR-006 and ADR-014 if relevant.

3. Add a short migration recipe to CLAUDE.md (under start-agent.sh's "Making changes" section, or a new "Migrating from legacy state" subsection):
   ```
   start-agent.sh --init-sandbox ~/sandboxes/default
   cp -r ~/.claude-containers/shared/* ~/sandboxes/default/state/claude/
   cp ~/.claude-containers/claude.json ~/sandboxes/default/state/claude.json
   cp -r ~/.claude-agent/opencode-config ~/sandboxes/default/state/opencode/config
   cp -r ~/.claude-agent/opencode-data   ~/sandboxes/default/state/opencode/data
   cp ~/.claude-agent/allowlist.txt      ~/sandboxes/default/state/allowlist.txt
   # Move repos in:  mv ~/Code/foo ~/sandboxes/default/repos/foo
   # Once verified:   rm -rf ~/.claude-containers ~/.claude-agent
   ```

**Acceptance:**

- CLAUDE.md no longer claims state is shared with `start-claude.sh` from `start-agent.sh`'s perspective.
- ADR-033 exists and is the highest-numbered ADR.

### README user walkthrough

**Steps:**

1. Skim the existing README sections that reference start-agent.sh (search for "start-agent" and the legacy paths). Update any path references to be sandbox-relative.

2. Add a "First run" subsection under start-agent.sh's coverage, showing:
   - `start-agent.sh --init-sandbox ~/sandboxes/default` produces the directory tree.
   - A short tree diagram of `~/sandboxes/default/` with `.sandbox`, `state/`, `repos/`.
   - The expected workflow: `cd ~/sandboxes/default/repos && git clone <repo> && cd <repo> && start-agent.sh`.

3. If the README has a "Multiple projects" or "Per-project" subsection, replace it with a "Multiple sandboxes" note: only one sandbox active at a time; switching restarts the shared `claude-agent` VM with the new `--mount`; projects within a sandbox share auth/memory.

**Acceptance:**

- README's start-agent.sh walkthrough starts with `--init-sandbox`.
- No README path references `~/.claude-containers/` or `~/.claude-agent/`.

---

## Phase 3: Validate

**Steps:**

1. `bash -n start-agent.sh` — must pass.

2. Run `python3 tests/test_agent_sh.py`. The static check confirms no `docker run` publishes a host port; the new mount block doesn't add `-p`, so this should still pass.

3. `grep -n -E '(\.claude-containers|\.claude-agent)' start-agent.sh CLAUDE.md README.md` — expect zero hits.

4. `grep -n -E 'CLAUDE_CONFIG_DIR|CLAUDE_JSON_FILE|OPENCODE_CONFIG_DIR|OPENCODE_DATA_DIR|ALLOWLIST_FILE|SEARXNG_DIR|SEARXNG_SETTINGS_FILE' start-agent.sh` — confirm all hits resolve to the new sandbox-relative paths.

5. End-to-end smoke test (manual, requires macOS host with Colima):
   - `start-agent.sh --init-sandbox /tmp/sb-test`.
   - `cd /tmp/sb-test/repos && git clone https://github.com/aryehj/start-claude.git && cd start-claude`.
   - `start-agent.sh` — VM comes up with `--mount /tmp/sb-test:w`, image builds, container launches.
   - Inside the container: `cat /etc/claude-agent/allowlist.txt` (works), `echo x >> /etc/claude-agent/allowlist.txt` (fails EROFS), `ls $SANDBOX_ROOT` (only `repos/` visible).
   - From the host: `colima ssh -p claude-agent -- mount | grep virtiofs` lists only `/tmp/sb-test`; `colima ssh -p claude-agent -- ls $HOME` returns missing/empty.
   - Sandbox-switch test: `start-agent.sh --init-sandbox /tmp/sb-test-2`, `cd /tmp/sb-test-2/repos && git clone …`, `start-agent.sh` — script detects the active mount is `/tmp/sb-test`, stops and restarts the VM with `--mount /tmp/sb-test-2:w`. `colima ssh -p claude-agent -- mount` confirms only the new path is visible.
   - `start-agent.sh --reload-allowlist` from the host updates tinyproxy without restarting the container.
   - `start-agent.sh --rebuild` removes the `claude-agent` VM with the existing prompt copy.

**Acceptance:**

- All static checks pass.
- The smoke test confirms (a) VM mount narrowing actually narrows, (b) allowlist is RO from inside, (c) sandbox-switch correctly stops and restarts the shared `claude-agent` VM with the new mount.

---

## Notes

- **Sandbox switching cost.** Switching from one sandbox to another requires `colima stop && colima start --mount $NEW:w` because the VM's mount config has to change. Roughly a 10-second restart on a warm host. Acceptable for coarse-grained sandboxes you switch between rarely. If switching becomes friction in practice, a follow-up plan could re-introduce per-sandbox Colima profiles to enable simultaneous use — but that brings back per-sandbox image-rebuild cost and Colima-namespacing complexity, so it's deliberately deferred.

- **start-claude.sh and research.py are out of scope.** `start-claude.sh` shares the same `$HOME`-scattered-state pattern but uses Apple Containers, which has no default `$HOME` VM mount, so only the state-sharing half of this redesign would apply there. `research.py` already has its own Colima profile (`research`) but inherits the default `$HOME` mount; the full redesign applies in principle. Both are separate plans.

- **No backwards-compatibility shim for `~/.claude-containers/` or `~/.claude-agent/`.** The script does not detect or migrate from those paths automatically. If a user runs the new script with the old paths still present, they're simply ignored; the manual migration recipe in CLAUDE.md is the supported path.

- **Marker file is empty by design.** Future use cases (per-sandbox name override, allowlist-template selection, default-VM-size override) could add structured fields, but adding now would be premature. Land empty; structure later only when something forces it.
