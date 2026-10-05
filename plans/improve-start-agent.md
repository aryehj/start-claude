# start-agent.sh: worthwhile improvement opportunities

## Status

- [x] Batch 1: security and correctness, small diffs (A1, A3, A4, B1, B4, B6, B7, C3)
- [x] Batch 2: close the firewall gaps (F1 agent-net jump, F2 INPUT chain, F3 DNS blackhole, F4 smoke tests) — implemented (ca69bce, 4c7d42d, 1e65222, b0fc3be); check after the live smoke on the Mac passes
- [x] Batch 3: container lifecycle (L1 one topology, L2 start-failure handling, L3 SearXNG pin) — implemented (bd4bdf6, 06b7f51, 887d943); check after the live smoke on the Mac passes
- [x] Batch 4: allowlist (W1 validation, W2 shared template) — implemented (4f0ef92, df10d4a); check after the live smoke on the Mac passes
- [ ] Batch 5: perf and cleanup (P1 reload handshakes, P2 `colima list` dedupe, P3 drop no-op limits, P4 `SCRIPT_DIR`) — implemented (2f12410, 138524a, 314f4d2, 7533ef8); check after the live smoke on the Mac passes
<!-- mark [x] as items land -->

Revised 2026-10-05 after a critique of the remaining batches. What changed, and why, is in
"Revision notes" at the end. Batches 2 to 5 replace the old Batches 2 to 4 and the two
"decisions pending" items.

## Implementation ground rules

- All work on this branch (`improve-start-agent`), never on `main`, so it can be tested and
  reverted as a unit.
- One commit per substantive change (one lettered item, or one tightly coupled pair), each
  with passing static tests. No `Co-Authored-By` lines (see CLAUDE.md).
- Keep the exact-string shapes `tests/test_agent_sh.py` anchors on (listed under Verification).
- Update ADR.md / CLAUDE.md / README.md in the same commit as the behaviour they describe.
- **Nothing may run in a container before the firewall is applied.** The iptables rules are not
  persisted in the VM; the script installs them on every run, before any `docker start` /
  `docker run`. Do not add restart policies, boot-time units, or reordering that breaks this.
- Locations below are given as function names or anchor strings, not line numbers. The script
  changes between batches; `grep` for the anchor.

## Context

`start-agent.sh` is the Colima-backed agent launcher. A full read of the script, its tests
(`tests/test_agent_sh.py`, `tests/test-agent-firewall.sh`, `tests/test-cross-vm-isolation.sh`),
the relevant ADRs, and the existing plans surfaced the findings below.

**Scoped elsewhere, not repeated here:** the readability refactor in
`plans/simplify-start-agent-full-pass.md` (unify the two duplicated `discover_models()` Python
blocks, extract the firewall heredoc, compact the arg parser, add a `main` driver). Its Phase 1
(allowlist template extraction) is **moved into this plan** as W2, because the template should
be shared with `start-claude.sh` rather than extracted to a second copy.

**Deliberately removed before, do not re-add:** the SearXNG `outgoing.proxies` drift check and
the 30-day image-staleness warning (`plans/_paused - simplify-start-agent.md`, steps 1 and 4).

---

## Batch 1 (landed)

| Item | What | Commit |
|---|---|---|
| A1 | `CLAUDE_AGENT` chain applied atomically via `iptables-restore --noflush` | `dce284c` |
| A3 | `DOCKER_CONTEXT` export instead of `docker context use` | `1d1a804` |
| A4, B1 | Value-less `-e` for secrets; forward `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | `21216e8` |
| B4 | VM memory compared as integer GiB | `55cbea3` |
| B6 | VM probe failure reported instead of silent exit | `db71705` |
| C3 | EXIT trap kills the background inference probe | `7f803ec` |
| B7 | Flag hygiene (combination checks, allowlist backup, `python3` preflight, usage text) | `f173e4d` |

---

## Batch 2: close the firewall gaps

Observed on 2026-10-05 from inside a running `claude-agent` container (search enabled, container
at `172.18.0.3` on `claude-agent-net`):

| Probe | Result |
|---|---|
| `getent hosts example.com` | resolves (0.16 s) |
| `/etc/resolv.conf` | `nameserver 127.0.0.11`, `# ExtServers: [host(192.168.5.1)]` |
| TCP `172.17.0.1:22` and `172.18.0.1:22` | open, banner `SSH-2.0-OpenSSH_9.6p1 Ubuntu` (the VM's sshd) |
| TCP `192.168.5.1:53` | open |
| TCP `172.17.0.1:8888` | open (tinyproxy, expected) |
| TCP `192.168.5.2:22`, `1.1.1.1:443` | `No route to host` (the chain's REJECT, expected) |

Observed the same day in the VM (`ip -4 addr`, `ss -lntup`, `iptables -S INPUT`):

| Fact | Detail |
|---|---|
| VM-local addresses | `192.168.5.1` (eth0), `192.168.64.2` (col0), `172.17.0.1` (docker0), `172.18.0.1` (the `claude-agent-net` bridge) |
| dnsmasq | udp and tcp 53 on `192.168.5.1` and `127.0.0.1`; this is the resolver embedded DNS forwards to |
| sshd | tcp 22 on `0.0.0.0` and `[::]`, so reachable on every address above |
| tinyproxy | tcp 8888 on `172.17.0.1` only |
| INPUT chain | `-P INPUT ACCEPT`, no rules |

IPv6 is not a second path: the container's `eth0` has `disable_ipv6=1` and no v6 address, so
`ip6tables` needs no matching rules.

Not verified: that dnsmasq answers a query sent straight to `192.168.5.1:53` from a container.
It accepts the TCP connection and nothing filters INPUT, so assume it does.

Two structural facts explain the table:

- Docker's embedded DNS forwards external names from the **host** network namespace (the
  `host(...)` marker), so those queries never traverse FORWARD and `CLAUDE_AGENT` cannot see them.
- Traffic from a container to a **VM-local address** (either bridge gateway, and apparently
  `192.168.5.1`) is delivered through the INPUT chain, not FORWARD. `CLAUDE_AGENT` hangs off
  DOCKER-USER, which is FORWARD only. So nothing governs container access to services listening
  on the VM, and the three `-d $BRIDGE_IP` RETURN rules in `CLAUDE_AGENT` (tinyproxy port,
  udp 53, tcp 53) can never match. The script's own comment above the agent-net jump
  ("does NOT go through FORWARD") already says this for tinyproxy.

### F1. The agent-net jump is skipped when search is disabled (fail-open)
In the `vm-probe.sh` heredoc, `AGENT_NET_CIDR` is only read inside
`if [ "$LOCAL_SEARCH_ENABLED" = "true" ]`. In the `firewall-apply.sh` heredoc, the
`-s "$AGENT_NET_CIDR" -j CLAUDE_AGENT` jump is only installed when that variable is non-empty.
Sequence that leaves egress unfiltered: container created with search enabled (so it is on
`claude-agent-net`), VM restarted (rules gone), next run uses `--disable-search` or
`CLAUDE_AGENT_DISABLE_SEARCH=1`. The container is reattached, still on `claude-agent-net`, and no
jump covers its source CIDR. Found by reading the code; not reproduced live.

**Fix:** remove the `LOCAL_SEARCH_ENABLED` guard around the `agent_cidr` read in the probe, so the
jump is installed whenever the network exists. Keep the guard on the `--dport 8080` RETURN rule;
that one really is search-only. L1 then makes the network always exist. Land F1 first: it is two
lines and closes the hole on its own.

### F2. Govern INPUT: containers can reach VM-local services
INPUT is `ACCEPT` with no rules, so a container can open a connection to the VM's sshd on any
of its four addresses and to dnsmasq on `192.168.5.1:53`. sshd still needs a key the container
does not have, so this is attack surface rather than an open door; dnsmasq is a working
resolver that bypasses F3.

**Fix:** add a second chain, `CLAUDE_AGENT_IN`, declared in the **same** `iptables-restore
--noflush` stanza as `CLAUDE_AGENT` so both swap in one commit:
```
:CLAUDE_AGENT_IN - [0:0]
-A CLAUDE_AGENT_IN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
-A CLAUDE_AGENT_IN -d $BRIDGE_IP -p tcp --dport $TINYPROXY_PORT -j RETURN
-A CLAUDE_AGENT_IN -j REJECT --reject-with icmp-admin-prohibited
```
Jump to it from INPUT for each container source CIDR, using the same idempotent `-C || -I`
pattern as the DOCKER-USER jumps, after the restore:
```
iptables -C INPUT -s "$BRIDGE_CIDR"    -j CLAUDE_AGENT_IN || iptables -I INPUT 1 -s "$BRIDGE_CIDR"    -j CLAUDE_AGENT_IN
iptables -C INPUT -s "$AGENT_NET_CIDR" -j CLAUDE_AGENT_IN || iptables -I INPUT 1 -s "$AGENT_NET_CIDR" -j CLAUDE_AGENT_IN
```
Then delete the three dead `-d $BRIDGE_IP` rules from `CLAUDE_AGENT`.

The jumps match on **source** CIDR only, never on destination, so one pair of rules covers all
four VM-local addresses, including `192.168.64.2` on col0.

Things that must keep working, and why they do:
- Agent and SearXNG to tinyproxy at `$BRIDGE_IP:$TINYPROXY_PORT`: the one RETURN rule.
- Container-name resolution (`searxng`): embedded DNS at `127.0.0.11` is handled inside the
  container's own namespace, not via VM INPUT.
- Inference at `$HOST_IP:$INFERENCE_PORT` and agent to `searxng:8080`: FORWARD, unchanged.
- `colima ssh` from the Mac and `docker build --network=host`: source is not a container CIDR.

Test anchors: `test_firewall_chain_applied_atomically` matches the literal strings
`-A CLAUDE_AGENT -j REJECT` and `-j CLAUDE_AGENT 2>/dev/null`; the `_IN` suffix keeps the new
rules from colliding with either. Add a parallel static test for `CLAUDE_AGENT_IN`.

### F3. Blackhole external DNS for the agent container
The channel is confirmed (table above): arbitrary external names resolve from inside the
container even though all direct egress is rejected. That is an information leak and a
low-bandwidth covert channel.

**Fix:** add `--dns 192.0.2.1` (RFC 5737, never routed) to the agent container's `docker run`
(the block containing `"$IMAGE_TAG"`). With an explicit `--dns`, embedded DNS should forward
external queries from the **container's** namespace instead of the host's, so they hit
`CLAUDE_AGENT`'s REJECT and fail fast. Nothing on the proxy path needs local resolution: curl,
git, npm, uv, pip and Node with `NODE_USE_ENV_PROXY=1` all hand the hostname to tinyproxy via
CONNECT, and every `NO_PROXY` entry is an IP, `localhost`, or `searxng`.

Leave SearXNG's `docker run` alone: it is not the adversary and reaches engines through
`outgoing.proxies`.

Must be verified live before the commit is kept:
- `getent hosts example.com` fails, and fails **fast** (under about a second). If it hangs for a
  resolver timeout instead, the forwarding-namespace assumption is wrong; say so and stop.
- `getent hosts searxng` still resolves.
- A Claude Code login round-trip and one `opencode` / `pi` request still work.

F2 is what closes the direct route (`192.168.5.1:53`); F3 alone would leave it open. Land F2
before F3.

`--dns` is a create-time flag. An existing container keeps its old resolver until it is
recreated. On reattach, read `{{json .HostConfig.Dns}}` from the existing container and, if the
blackhole is absent, print a warning that names the fix (`start-agent.sh --reset-container`).
Do not recreate automatically (see L2 for why).

### F4. Smoke tests
Add to `tests/test-agent-firewall.sh`, following the existing `test_*` / `pass` / `fail` /
`skip` shape:
- External DNS is blackholed: `getent hosts example.com` must fail.
- Container-name DNS works: `getent hosts searxng` must succeed (skip when search is disabled).
- VM-local services are closed: TCP connect to the default gateway on port 22 must fail, while
  the proxy port on `$BRIDGE_IP` still connects.

Update the header comment's "5 of the 6" count and the README test table.

### Docs for Batch 2
ADR-010 currently lists "bridge DNS" among the allowed destinations in three places and does not
mention INPUT. Amend it in the F2 and F3 commits: FORWARD-only coverage was the gap, INPUT is now
governed by `CLAUDE_AGENT_IN`, external DNS is blackholed for the agent container. Update the
CLAUDE.md "Egress allowlist" bullet to name both chains.

---

## Batch 3: container lifecycle

### L1. One network topology, whatever the search flag says
Today the agent container is on `claude-agent-net` when search is enabled and on the default
bridge when it is not, and the reattach check (the block starting
`if docker container inspect "$CONTAINER_NAME"`) only compares the projects mount. Toggling
`--disable-search` against an existing container therefore leaves it on the wrong network:
search silently fails one way round, and F1's hole opens the other way round.

**Fix:** always create `claude-agent-net` and always pass `--network "$AGENT_NET_NAME"` to the
agent container's `docker run`. The search flag then only controls whether SearXNG runs, whether
the `8080` RETURN rule is emitted, and whether the MCP block is written. `NETWORK_ARGS` and its
conditional go away.

Migration for existing containers that are not on the network: on reattach, inspect
`.NetworkSettings.Networks`; if `claude-agent-net` is missing, run
`docker network connect "$AGENT_NET_NAME" "$CONTAINER_NAME"` before `docker start`. This is
additive and works on stopped and running containers. Never `docker network disconnect`: it
would leave a container with no network unless paired with a `connect bridge`, and it cuts live
shells.

### L2. Start failures: report them; self-heal only SearXNG
`attach_existing` runs `docker start "$CONTAINER_NAME" >/dev/null 2>&1 || true` and then
`exec docker exec`, so a container that cannot start (the `RWLayer nil` case in ADR-041) dies
with Docker's bare "container is not running". The SearXNG branch
(`docker start "$SEARXNG_CONTAINER" >/dev/null || true`) hides the failure entirely.

**Fix, agent container:** capture `docker start`'s stderr; afterwards check `.State.Running`. On
failure print the captured stderr and exit non-zero with the remedy:
`start-agent.sh --reset-container`. Do **not** `docker rm -f` automatically. Unlike
`research.py`'s daemons, this container is a long-lived dev environment whose root filesystem
may hold tools the user installed, and many start failures (missing network, mount not ready)
are fixable without destroying it.

**Fix, SearXNG:** it is stateless (settings are bind-mounted), so port the `start_or_recreate`
semantics from `research.py`: on a failed start or a not-`Running` state, print the stderr,
`docker rm -f`, and fall through to the existing `docker run`. Keep that `docker run` as one
backslash-continued command containing `SEARXNG_CONTAINER` so the static tests keep finding it.

**No `--restart unless-stopped` on either container.** It would break the ground rule above:
dockerd would start the containers at VM boot, before the script has installed any rules. On
the agent container it would also turn `exit` into a restart, because the main process is an
interactive `bash`. The script already starts SearXNG on every run, which covers the same need.

### L3. Pin the SearXNG image
`docker.io/searxng/searxng` is the only unpinned runtime image. Introduce one constant,
`SEARXNG_IMAGE`, pinned by digest, with a dated comment: bump it when engines stop returning
results. The value has to come from the Mac, since the implementing session cannot reach the
registry or the VM's docker. Pin what is already running and known to work:
```
DOCKER_CONTEXT=colima-claude-agent docker image inspect docker.io/searxng/searxng \
  -f '{{index .RepoDigests 0}}'
```
If the user has not supplied the digest, leave L3 unchecked rather than guessing a tag.

---

## Batch 4: allowlist

### W1. Validate allowlist entries
`generate_filter_file` regex-escapes whatever is on the line. A pasted `https://foo.com` or
`foo.com/path` becomes a pattern that can never match a hostname, so the entry silently does
nothing and the user sees an unexplained 403. `re.escape` also emits `\-` for hyphenated
domains (`rust-lang.org`), which is undefined in POSIX ERE (glibc tolerates it).

**Fix:** in the Python generator, per non-comment line:
1. Lowercase; strip a leading `*.` and a trailing `.` (so entries copied from
   `sandbox.network.allowedDomains` style lists keep working; suffix match already covers
   subdomains).
2. Accept only names with at least two non-empty labels:
   `^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$`. This rejects URLs,
   paths, ports, and single labels such as `com` (which would allow a whole TLD). IPv4
   literals pass, which is intended.
3. For anything else print `warning: allowlist.txt:N: '...' is not a bare domain, skipped` to
   stderr.
4. Escape only `.` by hand; drop `re.escape`.

Make the `--reload-allowlist` summary count the lines of the generated filter rather than
`grep`-ing the source file, so it reports live entries.

### W2. One allowlist template for both scripts (moved from the simplify plan's Phase 1)
The seed heredoc in `start-agent.sh` (`cat > "$ALLOWLIST_FILE" <<'ALLOWLIST'`) and
`templates/sandbox-allowlist.txt` carry the same 227 entries, maintained by hand. They are not
byte-identical: the header and three comment lines are worded per script (286 vs 299 lines).

**Fix:** keep the existing filename `templates/sandbox-allowlist.txt` (so `start-claude.sh`,
`tests/test_sandbox_allowlist.py` and ADR-044 need no rename), rewrite its header and the
script-specific comment lines to describe both consumers neutrally, and have `start-agent.sh`
seed from it. Details:
- Add the template to preflight, next to the Dockerfile check.
- Seed and reseed by writing **into** the existing file (`cat template > "$ALLOWLIST_FILE"`),
  never by `mv`: the file is a single-file bind mount in the container and a new inode would
  leave the container looking at the old one.
- Keep the `.bak` behaviour from B7.
- Expect coordinated test updates; any test that counts on the heredoc should move to asserting
  the template path.

`plans/simplify-start-agent-full-pass.md` Phase 1 is marked as moved here.

---

## Batch 5: perf and cleanup

Per ADR-045 a `colima ssh` handshake costs roughly 100 to 300 ms.

### P1. `--reload-allowlist` costs 9 handshakes plus 1 to 3 in the background
Mount check (1) + batch probe (1) + `vm_put_file` twice at 2 each (4) + hash write (1) +
tinyproxy reload (1) + firewall apply (1), plus the background inference probe (1 to 3).

**Fix:** replace the push block (the `if $RELOAD_ALLOWLIST || [[ "$NEW_PROXY_HASH" ...` body)
with one `vm_sh` script that carries both files and the hash as base64 payloads, writes them,
and runs the reload-or-restart. Skip launching the inference probe when `RELOAD_ALLOWLIST` is
set; it is killed unread on that path. Saves about five handshakes on the path advertised as
fast. Keep `test_tinyproxy_push_hash_gated` and
`test_tinyproxy_reload_not_unconditional_restart` passing.

### P2. One `colima list` call
`colima_profile_running` and the sizing warning each call `colima list --json`. Call once,
keep the JSON, parse once.

### P3. Drop the no-op resource limits
The agent `docker run` passes `--memory "${CLAUDE_AGENT_MEMORY_GB}g"` and
`--cpus "$CLAUDE_AGENT_CPUS"`. Both equal the whole VM, and they use the *requested* size, which
B4 showed can differ from the running VM. They limit nothing while implying a limit. Remove
both. (A computed cap with headroom was considered and rejected: on a small VM a fixed reserve
is a large fraction of the total, and the gain is marginal.)

### P4. `SCRIPT_DIR` recomputed
`DOCKERFILE_PATH` and `GLOBAL_CLAUDEMD_TEMPLATE` each re-derive the script directory. Use
`$SCRIPT_DIR`.

---

## Dropped

- **Skills source (old B8).** `sync_skills` fetches the upstream tarball while
  `seed_agent_skills` copies from the checkout. ADR-005 chose upstream on purpose, the fetch
  runs on the Mac and already degrades to a warning, and there is no bug behind the proposal.
- **Folding the virtiofs mount check into the batch probe (part of old C2).** One handshake
  saved for a reordering of the VM bring-up path, which also has to re-probe after a restart.
- **Container memory headroom (old C4 alternative).** See P3.
- **Restart policies and auto-recreate for the agent container (part of old B3).** See L2.

## research.py follow-ups (separate plan or commits, not part of these batches)

- `docker context use` is still called there; switch to `DOCKER_CONTEXT` as in A3.
- The VM memory warning still does a substring match on a byte count, as B4 fixed here.
- Pin its SearXNG image to the same digest as L3.
- Its containers do use `--restart unless-stopped`. That is acceptable under a default-allow
  denylist, but worth a note in ADR-041 that they start before the `RESEARCH` chain exists
  after a VM boot.

## Verification

- `python3 -m pytest tests/test_agent_sh.py tests/test_dockerfile.py tests/test_sandbox_allowlist.py`
  after every commit. The static tests anchor on exact strings (`# Fresh container:`,
  `exec docker run`, the single-command `docker run` blocks found by `IMAGE_TAG` and
  `SEARXNG_CONTAINER`, the `-*)` unknown-option arm, the `CLAUDE_AGENT` restore stanza); keep
  those shapes. Add a static test alongside each behavioural change, as Batch 1 did.
- `bash -n start-agent.sh`.
- Live smoke on the Mac host, which the user runs (the implementing session has no VM): cold
  sandbox run, warm reattach, `--reload-allowlist`, `--reset-container`, a `--disable-search`
  toggle against an existing container, then `tests/test-agent-firewall.sh` inside the
  container and `tests/test-cross-vm-isolation.sh` from the host.
- F1: with a search-enabled container, `colima stop -p claude-agent`, start with
  `--disable-search`, and confirm test 1 (`test_default_deny`) still passes.
- F2: `colima ssh -p claude-agent -- sudo iptables -S CLAUDE_AGENT_IN` shows the three rules;
  from the container, port 22 refuses on `172.17.0.1`, `172.18.0.1`, `192.168.5.1` and
  `192.168.64.2`, and `192.168.5.1:53` refuses; `colima ssh` from the Mac still works.
- F3: the three checks listed under F3.
- L1: an old bridge-only container gains `claude-agent-net` on the next run and
  `getent hosts searxng` resolves in it.
- W1: an allowlist containing `https://foo.com`, `*.bar.com` and `com` yields two warnings and a
  filter entry for `bar.com`.

## Revision notes (2026-10-05)

- Old A2 (DNS) was "decision pending, verify first". Verified: the channel is real. It is now
  F3, and it depends on a gap the first pass missed (INPUT is ungoverned, F2).
- Old B2 (search toggle) was filed as a functionality bug with a `network connect` /
  `disconnect` fix. It is also a fail-open firewall bug (F1), and the lifecycle fix is now a
  single topology (L1).
- Old B3 ported `research.py`'s `--restart unless-stopped` and auto-recreate to both
  containers. Narrowed to L2 for the reasons given there.
- Old B5's validation regex accepted single labels and rejected wildcard forms; tightened in
  W1. The "byte-identical" claim about the two allowlists was wrong in the comments, right in
  the entries; W2 accounts for that.
- Line-number references (taken at `1d1da8f`, before Batch 1 moved everything) are replaced by
  anchors.
