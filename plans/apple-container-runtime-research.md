# Apple Containers runtime backend for `research.py`

## Status

- [ ] Phase 1: Apple-primitives spike — confirm the shared spike covers research's needs + run two research-specific probes (host port-publish, two nested containers) `(Opus recommended)`
- [ ] Phase 2: Runtime abstraction — factor the Colima-coupled half behind an interface; prove the Colima path is unchanged
- [ ] Phase 3: Apple runtime end-to-end — single-machine bring-up with nested podman, Squid denylist, host-published Vane UI (human-verified crux) `(Opus recommended)`
- [ ] Phase 4: Parity (fast-reload, `--rebuild`), docs, ADR, tests — finalization, gated only by Phase 3 verification

## Context

`research.py` (1182 lines, stdlib-only Python — ADR-018) brings up an isolated
Vane + SearXNG research environment on a dedicated **Colima** VM (`research`
profile) running docker. Two containers — `research-vane` and `research-searxng`
— share a docker network (`research-net`); their web egress is forced through a
**Squid forward proxy** that applies a **default-allow denylist**, backstopped by
an `iptables RESEARCH` child chain of `DOCKER-USER` (`render_iptables_apply_script`,
research.py:492-555). Vane's UI is published to the macOS host at
`localhost:3000`; host-side Ollama/omlx inference is reached via
`host.docker.internal` and explicitly **bypasses** the denylist (a dedicated
iptables `RETURN` rule, research.py:549, plus `NO_PROXY`, research.py:1007).

The request: add **Apple Containers** as an alternative container runtime so the
same environment can run without Colima, at feature parity. The user's framing —
"add multi-backend support to the existing script vs. write a new script" — is an
explicit evaluation; this plan commits to a recommendation (see Approach).

Naming note: research.py already has `--backend=ollama|omlx` for the *inference*
backend. To avoid collision, the runtime selector is **`--runtime=colima|apple`**
(env `RESEARCH_RUNTIME`), mirroring the `--runtime` toggle chosen for the sibling
port in `plans/apple-container-runtime-start-agent.md`.

### What the Apple model forces (established, not re-derived here)

The sibling plan already established, from Apple `container` 1.0 research, the
facts that shape this port — do not re-discover them:

- Regular `container run` workloads each get their **own** per-container micro-VM
  on vmnet with **no shared host-controllable bridge** and **no host-enforced
  per-container egress filter**. research.py's "two containers on a shared
  network, firewall outside them" topology therefore does **not** port to plain
  `container run`.
- The viable analog is the **single `container machine`** model (one persistent
  WSL-style Linux env with its own init/systemd): run Squid, the iptables
  denylist, **and both Vane and SearXNG** *inside* one machine, recreating the
  Colima topology within it. `start-claude.sh` is the reference for Apple CLI
  idioms (`container_exists` inspect-robustness at start-claude.sh:76-80, image
  build at start-claude.sh:241-343, `container system start`) but ships no
  firewall, so offers no prior art for the egress half.

### Why this port is materially easier than the start-agent port

The sibling plan's hardest phase is a hardened **uid-firewall** because
start-agent.sh enforces a **default-deny allowlist** against an adversarial root
agent. research.py is the opposite: a **default-allow denylist** guarding a
**cooperating** research UI. A uid-firewall buys almost nothing here — under
default-allow, even a compromised Vane could exfiltrate to any host *not* on the
denylist regardless of how the rules are owned. Per the decision recorded below,
the Apple firewall matches today's intent (Squid + iptables, all-as-root,
`HTTP(S)_PROXY` env as the primary path, iptables as defense-in-depth). The
denylist stays a guardrail, not a containment boundary. This removes the single
biggest source of difficulty the sibling port carries.

## Goals

- `research.py --runtime=apple` brings up the full environment (Vane at
  `localhost:3000`, SearXNG wired internally, denylist-filtered web egress,
  host inference reachable) on a single Apple container machine.
- `--runtime=colima` (default) is behaviorally **unchanged** — no regression.
- The runtime-agnostic core (denylist compose/fetch/render, Squid ACL, SearXNG
  settings, iptables-script render, Vane config patch) is **shared, not
  duplicated**, and its ~45 existing unit tests stay green.
- Per-mode lifecycle works on both runtimes: `--rebuild`, `--reload-denylist`,
  `--refresh-denylist`, `--reseed-denylist`, reattach-if-exists.
- A new ADR records the runtime abstraction, the single-machine + nested-podman
  topology, and the "denylist stays a guardrail (no uid-firewall)" reasoning.
  CLAUDE.md and README document `--runtime`.

## Approach

**Recommendation: extend `research.py` with a runtime abstraction. Do not write a
separate script.** The deciding fact is that roughly half of research.py is
runtime-agnostic pure logic — the denylist compose/fetch/prune pipeline, Squid
ACL generation (`denylist_to_squid_acl`, `_prune_subdomains`), `render_squid_conf`,
`render_searxng_settings`, `patch_vane_searxng_url`, and the parameterized
`render_iptables_apply_script` — all of it carrying ~45 unit tests
(`tests/test_research.py`). A second script would either duplicate this core (two
denylist implementations and two template sets drifting apart — the worst
outcome, given the denylist compose algebra is the crown jewel) or import it (at
which point it is not a separate script, just a second entrypoint). Either way the
"separate script" framing dissolves. Python also makes the abstraction idiomatic —
a `Runtime` ABC with `ColimaRuntime`/`AppleRuntime` — unlike the bash sibling,
which had to hand-roll dispatched helpers; the cost that made a runtime toggle
awkward there does not apply here. Finally, the nested-podman `run` arg lists are
near-identical to today's `docker run` arg lists (podman is CLI-compatible), so
`AppleRuntime` reuses most container-bring-up logic rather than reinventing it.

Keep it **one file** initially: `ColimaRuntime` wraps the existing functions in
place, `AppleRuntime` is added alongside, and the pure helpers are untouched
(tests keep importing from `research`). Extract a `research_core.py` module *only*
if the file crosses ~1500 lines — a mechanical follow-up, not a precondition.

Structurally, the work is gated on Apple primitives this repo has not yet
verified. The sibling plan's Phase 1 spike resolves most of them; the decision was
to **reference that shared spike rather than re-run it**, adding only the two
probes research needs that start-agent does not (host-port publishing, and two
nested containers on a shared network). Everything downstream of the spike is a
clean refactor (Phase 2) plus a new runtime implementation (Phase 3) whose
correctness is a human-verified gate, then finalization (Phase 4).

## Unknowns / To Verify

These gate the design and come from the **shared** Apple-primitives spike
(`plans/apple-container-runtime-start-agent.md`, Phase 1, currently unstarted).
Do not write `AppleRuntime` code that assumes any of these until the spike
resolves them against the installed `container` 1.0 CLI.

Covered by the shared spike (reuse its findings):

1. **`container machine` exists & lifecycle verbs** — `create`/`run`/`ls`/
   `inspect`/`stop`/`rm`/`set cpus=/memory=`, and whether `create` takes a
   locally-built OCI image. (Sibling Unknown 5; "partially seen, verify".)
2. **Arbitrary host mounts + suppress default `$HOME` mount** — needed to mount
   only `~/.research/` state into the machine. (Sibling Unknown 1.)
3. **In-machine iptables** — root in the machine can create filtering rules that
   constrain nested-container egress. (Sibling Unknown 3; here we need filter
   rules, *not* the `owner --uid-owner` match — see Phase 3.)
4. **Host-gateway reachability** — the machine's vmnet IP/CIDR and the
   host-gateway address for reaching host-side Ollama/omlx. (Sibling Unknown 4;
   feeds `discover_network`'s Apple branch and the inference `RETURN` rule.)
5. **Nested rootless podman inside a machine** — can `docker.io/searxng/searxng`
   and `docker.io/itzcrazykns1337/vane:slim-latest` run as nested podman
   containers in the machine? (Sibling Unknown 6; lean podman for image reuse.)

Research-specific — **add these two probes to the shared spike** (start-agent did
not need them: its agent stack is an interactive shell, not a host-published web
UI, and it nests only one container):

6. **Host port publishing through the machine.** Vane must be reachable at
   `localhost:3000` on the macOS host. Verify the two-hop path works: nested
   podman publishes a port to the machine, and `container machine` forwards a
   machine port to the host. Load-bearing — if this can't be done, the Vane UI is
   unreachable and the port forms part of the bring-up. Verify by standing up a
   trivial nested listener and curling `localhost:3000` from the Mac.
7. **Two nested containers on a shared network reach each other.** Vane →
   `research-searxng:8080` (`patch_vane_searxng_url`, research.py:558-575) must
   resolve over a nested podman network. Verify a podman network gives both
   containers name-based connectivity inside the machine.

Also flag (from sibling Notes): **macOS version sensitivity** of vmnet behavior.
The nested-podman approach keeps both containers in one machine and sidesteps
cross-VM networking, so this should not bite — confirm during Phase 3.

## Phase 1: Apple-primitives spike (shared + two research probes)

**Gate:** go/no-go on the single-machine + nested-podman design for research's
two-container, host-published-port shape.

### Steps

1. Run (or consume, if already run for the sibling port) the shared Phase 1 spike
   in `plans/apple-container-runtime-start-agent.md`, resolving Unknowns 1-5
   above. Do not duplicate the probe — reference its recorded findings.
2. Add probe 6 (host port publish): build a throwaway image with a nested podman
   listener, publish its port to the machine and the machine port to the host,
   and confirm `curl localhost:3000` succeeds from macOS. Record the exact flags.
3. Add probe 7 (two nested containers on a shared net): create a podman network in
   the machine, run two containers, confirm name-based connectivity
   (`curl http://other-container:PORT`).
4. Record findings (resolved facts + exact working commands) in this plan's
   Unknowns section or a short `spike-notes` block. Go/no-go: if host-port publish
   (probe 6) is impossible, escalate to the user before any `AppleRuntime` work —
   it has no graceful fallback.

## Phase 2: Runtime abstraction (Colima unchanged)

**Gate:** the Colima-coupled half is cleanly behind an interface and
`--runtime=colima` is byte-for-byte behaviorally unchanged — establishes the
refactor's blast radius before the Apple impl depends on the interface shape.

### Steps

1. Add `--runtime=colima|apple` parsing + `RESEARCH_RUNTIME` env to `parse_args`
   (research.py:127-205), defaulting to `colima`; validate early. Add `runtime` to
   `VmConfig` (research.py:103-123).
2. Define a `Runtime` ABC capturing the runtime-coupled operations, each presently
   a free function or subprocess wrapper:
   - host/VM lifecycle (`ensure_colima_vm` research.py:709; `rebuild_teardown`
     research.py:1055; `colima_profile_running` research.py:643)
   - privileged in-host shell + file injection (`vm_sh`/`vm_ssh`/`vm_put_file`,
     research.py:601-640) — used by `install_squid`, `apply_firewall`,
     `reload_denylist_fast_path`, `probe_inference`
   - network discovery (`discover_network` research.py:756; `ensure_docker_context`
     research.py:799; `ensure_docker_network` research.py:812)
   - workload run/start/recreate + existence (`ensure_searxng_container`,
     `ensure_vane_container`, `start_or_recreate`, `docker_container_*`,
     research.py:658-1017)
3. Implement `ColimaRuntime` as a thin wrapper that calls the **existing functions
   verbatim** — no behavior change. Route `main` (research.py:1101-1178) through
   the selected runtime instance.
4. Leave every runtime-agnostic helper untouched: all denylist functions
   (research.py:219-431), `render_squid_conf`, `render_searxng_settings`,
   `patch_vane_searxng_url`, and `render_iptables_apply_script`. Tests in
   `tests/test_research.py` continue importing them from `research` unchanged.

### Acceptance criteria

- `--runtime=colima` (and the bare default) reproduce today's behavior exactly,
  including all fast paths (`--reload-denylist`, `--refresh-denylist`,
  `--reseed-denylist`) and `--rebuild`. The existing unit suite passes unmodified.

## Phase 3: Apple runtime end-to-end (human-verified crux)

**Gate:** human verification that the Apple path actually delivers the
environment — this is the property the feature exists for, and the bring-up has
manual surface (browser UI, live denylist block) that automated tests won't reach.

### Steps

1. **Image.** Build a `claude-dev`-style machine image via `container build`
   (pattern: start-claude.sh:241-343) carrying Squid, iptables, and rootless
   podman. Decide ownership: a dedicated `research-machine` image, or extend an
   existing Dockerfile — keep it separate from `claude-dev:latest` since the
   package set differs.
2. **Machine lifecycle (`AppleRuntime`).** Implement create/start/inspect/stop/rm
   and `--rebuild` teardown against `container machine` (verbs from Phase 1),
   carrying `--memory`/`--cpus` through to the machine's `set cpus=/memory=`.
   Reuse `container_exists`-style inspect-robustness from start-claude.sh:76-80.
   Mount only `~/.research/` (Paths.base, research.py:64) into the machine and
   suppress the default home mount (flag from Phase 1).
3. **In-machine network discovery.** Implement the Apple branch of network
   discovery: the nested-podman network CIDR and the host-gateway IP for inference
   (Phase 1 Unknown 4), populating the same `VmConfig` fields the Colima branch
   sets (`bridge_ip`/`bridge_cidr`/`host_ip`/`research_net_cidr`,
   research.py:111-114) so the shared render functions consume them unchanged.
4. **Squid + denylist inside the machine.** Install Squid in the machine image (or
   on first boot, mirroring `install_squid` research.py:834) and push the
   **unchanged** `render_squid_conf` / `denylist_to_squid_acl` outputs via the
   runtime's file-injection method.
5. **iptables denylist (simpler firewall, per decision).** Apply the existing
   `render_iptables_apply_script` output inside the machine, all-as-root. The one
   coupling to break: that function hardcodes the `DOCKER-USER` chain and
   docker-bridge assumptions (research.py:530-544). Parameterize the hook chain so
   the Apple branch targets podman's egress chain (e.g. netavark/CNI `FORWARD`
   hook) instead of `DOCKER-USER`; keep all IP/CIDR/port interpolation as-is. Do
   **not** add a uid-owner match — Vane runs as the machine's default user and the
   `HTTP(S)_PROXY` env (research.py:1005-1007) is the primary path; iptables is
   defense-in-depth. Preserve the inference `RETURN` carve-out (research.py:549)
   and the SearXNG `:8080` intra-net allow (research.py:553).
6. **Nested containers.** Run SearXNG and Vane as nested rootless podman
   containers on a podman network inside the machine. The `docker run` arg lists
   (research.py:932-944, 991-1013) port to `podman run` near-verbatim: image,
   `--network`, `-v`, `-e HTTP_PROXY/HTTPS_PROXY/NO_PROXY`, `--add-host
   host.docker.internal:<host_ip>`. Keep `ensure_vane_searxng_url`
   (research.py:950) pointing at `research-searxng:8080` — name-based connectivity
   confirmed by Phase 1 probe 7.
7. **Publish Vane to the host.** Wire the two-hop port publish from Phase 1 probe 6
   so `localhost:3000` (or `--port`) reaches Vane on the Mac.
8. **Inference probe.** Implement the Apple branch of `probe_inference`
   (research.py:1020) — same reachability check, run inside the machine against
   the host-gateway IP and `inference_port`.

### Acceptance criteria

- From the macOS host: Vane loads at `localhost:3000`; a search round-trips
  through SearXNG; a known-denylisted domain is blocked while an allowed domain
  loads; host Ollama/omlx is reachable from Vane and its traffic bypasses Squid.
  Verify by hand — this is the trust-and-function gate, the Apple analog of
  `tests/probe-denylist.sh`.

## Phase 4: Parity, docs, ADR, tests (finalization)

No new uncertainty — gated only by Phase 3 passing. Brings the Apple path to full
lifecycle parity and documents it.

### Steps

1. **Fast-reload parity.** Implement the Apple branch of `reload_denylist_fast_path`
   (research.py:1077): regenerate the ACL on the host, inject it into the machine,
   `squid -k reconfigure` — no machine recreate. Wire `--reload-denylist` /
   `--refresh-denylist` / `--reseed-denylist` through the runtime so all work on
   `--runtime=apple`.
2. **Docs.** Update README's `research.py` section (README.md:538+) with
   `--runtime` and the Apple prerequisites; update CLAUDE.md's `research.py key
   decisions` with the runtime abstraction, the single-machine + nested-podman
   topology, and the denylist-stays-a-guardrail (no uid-firewall) rationale.
3. **ADR.** Add the next-numbered ADR (highest today is **ADR-044**; coordinate if
   the sibling Apple port lands concurrently to avoid a number collision)
   recording: the `--runtime` abstraction; why Container Machines don't reproduce
   the Colima topology so the stack collapses into one machine with nested podman;
   and why the denylist's default-allow model makes uid-hardening pointless here.
   Cross-reference ADR-018 (Python probe), ADR-021/023 (denylist design),
   ADR-033/034 (sibling sandbox/trust model), and the sibling Apple ADR.
4. **Tests.** Extend `tests/test_research.py` for `--runtime` parsing/selection and
   the `render_iptables_apply_script` chain-parameterization (assert the Colima
   branch still emits `DOCKER-USER` and the Apple branch emits the podman hook);
   add static checks that `AppleRuntime` generates correct `podman run` arg lists.
   Add a host-driven Apple smoke test analogous to `tests/probe-denylist.sh`,
   gated behind `container` CLI presence so the Colima suite runs without it.

## Notes

- **Spike ownership.** Q1 decision was to reference the shared spike, not own it.
  If the sibling port hasn't run Phase 1 by the time this work starts, running it
  here unblocks both — but record findings in the sibling plan too, so the shared
  facts have one home.
- **No language question.** Unlike the sibling port (ADR-018's >30% bash-rework
  trigger), research.py is already Python and the abstraction is idiomatic — there
  is no port-language decision to make here.
- **Resource pressure.** Everything (Squid, iptables, Vane, SearXNG, podman)
  shares one machine. Carry `RESEARCH_MEMORY`/`RESEARCH_CPUS` (research.py:55-56,
  185-196) through to `container machine set`; the default 2 GiB / 2 CPUs may need
  raising for the nested-podman + two-image footprint — tune during Phase 3.
- **Worst-case on code structure.** If Apple's model somehow forced a topology
  sharing nothing with today's, the right move is still extract-shared-module +
  thin per-runtime entrypoints, never copy-the-denylist-core. A full fork is not
  on the table.
