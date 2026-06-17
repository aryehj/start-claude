# Speed up start-agent.sh warm VM reattach

## Status

- [x] Batch the read-only VM probes into a single `colima ssh` round-trip (Opus recommended)
- [x] Push tinyproxy config + reload only when it changed; drop the unconditional restart (Opus recommended)
- [x] Run the inference-backend probe in the background, overlapping it with later host-side work
- [x] Update docs (ADR + CLAUDE.md key-decisions, README env notes if affected) (Haiku ok)

## Context

`start-claude.sh` reattaches fast because it does almost nothing on a warm path:
`container start` + `exec`. `start-agent.sh` re-runs its full VM provisioning
sequence on every invocation, even when the VM and container are already up and
nothing changed. The dominant cost is a chain of separate `colima ssh`
invocations, each paying a fresh SSH handshake. `vm_ssh`/`vm_sh`
(`start-agent.sh:693-706`) shell out to `colima ssh` per call; `vm_put_file`
(`711-714`) is two `colima ssh` calls (tee + chmod).

On the warm-reattach path (no `--rebuild`, no `--reload-allowlist`, tinyproxy
already installed, search enabled), the following each open their own SSH
session:

- `749` mount check (gates a possible VM restart)
- `790` + `795` — two `docker network inspect bridge` calls (same network, gateway then subnet)
- `804` default-route host IP (plus `807` getent fallbacks only if that fails)
- `828` agent-net subnet inspect
- `882` `dpkg-query` tinyproxy-installed check
- `920` + `921` — `vm_put_file` ×2 = four SSH calls pushing conf + filter
- `928` + `929` — `enable --now` then an **unconditional** `systemctl restart tinyproxy`
- `985` firewall apply (one call)
- `1000`/`1015` inference probe — `curl --max-time 3`, blocks up to 3s when the host model server is down

This plan removes most of those round-trips without changing the SSH transport
(decision: keep `colima ssh`, just make fewer calls) and without changing the
invariant that every attach re-asserts the egress rules. The firewall apply at
`985` is left as-is.

## Goals

- Warm reattach issues far fewer `colima ssh` round-trips — the read-only probes collapse to one, and tinyproxy is not pushed/restarted when its config is unchanged.
- No behavior change to the security model: the CLAUDE_AGENT iptables chain is still flushed and rebuilt on every attach; tinyproxy still ends up running with the correct config; the allowlist is still mounted `:ro`.
- `--reload-allowlist` still always reloads tinyproxy (its contract), and `--rebuild` / `--reset-container` paths are unaffected.
- The inference probe no longer adds wall-clock latency on the attach path; its warning still prints before the container is attached.

## Approach

The through-line is "cut per-attach SSH handshakes, don't touch the transport
or the trust boundary." Two independent moves: (1) gather every read-only fact
the script needs from the VM in one `vm_sh` heredoc that echoes `KEY=value`
lines parsed on the host, and (2) make the tinyproxy push+restart conditional on
a content hash so the warm path skips it entirely. The inference probe is moved
off the critical path by backgrounding it and collecting the result before the
final `exec docker run`, so its up-to-3s timeout overlaps the iptables apply and
the host-side Python config injection (`1107-1414`) that run regardless.

## Steps

1. **Batch the read-only probes (`789-838`, `882`).** Replace the separate
   `vm_ssh` discovery calls with one `vm_sh` heredoc that emits `KEY=value`
   lines for: bridge gateway and subnet (a single `docker network inspect
   bridge` with a multi-field Go template covers both, eliminating the duplicate
   inspect at `790`/`795`), the default-route host IP, the tinyproxy package
   status (`dpkg-query`), the stored tinyproxy config hash (see step 2), and —
   when `$LOCAL_SEARCH_ENABLED` — the agent-net subnet. Parse the output on the
   host and assign the existing variables (`BRIDGE_IP`, `BRIDGE_CIDR`,
   `HOST_IP`, `AGENT_NET_CIDR`, plus new vars for tinyproxy-installed and the
   stored hash). Preserve every current fallback (bridge `172.17.0.1` /
   `172.17.0.0/16`, the `host.lima.internal` / `host.docker.internal` getent
   fallbacks for `HOST_IP`, the agent-net `172.20.0.0/24` fallback and its
   warning). Keep the agent-net create-if-missing as a separate conditional
   write (`830`) — only when the batched probe reports it absent — followed by a
   re-read of its CIDR; this only happens on first run.

2. **Conditional tinyproxy push + reload (`894-930`).** Compute a host-side hash
   of the generated `tinyproxy.conf` + `filter` contents. Compare against a
   marker stored in the VM (read as part of step 1's batch; pick a path the
   batched probe can read, using `sudo` within the heredoc if it lives under
   `/etc/tinyproxy`). Push the two files and write the new marker **only when**
   the hash differs **or** tinyproxy is not currently active. When a push
   happens, reload (`systemctl reload`, falling back to `restart`) rather than
   the current unconditional `restart`; when nothing changed and tinyproxy is
   active, skip the push, the restart, and the now-redundant `enable --now`
   entirely. Keep the first-run install block (`882-892`) — gated on the batched
   `dpkg-query` result instead of its own `vm_ssh`. Ensure `--reload-allowlist`
   still forces a reload regardless of the hash (its whole purpose); the
   simplest correct behavior is that an edited allowlist changes the filter
   content and thus the hash, but do not let an unchanged-hash no-op silently
   swallow an explicit `--reload-allowlist` — preserve that command's "always
   reload" contract.

3. **Background the inference probe (`996-1025`).** Launch the probe
   (`vm_ssh curl --max-time 3 …`) as a background job right after `HOST_IP` is
   known, redirecting its warning text to a temp file. Keep the `==> Probing …`
   line at launch. `wait` for the job and emit any captured warning before the
   container is attached (before `exec docker run` at the end of the file, e.g.
   around the image-build step at `1027`) so there is no orphaned background
   process surviving the `exec`. The 3s timeout then overlaps the iptables apply
   (`985`) and the host-side config injection (`1107-1414`), which run
   regardless. Preserve both backend branches (ollama `/api/tags`, omlx
   `/v1/models` with optional bearer header) and their distinct warning text.

4. **Docs.** Add an ADR (next number is ADR-045; ADR-044 is current highest in
   `ADR.md`) recording the warm-reattach optimization: batched read-only probes,
   hash-gated tinyproxy push/reload, backgrounded inference probe — and the
   explicit decision to keep the `colima ssh` transport and the
   re-assert-firewall-every-attach invariant rather than adding a fast-path gate
   or SSH multiplexing. Add/adjust the corresponding bullet(s) under
   "start-agent.sh key decisions" in `CLAUDE.md`. Touch `README.md` only if any
   user-facing behavior or env var changed (it should not).

## Notes

- No new external dependencies; this stays within colima/docker/iptables/ssh
  behavior already relied on. The one thing to confirm during implementation is
  that the batched heredoc handles the agent-net-absent case correctly (probe
  reports empty → create → re-probe), since that is the only read that can also
  trigger a write.
- Explicitly out of scope (declined by the user): SSH ControlMaster/connection
  multiplexing via `colima ssh-config`, and a warm fast-path gate that skips the
  iptables/tinyproxy reconfiguration wholesale. The firewall chain is still
  flushed and rebuilt on every attach.
- These three steps are independent and ship together; no phase boundary
  resolves an uncertainty that reshapes the others.
