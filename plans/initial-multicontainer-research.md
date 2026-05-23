# Egress Firewall for start-claude.sh — Research Notes

> **Status:** Research / design notes, not an implementation plan.
> Captures the architectural exploration before committing to a direction.
> Pick this up by re-reading, picking the open questions below, and then
> spawning a real `/scope` with the chosen design.

## Why this exists

`start-claude.sh` (Apple Containers; per-project microVMs) currently has
**no egress firewall**. `start-agent.sh` (Colima; one shared VM + one
docker container) does, via tinyproxy + iptables in the Colima VM (see
ADR-010). The goal is to give `start-claude.sh` an equivalent guardrail,
under the constraint **no modifying host iptables / no host pf rules**.

Because Apple Containers gives each project its own Linux microVM with no
separate "host VM" layer, the trick that makes `start-agent.sh`'s design
work — putting the firewall one kernel above the workload — does not
translate directly. Most of the design space is about how to recover
that "agent can't touch the filter rules" property without a free upper
layer.

## Threat model (working assumption — confirm at pick-up)

Defense against:

- AI agent typo'ing a URL, fetching from the wrong domain, exfiltrating
  to an unintended host.
- AI agent following an attacker-controlled instruction in tool output to
  fetch from a malicious endpoint.

Not in scope:

- Adversarial root code with kernel-exploit capability.
- macOS host-level isolation (covered separately by
  `plans/model-host-firewall.md` for Ollama specifically).

## Three options on the table

### Option 1 — In-microVM soft enforcement

tinyproxy + iptables installed inside the `claude-dev` microVM, started
at boot. `HTTPS_PROXY` env var routes apps through tinyproxy. iptables
default-deny rejects anything not going via the proxy.

- **Lines added:** ~80
- **Bypass:** agent has root + `CAP_NET_ADMIN`, can `iptables -F` and
  unset `HTTPS_PROXY`. Pure honor system.
- **Sandbox compatibility:** keeps working.

### Option 2A — Run agent as a non-root UID

Image setup creates a `claude` user; `container run` execs into it; the
agent never has `CAP_NET_ADMIN`.

- **Lines added:** ~150–300 across `start-claude.sh` and image setup
- **Breaks:**
  - `~/.claude-containers/shared/` and `claude.json` ownership — one-time
    `chown -R` migration; reverting brings root-owned files back.
  - In-container `apt-get` needs sudo. Don't install sudo (becomes a back
    door). Lose interactive apt for debugging.
  - Project-dir UID mismatch: macOS bind mounts surface as the macOS
    user's UID (typically 501). If the in-container UID is 1000, git
    complains about dubious ownership. Fix is to align UID 501, but
    that's a new convention to maintain.
  - Claude Code installer `~/.local/bin` moves from `/root/` to
    `/home/claude/`. Symlink path retarget.
  - `/root/.claude*` bind-mount destinations either move to
    `/home/claude/.claude*` (breaks compatibility with start-agent.sh's
    shared-state assumption) or `/root` gets chowned to the agent user
    (ugly).
- **Sandbox compatibility:** keeps working — Debian ships `bwrap`
  setuid root, so a non-root caller can still set up the bubblewrap
  sandbox.
- **Performance:** zero overhead.

### Option 2B — Stay root, drop `CAP_NET_ADMIN` / `CAP_NET_RAW` via `capsh` in entrypoint *(recommended baseline)*

Entrypoint script runs as root with full caps, sets up tinyproxy +
iptables, then `exec capsh --drop=cap_net_admin,cap_net_raw -- /bin/bash
-l`. The shell and all descendants inherit the dropped cap mask; the
agent stays UID 0 for filesystem and process purposes, but cannot modify
network state.

- **Lines added:** ~30–60
- **Breaks:** almost nothing.
  - **Hard prereq:** do not install sudo. Currently `start-claude.sh`
    does not, so we just need to keep it that way. With sudo + NOPASSWD,
    the cap drop becomes a fig leaf.
  - Reloading the allowlist in a running container is impossible (agent
    can't re-run iptables). Restart the container to apply changes —
    same as Option 1's situation in practice.
- **Sandbox compatibility:** keeps working. Bubblewrap needs
  `CAP_SYS_ADMIN` (mount namespace), not `CAP_NET_ADMIN`. Setuid `bwrap`
  regains all caps when invoked, so the sandbox setup path is unaffected
  by the parent's reduced cap mask.
- **Performance:** zero overhead.

### Comparison

| | Option 1 | Option 2A | Option 2B |
|---|---|---|---|
| Lines added | ~80 | ~250 | ~50 |
| Breaks existing UX | no | yes (apt, ownership, paths) | no |
| Sandbox still works | yes | yes | yes |
| Agent can `iptables -F` | yes | no | no |
| Agent can re-acquire caps | n/a | only if sudo | only if sudo |

**Working recommendation:** 2B. ~30 lines more than soft enforcement,
breaks nothing, keeps sandbox mode, and gives a hard guarantee against
the specific threat (agent disabling its own firewall).

## Multi-container architecture (the bigger fork)

If the future setup is single-microVM (claude + opencode coexisting in
one container), 2B is enough. The question becomes interesting once
auxiliary services (SearXNG, etc.) live in their own microVMs — i.e. the
start-agent.sh feature parity case.

### 2B per workload container does not scale cleanly

For N workload containers, you have N copies of the iptables setup, N
copies of the tinyproxy filter file, and N reload paths. Allowlist drift
is real. `--reload-allowlist` either fans out or becomes bake-in-only.

### Dedicated egress container — the architecture worth committing to

```
                          ┌────────────────────────┐
                          │ macOS host             │
                          │  (no pf, no iptables)  │
                          └────────┬───────────────┘
                                   │ bridge100 (vmnet)
        ┌──────────┬───────────────┼───────────────┬──────────┐
        ▼          ▼               ▼               ▼          ▼
  ┌──────────┐ ┌──────────┐ ┌─────────────┐ ┌──────────┐ ┌──────────┐
  │ claude   │ │ opencode │ │  egress     │ │ searxng  │ │ ...      │
  │ microVM  │ │ microVM  │ │  microVM    │ │ microVM  │ │          │
  │          │ │          │ │             │ │          │ │          │
  │ caps:    │ │ caps:    │ │ tinyproxy   │ │          │ │          │
  │  -NET_*  │ │  -NET_*  │ │  + filter   │ │          │ │          │
  │          │ │          │ │  + iptables │ │          │ │          │
  │ iptables:│ │ iptables:│ │  (its own)  │ │          │ │          │
  │  only →  │ │  only →  │ │             │ │          │ │          │
  │  egress  │ │  egress  │ └──────┬──────┘ │          │ │          │
  │  + DNS   │ │  + DNS   │        │        │          │ │          │
  └──────────┘ └──────────┘        │        └──────────┘ └──────────┘
                                   ▼
                              outbound internet
                              (allowlist filtered)
```

**Workload containers:** small, static iptables ruleset — allow to
`egress-container:8888` (tinyproxy), allow DNS, reject everything else.
Locked in via 2B's cap-drop at entrypoint. No allowlist content; no
reload path. Agent's `HTTPS_PROXY` is the only viable route out; if
agent unsets it, raw connections die at the REJECT.

**Egress container:** its own microVM, its own kernel, runs tinyproxy
with the actual allowlist. Cap-dropped agents in workload containers
cannot touch its iptables or filter file. `--reload-allowlist` hits
this one container only.

**SearXNG:** points at `egress:8888` via `outgoing.proxies`, same as
start-agent.sh. Optionally same minimal iptables ruleset for symmetry.

### Why this beats 2B-everywhere

| | Per-container 2B × N | Dedicated egress |
|---|---|---|
| Allowlist locations | N (drift risk) | 1 |
| `--reload-allowlist` blast radius | N containers | 1 container |
| Agent isolation from filter rules | cap-drop only | cap-drop **and** separate kernel |
| Adding a new workload container | duplicate full firewall | add ~5 static iptables lines |
| Lines of iptables in workloads | grows with allowlist | small + fixed |

Recovering the "different kernel" property is the material upgrade. It
restores the start-agent.sh design property — the firewall lives where
the agent can't reach it — without needing a host-level VM layer.

### When it's overkill

For N=1 (claude + opencode in one microVM, no separate auxiliary
services), 2B alone is enough. Two microVMs to do what 2B does in one
isn't worth it. The decision pivot is: **are SearXNG / other services
expected to live in separate Apple Containers in the future setup?**

## Open questions for the user (resolve before `/scope`)

1. **Multi-container scope.** Are we planning around N=1 (single
   microVM), single-microVM-with-egress-sidecar (N=2), or full
   multi-microVM with auxiliary services (N≥3)? The architecture
   choice depends on this; ~30 lines of cap-drop vs. a real egress
   container is the cost difference.
2. **Allowlist file location.** Three options:
   - Shared `~/.claude-containers/allowlist.txt` (requires migrating
     start-agent.sh).
   - Separate `~/.claude-dev/allowlist.txt` (parallel to claude-agent's;
     drift risk).
   - Reuse `~/.claude-agent/allowlist.txt` (tight coupling; edits affect
     both environments).
3. **Runtime apply vs. bake-time.** Apply iptables / filter on every
   `start-claude.sh` invocation (matches `--reload-allowlist` UX) or
   bake into the image at build (allowlist edits require `--rebuild`)?
4. **Carve-outs.** Loopback + DNS only (mirrors start-agent.sh minus
   the host-inference RETURN rule), or also explicit non-proxy paths
   for specific known-good destinations? Default: loopback + DNS only.

## Unknowns to verify before committing to a design

1. **Container-to-container routing on `bridge100`.** Apple Containers'
   networking has historically had rough edges; earlier versions didn't
   always route between containers on the default bridge cleanly. Verify
   that two `container run`s can reach each other by IP. Load-bearing
   for the dedicated-egress design.
2. **IP stability for the egress container.** Workloads need to
   hardcode `egress:8888` (or its IP) in their iptables and proxy env.
   If Apple Containers does DHCP-style allocation per run, we need a
   stable-IP mechanism (static assignment flag, `/etc/hosts` injection,
   or DNS).
3. **Inter-container DNS.** start-agent.sh creates a user-defined docker
   network so `searxng` resolves by name (Docker embedded DNS only
   works on user-defined networks). Apple Containers may or may not
   have an analog. Fallback is hardcoded IPs or host-file injection.
4. **`--cap-drop` flag in `container run`.** capsh-in-entrypoint
   sidesteps this, but a runtime-level cap drop would be belt-and-
   suspenders. Worth probing.
5. **Apple Containers respect for `capsh` cap drops.** The cap drop is
   a userspace operation against the kernel's per-task cap masks, so
   should be runtime-agnostic. One-line sanity check: drop a cap in an
   entrypoint, verify with `capsh --print` from a child process.
6. **Setuid-bwrap behavior under reduced parent caps.** Verify that
   setuid `bwrap` still successfully sets up the sandbox when the
   invoking process has dropped `CAP_NET_ADMIN` / `CAP_NET_RAW`.
   Expected to be fine (bwrap uses `CAP_SYS_ADMIN`), but worth a
   smoke test on real Debian-in-microVM before committing.
7. **Whether sudo gets pulled in transitively.** Currently
   `start-claude.sh` does not install sudo. Audit that no future
   apt-get adds it as a recommended dep. The cap-drop guarantee
   collapses if sudo + NOPASSWD ever lands in the image.

## Notes / aside

- The `start-agent.sh` allowlist file (`~/.claude-agent/allowlist.txt`)
  is already a reasonable seed. ~280 entries covering AI APIs, package
  registries, OS package repos, scientific publishers, gov stats sites,
  major universities. Reuse-or-fork is partly a management question and
  partly a "do these two environments need different surface areas"
  question. Likely the same.
- start-agent.sh's filter regex generation (each non-comment domain
  becomes `(^|\.)domain\.tld$` for tinyproxy `FilterExtended Yes`) ports
  directly. No redesign needed at that layer.
- `HTTP_PROXY` / `HTTPS_PROXY` env wiring is identical to start-agent.sh,
  including `NO_PROXY=localhost,127.0.0.1,$EGRESS_IP` and
  `NODE_USE_ENV_PROXY=1`.
- Bubblewrap sandbox interaction is the most likely surprise surface.
  Worth running `claude --dangerously-skip-permissions` (or normal
  sandboxed flow) in a 2B prototype before scaling out.
