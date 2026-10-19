# offline-agent.py: no-egress local-model agent over a copy-on-write view of cwd

## Status

- [x] Phase 1: Go/no-go on overlayfs-over-virtiofs and offline harness startup (human gate: run on the Mac)
- [x] Phase 2: Core script usable end to end on a real project (human gate: drive a task with a local model)
- [x] Phase 3: Bound agent storage; no agent write path to the Mac (human gate: fill each sink on the Mac and confirm only that session breaks)
- [ ] Phase 4: Export/apply surface, docs, ADR (human gate: does `--apply` feel safe enough to use)

## Context

`start-agent.sh` gives a local model a sandbox with a curated egress allowlist;
`research.py` gives Vane a denylist. Neither offers the third point on the
line: an agent with **no egress at all**, pointed at whatever directory the
user is standing in, whose writes are quarantined until the user pulls them
out. Decisions settled during scoping:

- **Egress for the agent: none.** The only permitted destination is the local
  inference server on the macOS host (`$HOST_IP:$INFERENCE_PORT`). No proxy,
  no DNS, no SearXNG, no Claude Code (needs Anthropic API).
- **Egress for the user: image rebuild only.** Adding tools means editing a
  trailing "user packages" block in a new Dockerfile and `--rebuild`. The
  installer-container and pause-window designs were considered and rejected
  as complexity that is not yet warranted. Docker layer caching makes the
  incremental cost of this ≈ the new packages, provided additions land in the
  trailing block and never in the base layers.
- **Read scope: the directory the script is run from, nothing else.** Same
  ergonomics as `start-agent.sh`, without the `.sandbox_config/`+`projects/`
  tree.
- **Write model: copy-on-write overlay whose upper layer lives on the Colima
  VM disk.** Inside the container the agent sees cwd at its real host path,
  fully writable (including `git commit`). The host directory is the
  read-only lower layer; nothing the agent writes reaches the Mac filesystem
  until the user runs `--diff` / `--export` / `--apply`. This closes the
  sync-client exfil channel (iCloud/Dropbox uploading agent output) and keeps
  hook-like files (`.git/hooks`, `.envrc`, `.vscode/tasks.json`, shell rc)
  from running on the host without review.
- **Packaging: new stdlib-only Python script `offline-agent.py`**, modelled on
  `research.py` (`Paths`, `VmConfig`, `probe_vm`, `render_iptables_apply_script`,
  `start_or_recreate`, background inference probe). Own Colima profile
  `offline-agent`, state in `~/.offline-agent/`, image from
  `dockerfiles/offline-agent.Dockerfile`, both harnesses (OpenCode, Pi)
  installed, both inference backends supported with omlx as default
  (ADR-012).

Prior art to lean on, by file:line:

- `research.py:730-776` (`ensure_colima_vm`: vz + virtiofs, no `--mount`),
  `:777-840` (`probe_vm` single-SSH fact gathering), `:513-578` (iptables
  render), `:697-718` (`start_or_recreate`), `:1177-1237` (inference probe on
  a background thread), `:1291` (`main` flow).
- `start-agent.sh:861-927` (`CLAUDE_AGENT` + `CLAUDE_AGENT_IN` chains: the
  INPUT-side chain matters here because there is no proxy to carve out, so
  every VM-local service must be unreachable), `:1078-1215` (opencode.json
  injection with model discovery), `:1220-1340` (Pi `models.json` +
  `settings.json` injection), `:1563-1580` (`docker run` shape: `--dns
  192.0.2.1`, `--add-host`, config mounts).
- `dockerfiles/claude-agent.Dockerfile` for the Node/uv/opencode/pi install
  lines to copy; drop LibreOffice, the SearXNG shim, doc-tools, and Claude Code.
- `tests/test_agent_sh.py` for the static-invariant test style;
  `tests/test_research.py` for pure-helper tests.

## Goals

- `cd ~/some/project && offline-agent.py` lands in a shell inside the
  container with cwd at its host path, writable, with `opencode` and `pi` on
  PATH and wired to the local model. `curl https://example.com` and any DNS
  lookup fail; the inference endpoint works.
- The host directory is byte-identical after the session until the user runs
  `--apply`. `--diff` shows what changed; `--export PATH` writes a patch (git
  repos) or tarball (otherwise); `--discard` drops the overlay.
- Overlay state survives `colima stop` and script re-runs, keyed by cwd.
- Adding a package = edit one Dockerfile block + `--rebuild`; base layers are
  cache hits.
- Runs alongside `claude-agent` and `research` VMs; no published host ports.
- Pure helpers covered by `tests/test_offline_agent.py`; Dockerfile and
  `docker run` invariants covered by static tests.

## Approach

The overlay is a Docker `local` volume with overlayfs mount options
(`type=overlay`, `o=lowerdir=<cwd as seen in the VM>,upperdir=…,workdir=…`).
Colima mounts `$HOME` into the VM at the same path by default
(`start-agent.sh:504-505` observes this; Colima's own template says `$HOME` is
mounted writable), so the lower layer is simply the host path and **no VM
restart is needed when switching projects**, unlike `start-agent.sh`. Upper and
work dirs live on the VM's native disk under a per-session directory keyed by a
hash of `realpath(cwd)`, which satisfies overlayfs's rule that the upper layer
may not be a network filesystem. The kernel docs do not state whether a
virtiofs lower layer is supported, so that is the Phase 1 go/no-go. If it
fails, the fallback is a snapshot copy of cwd into a plain VM volume at
session start with the diff computed against the live host path at export
time; the rest of the plan is unchanged.

Containers are ephemeral (`docker run --rm`), because nothing worth keeping
lives in the container: packages belong in the image, overlay state in the
volume, harness state in `~/.offline-agent/`. This removes the whole
reattach/`--reset-container` surface that `start-agent.sh` carries.

The VM is a trusted layer, as in the sibling scripts: the docker daemon runs as
VM root and can see `$HOME`. The read-only guarantee the agent experiences
comes from the overlay's lower layer and the container's mount set, not from a
VM-level `:r` mount. See Unknowns for an optional hardening check.

## Unknowns / To Verify

1. **overlayfs with a virtiofs lowerdir, mounted through Docker's `local`
   volume driver.** Load-bearing for Phase 2+. Verify in Phase 1 on the Mac:
   `docker volume create --driver local --opt type=overlay --opt
   device=overlay --opt o=lowerdir=…,upperdir=…,workdir=…`, then write,
   delete, rename, and `git commit` from a container and confirm the host dir
   is untouched and the upper dir holds the changes (deletions appear as
   whiteout char devices). Also confirm whether upper/work dirs must pre-exist
   (expected yes; create via `colima ssh -- sudo mkdir -p`).
2. **OpenCode and Pi start with zero egress and blackholed DNS.** OpenCode has
   an `autoupdate` config key (`false` disables downloads at startup); it also
   fetches `.well-known/opencode` on provider auth, which should not fire for a
   local provider. Pi's update/telemetry behaviour is unverified. Verify in
   Phase 1 by launching both in a `--network none` container built from the
   existing `claude-agent:latest` image with the planned config; record
   startup time and any hard failures.
3. **Whiteout and rename handling on export.** Overlay upper dirs encode
   deletions as char devices and renames as redirect xattrs, so copying the
   upper dir is not a diff. Decision for Phase 4: compute changes from the
   *merged* view, never from the upper dir: `git status`/`git diff`/`git
   format-patch` for repos, `rsync -n --delete --itemize-changes` from merged
   to host for non-repos. Confirm `.git` inside the merged view behaves (object
   writes land in upper) in Phase 1.
4. **Optional VM-level hardening: can `colima start --mount "$HOME:r"` replace
   the default writable `$HOME` mount, or does it coexist with it?** Not
   required for the design; if it works cleanly it makes the ADR's trust
   statement stronger. Check in Phase 2 with `mount -t virtiofs` inside the VM.
5. **Exact model-discovery endpoints for omlx vs Ollama** are already
   encoded in `start-agent.sh:1112-1140`; port, don't re-derive.

## Phase 1: Go/no-go on overlayfs-over-virtiofs and offline harness startup

Human gate: this runs on the Mac (Colima is not reachable from inside a dev
container). Nothing in the repo changes except this plan's Notes.

### Steps

1. Start a Colima profile named `offline-agent` with the same flags as
   `research.py:762-773` (vz, virtiofs, docker runtime, `--network-address`).
   Use the real profile name so the VM is not wasted if Phase 2 proceeds.
2. Exercise Unknown #1 against a scratch git repo under `$HOME`: create the
   overlay volume, run busybox/debian with the volume mounted at the repo's
   host path, perform write/delete/rename/`git commit`, verify host-side
   `git status` is clean and the upper dir shows the changes. Stop and restart
   the container and confirm the upper layer persists; `colima stop`/`start`
   and confirm again.
3. Exercise Unknown #2: run `claude-agent:latest` (from the `claude-agent`
   profile's docker context, or `docker save`/`load` it into the new VM) with
   `--network none --dns 192.0.2.1`, an `opencode.json` containing
   `autoupdate: false` and a local provider pointing at an unreachable URL,
   and launch `opencode` and `pi`. Record startup latency and failure modes.
4. Record results in this plan's Notes. If overlay fails outright, switch the
   Phase 2 design to the snapshot-copy fallback described in Approach and note
   the disk/startup cost.

## Phase 2: Core script usable end to end on a real project

Human gate: the user drives a real task against a real project with a local
model and confirms the firewall, the read-only host dir, and the harness
wiring all behave.

### Steps

1. Create `dockerfiles/offline-agent.Dockerfile`: `debian:bookworm-slim`,
   system packages, Node LTS, uv, and the opencode/pi npm install, copied from
   `claude-agent.Dockerfile`; drop everything else. End with a clearly marked
   `# ── user packages ──` `RUN` block (initially a no-op) and a comment
   explaining that additions go there to keep base layers cached. Keep the
   `UV_PROJECT_ENVIRONMENT` redirect (ADR-007 still applies).
2. Create `offline-agent.py` with `research.py`'s skeleton: `Paths`
   (`~/.offline-agent/` with `opencode/config`, `opencode/data`, `pi`),
   `VmConfig` (profile `offline-agent`, backend default `omlx`, sizing env vars
   `OFFLINE_AGENT_MEMORY` / `OFFLINE_AGENT_CPUS` / `OFFLINE_AGENT_BACKEND`),
   `ensure_colima_vm` (no `--mount`), `ensure_docker_context`, `probe_vm`
   (bridge IP/CIDR, host IP, image presence, chain presence, session dir
   listing), background inference probe. argparse flags: `--rebuild`,
   `--backend`, `--memory`, `--cpus`; the overlay flags arrive in Phase 4.
3. Resolve the project: `realpath(os.getcwd())`; hard-error with remediation
   if it is not under `$HOME` (Colima does not mount it) or if it is `$HOME`
   itself. Derive a session id (short hash of the path) and VM-side dirs
   `/var/lib/offline-agent/sessions/<id>/{upper,work}` plus a `path` marker
   file for `--list-sessions` later. Create them via `vm_sh` with sudo.
4. Image: build `offline-agent:latest` from the Dockerfile when absent
   (`docker build --network=host`, per ADR-011). `--rebuild` removes image
   and volume-less containers, then rebuilds; the Colima VM delete prompt
   must warn when any session dir under `/var/lib/offline-agent/sessions/`
   is non-empty, since VM deletion destroys un-exported agent work.
5. Firewall: port `render_iptables_apply_script` with an `OFFLINE_AGENT`
   chain hooked from `DOCKER-USER` (RETURN only ESTABLISHED,RELATED and
   `$HOST_IP:$INFERENCE_PORT`; REJECT the rest, no DNS or proxy carve-outs)
   and an `OFFLINE_AGENT_IN` chain hooked from `INPUT` that REJECTs all new
   container→VM traffic (mirror `start-agent.sh:884-927`, minus the tinyproxy
   RETURN). Apply as one `iptables-restore --noflush` commit as start-agent
   does, so neither chain is empty mid-apply. Use a dedicated bridge network
   `offline-agent-net` so the chain's source match is tight, following
   `research.py:880-918`.
6. Harness wiring: port the opencode.json and Pi `models.json`/`settings.json`
   injection from `start-agent.sh:1078-1340` into native Python functions
   (pure merge logic separated from the probe so it is unit-testable). Set
   `autoupdate: false`, `permission.webfetch: "deny"`,
   `permission.websearch: "deny"`, no `mcp` block. Apply whatever Pi offline
   settings Phase 1 found necessary. Honour `OFFLINE_AGENT_DEFAULT_MODEL`,
   `OMLX_API_KEY`.
7. Container launch: `docker run --rm -it` on `offline-agent-net`, `--dns
   192.0.2.1`, `--add-host host.docker.internal:$HOST_IP` (the VM default
   route, not `host-gateway`; see the comment at `research.py:1151-1156`),
   `-w <cwd>`, the overlay volume mounted at `<cwd>`, the three harness state
   dirs from `~/.offline-agent/`, `TERM`/`COLORTERM`/git identity env as the
   sibling scripts do. No `-p`. No `HTTP_PROXY`/`HTTPS_PROXY`. Drop into
   `bash`; the user runs `opencode` or `pi` themselves, as with
   `start-agent.sh`.
8. Tests: `tests/test_offline_agent.py` for the iptables render (no DNS
   RETURN, inference RETURN present, final REJECT), session-id derivation,
   cwd validation, and the opencode/pi config merge (autoupdate false,
   webfetch/websearch deny, existing user keys preserved). Static checks on
   the Dockerfile (opencode and pi install lines present; no Claude Code
   installer; user-packages block is last `RUN`) and on `offline-agent.py`'s
   `docker run` argv (no `-p`, `--dns` blackhole, `--rm`, no proxy env). Put
   the Dockerfile checks in `tests/test_dockerfile.py` next to the existing
   ones if that reads cleanly, else a sibling file.
9. Add `tests/test-offline-agent-firewall.sh` in the style of
   `tests/test-agent-firewall.sh`: from inside the container, inference
   endpoint reachable; `curl https://example.com`, `curl http://$BRIDGE_IP:22`,
   `getent hosts example.com` all fail fast.

### Acceptance criteria

- After a session that creates, edits, and deletes files and commits in the
  overlay, `git status` on the host is clean and `ls -la` shows no new
  entries. Checked by hand at the gate; automated coverage waits for Phase 4's
  `--diff`.
- `offline-agent.py` and `start-agent.sh` run concurrently without either
  firewall affecting the other (`tests/test-cross-vm-isolation.sh` pattern,
  extended or copied).

## Phase 3: Bound agent storage; no agent write path to the Mac

Human gate: on the Mac, the user fills each write sink from inside a session
and confirms the agent gets `ENOSPC`/OOM in that session only, the VM and
other sessions keep working, and the Mac's free space does not move beyond the
session's cap.

### Context

Phase 2 leaves four places the agent can write, none with a cap smaller than
"the disk":

| Sink | Lands on | Bound today |
|---|---|---|
| Overlay upper dir (`sessions/<sid>/upper`) | VM root disk | Colima's default VM disk (100 GiB sparse file on the Mac; `ensure_colima_vm` passes no `--disk`) |
| Container writable layer (`/tmp`, `/root/.cache`, …) | VM `/var/lib/docker` | same |
| `json-file` log of the `-it` TTY stream | VM `/var/lib/docker/containers` | same |
| Harness bind mounts (`~/.offline-agent/{opencode,pi}`) | **Mac APFS via virtiofs** | Mac free space |

Disk exhaustion is a denial-of-service, not an exfil channel: nothing outside
the VM observes it. The harms are the Mac's disk filling and the shared VM
breaking (dockerd fails on a full root disk, taking every session down). The
fourth row is also the one place agent writes reach the Mac without going
through the overlay, which the Phase 4 ADR's trust statement should not have
to caveat.

Design: each session gets its own fixed-size filesystem (a sparse ext4 image,
loop-mounted in the VM) holding the overlay upper/work dirs *and* the
harnesses' writable state; the container root is read-only with sized tmpfs
for scratch; the VM disk gets an explicit cap as a backstop. Default session
cap **5 GiB**.

Rejected: `docker run --storage-opt size=` (overlay2 supports it only on XFS
with `pquota`; Colima's VM root is ext4); ext4 project quotas on the VM root
(needs re-mounting `/` with `prjquota` and tuning the VM's filesystem);
tmpfs for the overlay upper (loses persistence across `colima stop`, and
charges agent output to RAM).

### Unknowns / To Verify

1. `mkfs.ext4` and loop devices are available in the Colima VM image
   (expected: Ubuntu with e2fsprogs; `losetup -f` works as root).
2. A loop-mounted ext4 image is accepted as overlayfs upper/work (expected:
   yes, it is a local filesystem; Phase 1 only ruled out network upper dirs).
3. Whether freed blocks return to the Mac. `mount -o loop,discard` punches
   holes in the image file on delete; whether the VM disk image on the Mac then
   shrinks depends on vz's virtio-blk discard support. Measure with `du` on the
   Colima disk image before/after a fill-and-delete. If it does not shrink,
   document it: the cap still holds, space is just not reclaimed until
   `--discard`.
4. Exactly which paths opencode and pi write outside their state dirs
   (`~/.local/state`, `~/.npm`, `~/.cache/opencode`, …). Found by trial under
   `--read-only` (step 4).
5. How to point each harness at a read-only config plus a writable state dir.
   Candidates: opencode `OPENCODE_CONFIG` (config file path) with
   `~/.local/share/opencode` writable; pi per-file `:ro` bind mounts of
   `agent/models.json` and `agent/settings.json` over a writable `agent/`, or
   an agent-dir env override if pi has one. Per-file mounts are safe with the
   current `_write_json`, which truncates in place and keeps the inode; an
   atomic-rename writer would break them, so note that next to `_write_json`.

### Steps

1. **VM disk cap.** Add `--disk` to `colima start` in `ensure_colima_vm`, from
   `OFFLINE_AGENT_DISK` / `--disk` (default 10 GiB: roughly 2–3 GiB VM OS,
   ~1 GiB image, 1–2 GiB of superseded build layers, leaving ~4–5 GiB for
   sessions). Session images are sparse, so typical sessions cost megabytes
   and overcommitting the 5 GiB caps is fine, but a single session that fills
   its whole cap can exhaust the VM. So `ensure_session` reads the VM root's
   free space (one `df` in `probe_vm`) and warns when it is below the session
   cap, naming `docker builder prune` and old sessions as the remedies.
   Measure the real OS + image footprint at the gate (`df /` and
   `docker system df` in a fresh VM) and raise the default if it leaves less
   than one session cap free. Extend
   the running-VM mismatch warning to disk (`colima list --json` reports it).
   Colima can grow but not shrink a disk, so an existing VM only picks up a
   smaller cap via `--rebuild` + VM delete; the rebuild prompt's
   unexported-sessions warning already covers the data-loss side.
2. **Per-session filesystem.** Rework `render_session_setup_script`: create
   `sessions/<sid>/disk.img` with `truncate -s <cap>` and `mkfs.ext4 -q -F`
   only if absent; `mount -o loop,discard` it at `sessions/<sid>/mnt` only if
   not already mounted (`mountpoint -q`), since loop mounts do not survive a
   VM restart and `ensure_session` already runs on every start; create
   `mnt/{upper,work,harness}` inside it. `volume_create_argv` points
   `upperdir`/`workdir` at `mnt/upper`/`mnt/work`. Cap from
   `OFFLINE_AGENT_SESSION_DISK` / `--session-disk` (default 5 GiB), applied
   at session creation only; growing an existing session is `truncate -s` +
   `resize2fs` on the mounted image, documented but not automated. The image
   also caps inodes (ext4 default ≈ one per 16 KiB, ~320k at 5 GiB; only
   new or copied-up files count, not the lower layer).
3. **Legacy sessions.** A session dir with `upper/` but no `disk.img` predates
   this phase, and its volume's mount options are fixed at create time.
   `ensure_session` hard-errors with remediation (export with the manual
   bundle recipe, then remove the volume and session dir) rather than
   migrating; there is one such session today and Phase 4's `--discard` does
   not exist yet.
4. **Read-only container root.** `docker run` gains `--read-only`, sized
   `--tmpfs` mounts for the paths found under Unknown #4 (start with
   `/tmp:size=512m`, `/root/.cache:size=256m`), `--memory 1536m` (tmpfs is
   charged to the container's memory cgroup; the VM has 2 GiB) and
   `--pids-limit 512`. With no egress nothing can `apt`/`pip`/`npm install`
   at runtime, so a writable root buys nothing but a disk bomb. Note that a
   uv venv under `$TMPDIR` lands in the `/tmp` tmpfs; packages must come from
   the image anyway. Sizes are constants, tuned at the gate.
5. **No TTY log.** `--log-driver none` on `docker run`: an interactive shell
   has no use for `docker logs`, and without it a runaway `cat` is persisted
   to the VM disk. (`docker exec` output is not logged.)
6. **Harness state off the Mac.** Config the script writes every run stays on
   the host under `~/.offline-agent/` and is mounted `:ro`; everything the
   harnesses write (opencode's data dir, pi's sessions/auth) moves into
   `mnt/harness/` in the session image, mounted read-write. Mechanism per
   Unknown #5. History becomes per-session and is lost with the session, the
   same as overlay work, which is the right trade for a quarantine. Leave the
   existing `~/.offline-agent/opencode/data` in place, unmounted; say so once.
   Side effect: with no writable `$HOME` mount left in the container, Unknown
   #4 of the main plan (`colima start --mount "$HOME:r"`) no longer needs a
   writable carve-out for harness state.
7. **Tests.** Update `test_session_setup_script_creates_upper_work_and_marker`,
   `test_volume_create_argv_is_overlay_over_project`, and
   `test_run_mounts_harness_state_only` for the new layout. Add static checks:
   setup script is idempotent (mkfs guarded by existence, mount guarded by
   `mountpoint -q`), upper/work/harness under `mnt/`; `docker run` argv has
   `--read-only`, sized `--tmpfs`, `--memory`, `--pids-limit`,
   `--log-driver none`, and every host-path `-v` is `:ro`; `colima start`
   has `--disk`; legacy-session detection; sizing env/flag parsing; the
   low-free-space warning fires below one session cap and not above it.
8. **Smoke test.** `tests/test-offline-agent-limits.sh`, in-container like the
   firewall test, meant for a scratch project started with
   `OFFLINE_AGENT_SESSION_DISK=256M` so a fill is quick: `/` is read-only;
   `dd` into the project dir hits `ENOSPC` near the cap; `/tmp` fill hits its
   tmpfs size; the harness state dir is writable and on the session image;
   no mount in `findmnt` is a writable virtiofs path; the cgroup's
   `pids.max` and `memory.max` match the configured caps (read, not
   fork-bombed). It deletes its fill files on exit.

### Acceptance criteria

- Filling the project dir, `/tmp`, and the harness state dir each fails
  inside the session with `ENOSPC` (or OOM for tmpfs past the memory cap);
  a second session and a fresh `offline-agent.py` start in another directory
  still work; `docker info` in the VM still works.
- On a fresh 10 GiB VM with the image built, VM free space is at least one
  session cap, and the low-free-space warning fires when it is not.
- The Mac's free space drops by at most the session cap during the fill
  (and by the Unknown #3 answer after delete).
- `findmnt` in the container shows no read-write mount backed by the Mac.
- Phase 2's firewall and cross-VM smoke tests still pass.

## Phase 4: Export/apply surface, docs, ADR

Human gate: the user exercises `--diff`, `--export`, `--apply --dry-run`,
`--apply`, and `--discard` on a real session and judges whether `--apply` is
conservative enough to trust.

### Steps

1. Add `--diff`, `--export PATH`, `--apply [--dry-run]`, `--discard`, and
   `--list-sessions`. All operate on the session for the current cwd (or
   `--session ID` for `--discard`/`--export`), and all run against the
   **merged** view inside a short-lived helper container that mounts the
   overlay volume plus, for `--apply`, the host path read-write at a separate
   mount point. Per Unknown #3: git repos → `git -C <cwd> diff` / `git
   format-patch <base>..HEAD` (where `<base>` is the host repo's HEAD recorded
   in the session marker at session start); non-repos → `rsync --delete`
   merged→host with `-n --itemize-changes` for `--dry-run` and `--diff`.
2. `--apply` refuses to run if the host path's git HEAD moved since the
   session started, or (non-repo) if `rsync -n` shows the host has changes the
   overlay did not make; print the conflict and point at `--export`.
3. `--discard` removes the volume, unmounts the session image, and removes
   the session dir (image included); require the session's `--diff` to be
   empty or `--force`. Every helper-container command calls `ensure_session`
   first, so the loop mount exists after a VM restart (Phase 3).
   `--list-sessions` shows each session's used/cap from `df` on its mount.
4. Tests: pure functions for the export plan (git vs non-git selection, base
   HEAD comparison, itemize-changes parsing) in `tests/test_offline_agent.py`.
5. Docs: README section after `research.py` (usage, threat model incl. the
   note that cwd secrets such as `.env` are readable by the agent and can be
   sent to the inference endpoint; adding packages via the user-packages block
   and `--rebuild`; concurrent-VM note; storage caps and their env vars),
   `CLAUDE.md` Layout and a new "offline-agent.py key decisions" block, `templates/global-claude.md`
   environment note if the harness AGENTS.md is seeded (mirror
   `seed_agentsmd`, `start-agent.sh:1017`, or decide it is not needed and say
   so), and the tests table in README.
6. ADR-050 in `ADR.md` (next after ADR-049): no-egress model, overlay write
   quarantine and its trust statement (VM trusted, lower layer enforces
   read-only), rebuild-only package path with the layer-cache argument,
   ephemeral containers, cwd-under-`$HOME` restriction, the Phase 3 storage
   bounds (per-session image, read-only root, no writable Mac mount) with
   their own rejected alternatives, and the rejected
   alternatives (installer container, pause window, `--offline` flag on
   `start-agent.sh`).

## Notes

- **Threat model, stated plainly for the ADR:** the agent can read everything
  in cwd and can send it to the inference server; that is the one channel
  left open by design. It cannot reach the network, other VM services, or the
  Mac filesystem outside the overlay. A prompt-injection payload inside the
  project can at most corrupt the overlay, which the user reviews before
  `--apply`.
- **Why not `--network none`:** it would also cut the inference endpoint.
  The iptables chain with a single RETURN is the established pattern here.
- **Disk growth:** the overlay upper dir grows with agent writes and is freed
  by `--discard`. Image rebuilds reuse cached layers unless a base layer is
  edited; superseded layers linger until `docker builder prune` inside the
  `offline-agent` VM.

### Phase 1 results (2026-10-18, macOS 27.0.1, colima 0.10.3, docker 29.5.2 in VM)

Probe: `plans/offline-agent-phase1-probe.sh` (raw logs in
`plans/offline-agent-phase1-results-{overlay,harness}.txt`, not committed).

**Verdict: GO on the overlay design.** The snapshot-copy fallback is not needed.

VM and overlay (Unknowns #1, #3):

- `colima start -p offline-agent` with research.py's flags: 23s cold start,
  22s `stop`+`start`. Kernel 6.8 has `overlay`. With no `--mount`, Colima
  mounts `$HOME` as virtiofs `rw` at the same path, so the lower layer is
  just the host path, as the Approach assumed.
- The `local`-driver overlay volume with a virtiofs lowerdir works. The
  upper/work dirs **must exist before mounting**: the mount fails otherwise,
  so Phase 2 step 3's `sudo mkdir -p` is required.
- Write, append, delete, `git mv`, `rm -rf` of a directory, a commit, and a
  planted `.git/hooks/post-checkout` all worked in the merged view. The host
  repo stayed byte-identical (manifest of names and SHAs, `.git` included),
  with the same HEAD and a clean `git status`. `git fsck` passes in the
  merged view.
- The upper dir holds deletions as `c 0,0` whiteouts (files and the removed
  directory). A file rename shows up as a full copy under the new name plus a
  whiteout of the old one: no redirect xattrs. The new `.git` objects, the
  refs and the index landed in upper. This confirms Phase 4 must diff the
  merged view, never the upper dir.
- The upper layer persists across container restarts and across
  `colima stop`/`start`. `/var/lib` on the VM disk survives restarts.
- Inside the container the virtiofs files show as owned by `0:0`. git needs
  `safe.directory '*'`; set it in the image's system gitconfig in Phase 2.
- A file added on the host *between* sessions shows up in the merged view,
  because the lower layer is live. Phase 4 implication for non-repo `--apply`:
  a host-side edit to a file the agent also changed is masked by the upper
  copy, so rsync merged→host would silently overwrite it. The conflict check
  needs a baseline manifest recorded at session start, not just `rsync -n`.

Harness startup with `--network none` (Unknown #2), `claude-agent:latest`
image (opencode 1.19.3, pi 1.3.0):

- Both start offline. Both TUIs were still running after 20s under a pty and
  did not crash. The rendered screen was not captured, so TUI startup latency
  is unmeasured; check it at the Phase 2 gate.
- **opencode phones home despite `autoupdate: false`.** Behind a logging
  proxy it tried `registry.npmjs.org:443` and `models.opencode.ai:443`.
  Setting `OPENCODE_DISABLE_AUTOUPDATE=1`, `OPENCODE_DISABLE_MODELS_FETCH=1`,
  `OPENCODE_DISABLE_LSP_DOWNLOAD=1` and `OPENCODE_DISABLE_DEFAULT_PLUGINS=1`
  removed both, leaving only the inference endpoint. The run did not show
  which flag removed which host, so Phase 2 sets all four (Dockerfile `ENV`).
- The `@ai-sdk/openai-compatible` provider is bundled: the request reached
  `POST /v1/chat/completions` with no npm fetch. No pre-install needed.
- `opencode models` lists the bundled `opencode/*` (Zen) cloud models next to
  `local/test-model`. These are unusable offline. Phase 2 should try
  restricting providers to `local` (`enabled_providers`; verify the key
  exists in 1.19).
- `opencode run` against an endpoint that refuses connections retried until
  the 60s timeout. Against one that answers with an HTTP error it exited in
  ~1.6s. So expect a hang, not an error, when the local inference server is
  down.
- The opencode TUI ignores SIGTERM: `timeout` hung the first probe run.
  SIGKILL ends it cleanly with no leftover processes. This doesn't affect
  interactive use.
- pi contacted only the inference endpoint in every mode, with no update or
  telemetry traffic seen. `pi -p` fails with "Connection error." after
  14.3s of retries. **No Pi offline settings needed.**

### Phase 2 implementation notes (2026-10-19)

Built in the dev container; nothing ran against Colima yet, so the gate
checks below are the first real run.

- `enabled_providers: ["<backend>"]` exists in opencode 1.19.3 and was checked
  locally: with it `opencode models` lists only the local model; without it
  the bundled `opencode/*` Zen models appear. The upstream schema now marks
  the key deprecated, so re-check it after an opencode bump.
- Defaults chosen at build time: VM 2 GiB / 2 CPUs. Inference runs on the
  host and shares unified memory with the model, so the VM stays lean. The
  docker context is set per process via `DOCKER_CONTEXT`, never persisted, so
  the sibling VMs' CLI context is left alone.
- The container is named `offline-agent-<session id>`. Re-running the script
  in the same directory while a session is open attaches a second shell with
  `docker exec` instead of starting a second container.
- The overlay volume is mounted with `:nocopy`. Cloud API keys are not
  forwarded (there is no route to use them).
- The firewall jumps cover both the default bridge and `offline-agent-net`.
  A failed firewall apply, or a missing host IP or network CIDR, aborts
  before any container starts. research.py falls back to guessed values here.
- Not done in Phase 2, for Phase 4: the session marker records only `path`.
  Phase 4's `--apply` needs the base HEAD (git) or a baseline manifest
  (non-git) written at session *creation*. Sessions created before that lands
  will have no baseline, so `--discard` them before testing `--apply`.
- Unknown #4 (`--mount "$HOME:r"`) is unchecked. If it is adopted, the
  harness state bind mounts under `~/.offline-agent/` need a writable mount.

### Phase 3 implementation notes (2026-10-19)

Built in the dev container; nothing ran against Colima yet, so the gate
checks are the first real run.

- **Correction to step 1: Colima 0.10.3 has two VM disks.** `--disk`
  (default 100 GiB) is a separate data disk bind-mounted at `/var/lib/docker`,
  `/var/lib/containerd` and friends; `--root-disk` (default 20 GiB) holds the
  OS and `/var/lib/offline-agent`, so the session images
  (`cmd/start.go`, `environment/vm/lima/disk.go`, `environment/container/docker/docker.go`).
  Session images therefore compete with the VM OS, not with the image or
  the build cache. Decision with the user: cap both, `--disk` /
  `OFFLINE_AGENT_DISK` and `--root-disk` / `OFFLINE_AGENT_ROOT_DISK`,
  10 GiB each. The free-space probe is `df -Pk /var/lib` (root disk), and the
  low-space warning names old sessions and a larger `--root-disk` as remedies.
  `docker builder prune` frees the data disk, not the root disk, so it is not
  named. At the gate, measure `df /` (root) and `docker system df` (data) on
  a fresh VM.
- Disk flags go to `colima start` only when the VM is created, never on a
  restart. Colima ignores a smaller `--disk` with a warning
  (`syncDiskSize`); a smaller `--root-disk` against an existing VM is
  unverified, so it is not attempted. `colima list --json` reports only the
  data disk, so the running-VM mismatch warning covers that disk alone.
- Unknown #4, measured by running each harness as `nobody` against
  root-owned dirs: opencode 1.19.3 needs `~/.local/share/opencode`,
  `~/.local/state` (lock files; startup fails without it), `~/.cache` and
  `$TMPDIR`. With its config dir unwritable it skips the
  `@opencode-ai/plugin` npm install it otherwise puts there, and both
  `opencode models` and `opencode run` work. pi 1.3.0 writes `auth.json`,
  `models-store.json` and `sessions/` in its agent dir.
- Unknown #5: opencode gets its config dir `:ro`; `OPENCODE_CONFIG` was not
  needed. pi has one agent dir for config and state (`PI_CODING_AGENT_DIR`
  moves both), so `/root/.pi` comes from the session image with
  `models.json`/`settings.json` bind-mounted `:ro` over it. pi writes settings
  in place with `writeFileSync`, and a failed save goes to `recordError`, so
  `/model` in the TUI does not crash; the choice just isn't persisted. Use
  `OFFLINE_AGENT_DEFAULT_MODEL` instead. Check that at the gate.
- Session image layout: `mnt/{upper,work}` plus `mnt/harness/{opencode,state,pi}`
  mounted at `~/.local/share/opencode`, `~/.local/state` and `~/.pi`.
  `HISTFILE` points at `~/.local/state/bash_history`, because the read-only
  root would otherwise refuse bash's history write. The image is formatted
  as `disk.img.new` and renamed, so a failed mkfs is retried, not mounted.
- tmpfs mounts are explicitly `exec` (Docker's default is `noexec`), because
  uv venvs under `$TMPDIR` run binaries from `/tmp`.
- `~/.npm` is not writable. npm commands warn that they cannot write their
  log but still run. Add a tmpfs at the gate if that turns out to be noisy.
- `list_sessions`, which drives the `--rebuild` VM-delete warning, counts an
  unmounted session image as holding work. After a VM boot no image is
  mounted until its session starts, and checking `upper/` would miss them.
- pi settings.json is now always written (as `{}` if empty). Docker would
  create a directory, on the Mac, for a missing bind-mount source.
- Old harness state (`~/.offline-agent/opencode/data`, and everything in
  `pi/agent/` except the two config files) is left in place. A one-time
  notice lists it and writes `~/.offline-agent/README-state-moved.txt`.
- `tests/test-offline-agent-limits.sh` was run (non-fill checks only) inside
  this repo's `claude-agent` container as a negative control. All five checks
  failed there for the right reason (writable root, writable virtiofs mounts,
  no cgroup caps).
