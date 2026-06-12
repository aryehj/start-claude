# Apple Containers runtime backend for `start-agent.sh`

## Status

- [ ] Phase 1: Spike — prove the `container machine` surface supports the four primitives the design needs
- [ ] Phase 2: Runtime abstraction + Apple machine lifecycle (`--runtime=apple|colima`)
- [ ] Phase 3: In-machine egress allowlist via `--uid-owner` iptables + tinyproxy (security crux)
- [ ] Phase 4: Feature parity (OpenCode, Pi, SearXNG, local inference) + docs/ADR/tests

## Context

`start-agent.sh` (1598 lines) runs Claude Code, OpenCode, and Pi on a single shared
**Colima** VM + a single shared **docker** container, with a VM-level egress allowlist
the in-container agent cannot modify. The allowlist is enforced *outside* the container:
tinyproxy + an iptables `CLAUDE_AGENT` child chain of `DOCKER-USER`
(`start-agent.sh:932-981`) that `REJECT`s all unmatched bridge egress. The agent runs as
**root inside an unprivileged docker container**; the trust boundary is the privilege gap
between that container and the Colima VM that hosts it (ADR-033, ADR-034, ADR-010).

The request is to add **Apple Containers** as an alternative runtime via a
`--runtime=apple|colima` toggle, Colima remaining the default and staying fully intact.
The Apple path must carry the full feature set: OpenCode, Pi, SearXNG websearch, and
host-side local inference (Ollama/omlx).

The hard part is the firewall. Research into Apple's `container` 1.0 release established:

- A **container machine** is a *single persistent Linux environment* (WSL-style) running
  its own init/systemd — **not** a host that runs multiple regular containers inside it.
- Normal `container run` workloads each still get their **own** per-container VM on vmnet
  (`192.168.64.0/24`, host gateway `.1`). There is **no** shared host-controllable bridge
  the way Colima provides, and Apple documents **no** host-enforced per-container egress
  filter at the vmnet layer.

So the Colima topology ("firewall in the VM, outside the container") does not port
directly. The chosen design recreates the privilege gap *inside one container machine*:
run the entire stack in a single machine, and enforce egress with iptables `OUTPUT` rules
using the `owner --uid-owner` match — root installs the rules; the agent runs as a
**non-root user without sudo**, so it can reach the network only through the local
allowlist proxy and cannot rewrite the rules. (Decisions confirmed with the user:
runtime toggle; full feature parity; one-machine + uid-firewall; non-root agent is
acceptable.)

`start-claude.sh` already drives Apple Containers (image build via `container build`,
`container_exists()` version-robust inspect, `container run -v host:container`) and is the
reference for the Apple CLI idioms — but it ships **no** egress firewall, so it offers no
prior art for the firewall half of this work.

## Goals

- `start-agent.sh --runtime=apple` brings up the full agent stack on a single Apple
  container machine; `--runtime=colima` (default) behaves exactly as today.
- Egress on the Apple path is restricted to the sandbox's `allowlist.txt`, enforced so a
  non-root agent inside the machine cannot bypass it.
- Per-mode lifecycle (`--rebuild`, `--reset-container`, `--reload-allowlist`,
  reattach-if-exists) works on both runtimes.
- Only the active sandbox is visible inside the machine (ADR-034 one-directory boundary
  preserved — the machine's default whole-`$HOME` mount must be suppressed).
- OpenCode, Pi, SearXNG, and Ollama/omlx inference all function on the Apple path.
- A new ADR records the runtime-abstraction decision and the uid-firewall design; CLAUDE.md
  and README document the toggle.

## Approach

Treat the runtime as a thin abstraction layer, not a fork. Today the script hard-codes
Colima/docker verbs throughout: `colima start` (`672`), `vm_ssh`/`vm_sh`/`vm_put_file`
(`693-716`), `docker run` (`1580`), `docker exec` (`1540`), `docker rm` (`717`). The Apple
path needs the same *operations* — bring up a host VM, run a privileged setup step in it,
mount the sandbox, start the workload, exec an interactive shell, tear down — against a
different CLI. Introduce a small set of runtime-dispatched helpers (e.g. `rt_up`,
`rt_exec_priv`, `rt_attach`, `rt_down`, `rt_machine_exists`) selected by `$RUNTIME`, and
route the existing orchestration through them rather than duplicating 1500 lines. The
Colima branch wraps the current code paths verbatim; the Apple branch is new.

The single biggest risk is that the `container machine` CLI does not actually expose the
primitives the design assumes (arbitrary non-home mounts, non-root exec, in-machine
iptables, host-gateway reachability) — Apple's published docs are silent on all four. That
is why Phase 1 is a throwaway spike whose only job is to convert those assumptions into
verified facts (or kill the design) before any script changes land. The firewall itself
(Phase 3) is gated separately because "a non-root agent provably cannot reach a blocked
host" is the one correctness property the whole feature exists to provide, and it benefits
from explicit manual verification.

This is a >30% change to the firewall/runtime core of `start-agent.sh` — exactly the
threshold ADR-018 names as the point to reconsider a Python port. The implementer should
make a deliberate call early (see Notes) rather than discover it mid-build.

## Unknowns / To Verify

These gate the design and are **not** answerable from Apple's published docs (checked:
`apple/container` `docs/container-machine.md`, `technical-overview.md`, `how-to.md` — all
silent on machine mounts, non-root exec, and machine networking). Phase 1 resolves them
empirically against the installed `container` 1.0 release; do not write script code that
assumes any of these until verified.

1. **Arbitrary host mounts into a machine.** Does `container machine create`/`run` accept
   a non-home bind mount (some `--volume`/`--mount`/`--bind`), and can the default
   whole-`$HOME` mount be set to `none`? Required for: mounting only `$SANDBOX_ROOT` and
   preserving ADR-034. Gates Phase 2.
2. **Non-root exec / shell.** Can a command or interactive shell run inside a machine as a
   chosen non-root uid (a `--user` flag, or a `su`/`runuser` step inside)? The uid-firewall
   is meaningless without this. Gates Phases 2 and 3.
3. **In-machine iptables + `owner` match.** Can root inside the machine create `OUTPUT`
   rules with `-m owner --uid-owner`, and do they actually constrain another uid's egress?
   (Needs the `xt_owner` kernel module / `iptables` present in the machine's Linux kernel.)
   Gates Phase 3 — this is the load-bearing one.
4. **Machine networking + host gateway.** What IP/CIDR does a machine get, can it reach the
   internet, and what is the host gateway address for reaching host-side Ollama/omlx
   (the Colima analog is the vmnet default route, `start-agent.sh:804`)? Gates Phases 3-4.
5. **Machine lifecycle verbs.** Confirm exact `container machine` subcommands/flags:
   `create`, `run -n`, `ls`, `inspect`, `stop`, `rm`, `set -n … cpus=/memory=`,
   `set-default`, and whether `create` takes an OCI image tag built locally via
   `container build`. (Partially seen in research; verify against the installed CLI.)
   Gates Phase 2.
6. **SearXNG co-location.** SearXNG ships as an OCI image and today runs as a *separate*
   docker container (`start-agent.sh:1045`). In a single-machine model it must run *inside*
   the machine. Verify the chosen mechanism works there: a nested rootless container
   (podman installed in the machine) vs. running SearXNG from source as a systemd service.
   Gates Phase 4; lean toward nested-podman for image reuse (see Phase 4).

## Phase 1: Spike — prove the machine surface

Throwaway exploration (no committed script changes beyond optional scratch notes). Resolve
Unknowns 1-5 against the actually-installed `container` 1.0 CLI. Build the smallest possible
OCI image (Debian + `iptables` + a non-root user) via `container build`, `container machine
create` it, and probe each primitive by hand.

### Steps

1. Confirm machine lifecycle verbs and flags (Unknown 5): create a machine from a
   locally-built image, `ls`/`inspect`/`stop`/`rm` it, and confirm `set cpus=/memory=`.
2. Test mounts (Unknown 1): attempt to mount a scratch host dir at its host path and to
   suppress the default home mount (`--home none` or equivalent). Record the exact flag
   syntax that works.
3. Test non-root exec (Unknown 2): create a non-root user in the image; get an interactive
   shell and run a command as that uid inside the machine. Record the mechanism.
4. Test the uid-firewall (Unknowns 3): as root in the machine, install
   `iptables -A OUTPUT -m owner --uid-owner <agent-uid> ! -d <proxy> -j REJECT` (or the
   allowlist-proxy-only shape), then confirm from the agent uid that direct egress is
   blocked while proxy egress succeeds, and that the agent uid cannot flush the rule.
5. Test host reachability (Unknown 4): from inside the machine, determine the host gateway
   IP and curl a host-bound port (stand up a trivial listener on the Mac) to confirm the
   inference-routing carve-out is achievable; record the CIDR/gateway.
6. Write findings into the Unknowns section of this plan (or a short `spike-notes` block) as
   resolved facts with the exact commands that worked. Go/no-go on the one-machine +
   uid-firewall design.

### Acceptance criteria

- Each of Unknowns 1-5 is marked resolved with a concrete, reproduced command — or the
  design is revised/escalated to the user if a primitive is unavailable (e.g. no non-home
  mount → revisit ADR-034 strategy; no `xt_owner` → fall back to host-pf or env-proxy from
  the earlier options).

## Phase 2: Runtime abstraction + Apple machine lifecycle

Introduce `--runtime` and the Apple bring-up path, reaching a working **non-firewalled**
interactive shell in a machine with only the sandbox mounted. Firewall comes in Phase 3.

### Steps

1. Add `--runtime=apple|colima` parsing + `CLAUDE_AGENT_RUNTIME` env (mirror the existing
   `--backend` plumbing at `start-agent.sh:150,277`), defaulting to `colima`. Validate the
   value early like `BACKEND` (`278-291`).
2. Add an Apple pre-flight: require the `container` CLI (mirror `start-claude.sh:57`) and
   reuse `container system start`. Keep the Colima `colima`/`docker` checks
   (`start-agent.sh:330-336`) behind the colima branch only.
3. Introduce runtime-dispatched helpers selected on `$RUNTIME`, wrapping the existing Colima
   verbs unchanged and adding Apple equivalents: VM/machine up+down (`start_colima_vm`/
   `destroy_colima_vm` → `container machine create/stop/rm`), privileged in-VM exec
   (`vm_ssh`/`vm_sh` → `container machine run` as root), file injection (`vm_put_file`),
   workload start (`docker run` → enter the machine), interactive attach (`docker exec`
   `1540` → `container machine run`/shell as the agent uid), and existence check (reuse
   `start-claude.sh:73` `container_exists`-style logic for machines).
4. Build the Apple machine image. Start from `dockerfiles/claude-agent.Dockerfile` (already
   omits sandbox deps per ADR-033) and add: a dedicated **non-root agent user** owning its
   home/state, plus `iptables` + the proxy package (Phase 3 needs them present). Build via
   `container build` following `start-claude.sh:293-308`. Decide image-build ownership:
   reuse the Dockerfile for both runtimes, or a separate `claude-agent-machine.Dockerfile`
   if the non-root/firewall layers diverge enough.
5. Mount only `$SANDBOX_ROOT` into the machine (verified flag from Phase 1) and suppress the
   default home mount — do **not** collapse to a whole-`$HOME` mount; ADR-034's
   one-directory boundary must hold. Map the bind-mounted state dirs the Colima path uses
   (`start-agent.sh:1580-1595`: projects, `/root/.claude`, `.claude.json`, opencode
   config/data, `.pi`, agents skills, allowlist `:ro`) to the **non-root user's** home
   inside the machine.
6. Wire `--rebuild` / `--reset-container` / reattach-if-exists through the runtime helpers so
   both runtimes honor them. Apple `--rebuild` removes machine + image; reattach enters the
   existing machine as the agent uid.
7. Run the existing host-side seeding (settings.json, opencode.json, models.json,
   `sync_skills`, `seed_agent_skills`, allowlist seed) regardless of runtime — these write to
   `$SANDBOX_ROOT/.sandbox_config/` on the host and are runtime-agnostic. Confirm the Apple
   mount layout lands them where the in-machine non-root user reads them.

### Acceptance criteria

- `--runtime=colima` is byte-for-byte behaviorally unchanged (no Colima/docker regression).
- `--runtime=apple` drops the user into an interactive shell as the non-root agent user,
  with the sandbox visible and the rest of the host filesystem not. (Egress is still open at
  this phase — firewall is Phase 3.)

## Phase 3: In-machine egress allowlist (security crux)

Recreate the allowlist enforcement inside the single machine using `--uid-owner`. This is
the property the whole feature exists for, so it is gated and manually verified.

### Steps

1. Run tinyproxy inside the machine, reading the same generated filter file the Colima path
   builds (`generate_filter_file`, `start-agent.sh:648`). The filter generation is
   runtime-agnostic; only *where* tinyproxy runs changes (in-machine systemd service vs. the
   Colima in-VM process).
2. Install the uid-owner OUTPUT firewall as root in the machine: the agent uid may egress
   only to the local tinyproxy port (and DNS as needed); all other direct egress `REJECT`ed.
   This replaces the `CLAUDE_AGENT` chain semantics (`start-agent.sh:970-981`) with an
   `OUTPUT`/`owner`-based equivalent — same allow-set shape (established/related, proxy port,
   inference host:port, DNS), different match mechanism. Root and system services are
   unconstrained; only the agent uid is forced through the proxy.
3. Point the agent's environment at the in-machine proxy: set `HTTP(S)_PROXY`/`NO_PROXY`/
   `NODE_USE_ENV_PROXY` for the agent user (mirror `start-agent.sh` `DOCKER_ENV_ARGS`).
   Belt-and-suspenders: env routes well-behaved clients; the uid-firewall is the actual
   enforcement so a client that ignores the env still cannot escape.
4. Mount `allowlist.txt` read-only and wire `--reload-allowlist` for the Apple path:
   regenerate the filter file on the host and reload in-machine tinyproxy without recreating
   the machine (the Colima analog is the fast reload path, `start-agent.sh:72-74`).

### Acceptance criteria

- From the agent uid inside the machine: a host **on** the allowlist is reachable; a host
  **off** it is refused; and the agent uid **cannot** flush/modify the iptables rules or
  reach a blocked host by bypassing the proxy (e.g. direct IP, alternate port). Verify all
  three by hand — this is the trust-boundary check, equivalent to
  `tests/test-agent-firewall.sh` for the Colima path.

## Phase 4: Feature parity, docs, ADR, tests

Wire the remaining capabilities under the single-machine model and document.

### Steps

1. **Local inference.** Add the host-gateway carve-out to the uid-firewall (allow agent uid →
   `host:INFERENCE_PORT`) using the gateway IP found in Phase 1, and set `OLLAMA_HOST`/
   `OMLX_HOST` for the agent user (mirror `start-agent.sh` `DOCKER_ENV_ARGS` + the inference
   probe at `997-1017`, retargeted to run inside the machine).
2. **SearXNG.** Run SearXNG inside the machine. Recommended: a nested **rootless podman**
   container of `docker.io/searxng/searxng` (reuses the upstream image, mirrors the Colima
   `docker run`, `start-agent.sh:1045`), with its outgoing requests routed through the
   in-machine tinyproxy (the `outgoing.proxies` mechanism, ADR-014) and an allow rule for
   agent uid → SearXNG port. Confirmed feasible in Phase 1 (Unknown 6); if nested podman is
   unavailable in the machine, fall back to running SearXNG from source as a systemd service.
   Honor `--disable-search`.
3. **OpenCode / Pi.** These are config-injection + binaries already in the image; verify the
   injected `opencode.json` / `models.json` (host-seeded in Phase 2) resolve correctly for
   the non-root user and that both CLIs run as the agent uid behind the firewall.
4. **Docs.** Update CLAUDE.md's `start-agent.sh key decisions` with the runtime toggle and
   the one-machine + uid-firewall model; update README usage and the `--runtime` flag; note
   the Apple path's non-root-agent and single-machine differences from the Colima path.
5. **ADR.** Add the next-numbered ADR (highest today is ADR-042) recording: the runtime
   abstraction, why Container Machines don't reproduce the Colima topology, and the
   `--uid-owner` firewall + non-root agent as the chosen privilege gap. Cross-reference
   ADR-010/033/034.
6. **Tests.** Extend the static suite (`tests/test_agent_sh.py`) for the `--runtime` plumbing
   and the non-root/uid-firewall invariants; add an Apple-path firewall smoke test analogous
   to `tests/test-agent-firewall.sh` (host-driven where the in-machine assertions can't run
   in CI). Gate runtime-specific tests so the Colima suite still runs without the `container`
   CLI.

## Notes

- **Language (ADR-018).** This reworks the firewall/runtime core — past the ">30% / rework of
  the firewall-allowlist core" threshold ADR-018 names as the trigger to reconsider porting
  `start-agent.sh` to Python. Recommendation: still implement in bash to keep one runtime
  path identical to today and contain blast radius, but make the call consciously and note it
  in the new ADR. Do not introduce piecemeal Python helpers called from bash (ADR-018
  explicitly warns against the worst-of-both-languages outcome).
- **macOS version sensitivity.** vmnet container-to-container communication and multi-network
  support vary by macOS version (15 vs. later). If SearXNG ends up in a separate VM rather
  than nested in the machine, this matters; the nested-podman approach sidesteps it.
- **Weaker-than-Colima honesty.** Even done well, the uid-firewall lives *inside* the same VM
  as the agent, whereas Colima's lives in a VM the agent has no presence in. The gap is
  smaller (a local privilege boundary vs. a VM boundary). The ADR should state this plainly
  so the two runtimes' threat models aren't conflated.
- **Single-machine resource pressure.** Everything (Claude Code, OpenCode, Pi, SearXNG,
  proxy) shares one machine's CPU/memory. Carry the existing `CLAUDE_AGENT_MEMORY`/`_CPUS`
  knobs through to `container machine set cpus=/memory=`.
