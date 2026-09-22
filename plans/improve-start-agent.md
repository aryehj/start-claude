# start-agent.sh: worthwhile improvement opportunities

## Status

- [ ] Batch 1: security and correctness, small diffs (A1, A3, A4, B1, B4, B6, B7, C3)
- [ ] Batch 2: container lifecycle (B2, B3, SearXNG pin + `--restart`)
- [ ] Batch 3: allowlist (B5 validation, then shared template)
- [ ] Batch 4: perf (C1, C2, C4)
- [ ] Decisions pending: A2 (DNS blackhole), B8 (skills source)
<!-- mark [x] as items land -->

## Implementation ground rules

- All work on this branch (`improve-start-agent`), never on `main`, so it can be tested and
  reverted as a unit.
- One commit per substantive change (one lettered item, or one tightly coupled pair), each
  with passing static tests. No `Co-Authored-By` lines (see CLAUDE.md).
- Keep the exact-string shapes `tests/test_agent_sh.py` anchors on (listed under Verification).
- Update ADR.md / CLAUDE.md / README.md in the same commit as the behaviour they describe.

## Context

`start-agent.sh` (1708 lines) is the Colima-backed agent launcher. A full read of the script,
its tests (`tests/test_agent_sh.py`, `tests/test-agent-firewall.sh`, `tests/test-cross-vm-isolation.sh`),
the relevant ADRs, and the existing plans surfaced the findings below.

**Already scoped elsewhere, not repeated here:** the readability refactor in
`plans/simplify-start-agent-full-pass.md` (extract the 290-line allowlist heredoc to
`templates/agent-allowlist.txt`, unify the two duplicated `discover_models()` Python blocks,
extract the firewall heredoc, compact the arg parser, add a `main` driver). Those remain the
right readability moves. Two of those items get a security/perf angle here (allowlist template
sharing, firewall atomicity) that the existing plan does not cover.

**Deliberately removed before, do not re-add:** the SearXNG `outgoing.proxies` drift check and
the 30-day image-staleness warning (`plans/_paused - simplify-start-agent.md`, steps 1 and 4).

Findings are ranked by value within each group. Each has a concrete fix. Line numbers refer to
the current `main` (`1d1da8f`).

---

## A. Security

### A1. Firewall re-apply is not atomic (contradicts CLAUDE.md and ADR-010)
`start-agent.sh:1065-1094`: `iptables -F CLAUDE_AGENT` followed by six separate `iptables -A`
calls. Between the flush and the final `REJECT`, the chain is empty, so any bridge egress in
that window falls through DOCKER-USER and is **allowed**. This runs on every attach and every
`--reload-allowlist`. CLAUDE.md line 84 and ADR-010 (`ADR.md:362`) both claim atomicity.

**Fix:** render the chain as an `iptables-restore --noflush` stanza and apply it in one commit:
```
*filter
:CLAUDE_AGENT - [0:0]
-A CLAUDE_AGENT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
... (same rules as today) ...
-A CLAUDE_AGENT -j REJECT --reject-with icmp-admin-prohibited
COMMIT
```
With `--noflush`, declaring `:CLAUDE_AGENT` flushes and repopulates only that chain, atomically.
Keep the idempotent `-C || -I` DOCKER-USER jump rules as separate commands (they are never
removed, so they carry no window). Update the ADR-010 wording. Dovetails with Phase 3 of the
existing simplify plan (firewall template).

### A2. Container DNS is an uncontrolled channel on the default (search-enabled) path
When search is enabled the container sits on `claude-agent-net`, so its resolver is Docker's
embedded DNS at 127.0.0.11. Embedded DNS forwards external names to the VM's upstream resolver
from the daemon side, which never traverses FORWARD, so the CLAUDE_AGENT chain cannot see it.
Result: arbitrary external hostnames resolve from inside the container even though all TCP/UDP
egress is rejected. That is both an information leak and a low-bandwidth covert channel.
Conversely, the two `--dport 53 -d $BRIDGE_IP` RETURN rules (`:1086-1087`) almost certainly
match nothing on either network path and give a false impression that DNS is governed here.

**Verify first** (inside the container): `getent hosts example.com` and `nslookup example.com`.
If they resolve, the channel is real.

**Candidate fix:** pass `--dns <blackhole>` on `docker run` (embedded DNS forwards external
queries to the container's configured upstream; an RFC 5737 address such as `192.0.2.1` makes
them time out). Nothing in the proxy path needs local resolution: curl, git, npm, uv, pip and
Node with `NODE_USE_ENV_PROXY=1` all hand the hostname to tinyproxy via CONNECT. `searxng`
still resolves because container names are answered by embedded DNS directly. Must be checked
against `tests/test-agent-firewall.sh` (all six cases) plus a Claude Code login round-trip,
because a tool that resolves before proxying would now hang for the timeout instead of failing
fast. If the trade-off is rejected, document the channel as a known limitation in ADR-010 and
delete the two dead `:53` rules.

### A3. `docker context use` permanently rewrites the user's host Docker config
`start-agent.sh:774`: `docker context use colima-claude-agent` writes `currentContext` into
`~/.docker/config.json`. After running the script, every plain `docker` command on the Mac
targets the agent VM, and `research.py:858` does the same for its own profile, so the two
scripts silently fight over the user's global context.

**Fix:** `export DOCKER_CONTEXT="colima-$COLIMA_PROFILE"` instead. The Docker CLI honours it
for every `docker` call in the script, and the final `exec docker run|exec` inherits it. No
persistent state is touched. Apply the same change to `research.py` in a follow-up.

### A4. Secret passed on the command line
`start-agent.sh:1575`: `-e "OMLX_API_KEY=$OMLX_API_KEY"` puts the key into the `docker` argv
(visible in `ps` on the host for the life of the exec). Docker supports `-e OMLX_API_KEY` with
no value, which reads it from the client environment. Same for the API keys in B1 below.

---

## B. Functionality and correctness

### B1. Cloud API keys are documented as forwarded but are not
CLAUDE.md line 91, README line 484 and ADR-036 all say `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
are forwarded from the host so Pi can use cloud providers. `grep` finds neither name anywhere
in `start-agent.sh`. Pi inside the container cannot see them.

**Fix:** in `DOCKER_ENV_ARGS` (`:1546`), append `-e ANTHROPIC_API_KEY` / `-e OPENAI_API_KEY`
(value-less passthrough, per A4) only when each is set on the host. Both `docker run` and
`docker exec` already consume the same array, so reattach picks them up too.

### B2. Toggling `--disable-search` does not update an existing container's network
The reattach check (`:1651-1668`) only compares the projects mount. A container created with
search disabled sits on the default bridge; if the user later runs without the flag, the script
writes the searxng MCP block into `opencode.json` and starts SearXNG on `claude-agent-net`, but
the agent container is not on that network, so `searxng` never resolves and websearch fails
silently. The reverse leaves a stale network attachment.

**Fix:** before `attach_existing`, inspect `.NetworkSettings.Networks` for `claude-agent-net`
membership; `docker network connect` / `disconnect` to match `$LOCAL_SEARCH_ENABLED` (both work
on stopped containers). Cheaper than recreating.

### B3. Broken containers are not self-healed (research.py already does this, ADR-041)
`attach_existing` (`:1644`) runs `docker start ... || true` then `exec docker exec`, so a
container that fails to start (the `RWLayer nil` case ADR-041 documents) dies with Docker's
"container is not running" instead of being recreated. `docker start "$SEARXNG_CONTAINER" || true`
(`:1135`) has the same problem and additionally hides the failure entirely (search just stops
working). Neither container has `--restart unless-stopped`.

**Fix:** port the `start_or_recreate` semantics from `research.py:697`: after `docker start`,
check `.State.Running`; on failure print the captured stderr, `docker rm -f`, and fall through to
the fresh-create path (for the agent container this means skipping `attach_existing` and
continuing to the `docker run` below). Add `--restart unless-stopped` to both `docker run`
calls. Keep the `IMAGE_TAG` and `SEARXNG_CONTAINER` `docker run` blocks as single
backslash-continued commands so `tests/test_agent_sh.py` keeps finding them.

### B4. VM memory-mismatch warning compares a substring of a byte count
`start-agent.sh:766`: `"$running_mem" != *"${CLAUDE_AGENT_MEMORY_GB}"*`. `colima list --json`
reports memory in bytes (e.g. `8589934592`), so the check asks whether the digit "8" appears in
that string. Requesting 8 GiB against a 12 GiB VM (`12884901888`) never warns; requesting 4 GiB
against a 6 GiB VM (`6442450944`) never warns. `research.py:749` has the identical bug.

**Fix:** parse once with Python, convert `memory // 2**30`, compare integers. Also collapse the
two `python3 -c` invocations and the second `colima list --json` call (see C2).

### B5. Allowlist entries are never validated
`generate_filter_file` (`:651`) regex-escapes whatever is on the line. A pasted `https://foo.com`
or `foo.com/path` becomes a regex that can never match a hostname, so the entry silently does
nothing and the user sees an unexplained 403. `re.escape` also emits `\-` for hyphenated domains
(`rust-lang.org`), which is undefined in POSIX ERE (glibc tolerates it; it is not guaranteed).

**Fix:** in the Python generator, accept only `^[A-Za-z0-9.-]+$` (lowercased), print
`warning: allowlist.txt:N: '...' is not a bare domain, skipped` to stderr for anything else,
and escape only `.` by hand. Run the same validation on `--reload-allowlist` so the entry count
it prints reflects live entries. Consider sharing the template with `start-claude.sh`: the
embedded list and `templates/sandbox-allowlist.txt` are currently byte-identical 227-entry
copies maintained by hand (Phase 1 of the simplify plan should point both scripts at one file).

### B6. Silent death when the VM probe fails
`start-agent.sh:847`: `PROBE_OUTPUT=$(colima ssh ... 2>/dev/null | tr -d '\r')`. Under
`set -eo pipefail` a failed `colima ssh` aborts the script with no message at all (stderr is
discarded). Add an explicit `|| { echo "error: could not run probe in VM"; exit 1; }` and let
ssh's stderr through on failure.

### B7. Flag hygiene
- `--rebuild --reload-allowlist` and `--reset-container --reload-allowlist` silently drop the
  first flag (`:734`, `:781`, `:787`). Reject the combination like the existing
  `--reset-container`/`--rebuild` check at `:171`.
- `--reseed-allowlist` overwrites the user's curated file with no backup. Write
  `allowlist.txt.bak` first and say so.
- `python3` is a hard host dependency (four heredocs) but is not in preflight (`:333-348`);
  a Mac without the Xcode CLT fails halfway through after the VM has already started. Add it.
- README "Usage" block omits `--reseed-allowlist` and `--init-sandbox`; the tests table still
  describes `test_agent_sh.py` as a single host-port check (it has 33 tests) and omits
  `test_dockerfile.py`. `usage()` omits `CLAUDE_SKILLS_ARCHIVE_URL`.

### B8. Skills sync fetches GitHub even though the repo checkout is the source (decision needed)
`sync_skills` (`:1516`) downloads the upstream tarball on every fresh container, while the
sibling `seed_agent_skills` copies from the local `skills-agents/`. Fresh containers therefore
need GitHub reachability and can seed a different skills version than the checkout that seeded
everything else. ADR-005 chose upstream deliberately (stale local checkouts). Recommendation:
local `$SCRIPT_DIR/skills` first, upstream only when `CLAUDE_SKILLS_ARCHIVE_URL` is set or the
local dir is missing; amend ADR-005. This changes documented behaviour, so it is the user's call.

---

## C. Performance (warm reattach and `--reload-allowlist`)

ADR-045 already cut the warm path to roughly three SSH handshakes. What is left:

### C1. `--reload-allowlist` still costs 9 to 11 SSH handshakes
Mount check (1) + probe (1) + `vm_put_file` x2 files at 2 each (4) + hash (1) + reload (1) +
firewall (1) + background inference probe (1 to 3). Fold the config push into one `vm_sh`
script that carries both files and the hash as base64 payloads and runs the reload, and skip
starting the inference probe at all when `RELOAD_ALLOWLIST` is set (it is killed unread at
`:1104` today). Saves about five handshakes on the path that is advertised as "fast".

### C2. Small warm-path wins
- Two `colima list --json` calls (`:667`, `:759`) and two `python3` parses of the same JSON.
  Call once, parse once.
- The virtiofs mount check (`:752`) is its own SSH call before the batch probe; when the VM is
  already running, include `mount -t virtiofs` in `vm-probe.sh` and only re-probe after a
  restart. Saves one handshake on every warm attach.
- The two host-side `discover_models()` HTTP probes (OpenCode and Pi) plus the in-VM curl probe
  make three round-trips to the inference server per run. Phase 2 of the simplify plan already
  targets the first two.

### C3. Orphaned background probe on error exits
The EXIT trap (`:794`) removes `$TMP_WORK` but does not kill `$PROBE_PID`, so any `set -e`
failure after `:922` leaves a `colima ssh curl` running detached. Add the kill to the trap.

### C4. `docker run --memory` equals the whole VM
`:1693` limits the container to exactly the VM's RAM, which is no limit at all while implying
one. Either drop it or leave headroom (VM size minus 1 GiB) so sshd and tinyproxy survive an
in-container memory blow-up.

---

## D. Readability (beyond the existing simplify plan)

- Pin `docker.io/searxng/searxng` to a tag or digest (`:1132`, also `research.py:1051`). It is
  the only unpinned runtime image; ADR-047 already sets the precedent for pinning security-
  relevant dependencies.
- `DOCKERFILE_PATH` (`:317`) and `GLOBAL_CLAUDEMD_TEMPLATE` (`:1154`) recompute `SCRIPT_DIR`.
- ADR-010 and CLAUDE.md line 84 need the atomicity wording corrected once A1 lands.

---

## Recommended implementation order

1. **Batch 1, security and correctness, small diffs:** A1, A3, A4, B1, B4, B6, B7, C3.
   Each is under 30 lines; none changes user-visible behaviour except to fix it.
2. **Batch 2, container lifecycle:** B2, B3, SearXNG pin and `--restart`.
3. **Batch 3, allowlist:** B5 validation, then Phase 1 of the simplify plan with the template
   shared by both scripts.
4. **Batch 4, perf:** C1, C2, C4.
5. **Decisions needed from the user before touching:** A2 (DNS blackhole, after verification),
   B8 (skills source, amends ADR-005).

## Verification

- `python3 -m pytest tests/test_agent_sh.py tests/test_dockerfile.py tests/test_sandbox_allowlist.py`
  after every batch. The static tests anchor on exact strings (`# Fresh container:`,
  `exec docker run`, the single-command `docker run` blocks, the `-*)` unknown-option arm);
  keep those shapes.
- `bash -n start-agent.sh` and `shellcheck start-agent.sh` (shellcheck is not in the repo's
  tooling today; adding it to the pytest run is cheap and would have caught B6).
- Live smoke, on the Mac host: cold sandbox run, warm reattach, `--reload-allowlist`,
  `--rebuild`, `--disable-search` toggle on an existing container (B2), then
  `tests/test-agent-firewall.sh` inside the container and `tests/test-cross-vm-isolation.sh`
  from the host.
- A1 specifically: `colima ssh -p claude-agent -- sudo iptables -S CLAUDE_AGENT` before and
  after must be byte-identical.
- A2 specifically: `docker exec claude-agent getent hosts example.com` must fail after the
  change, while `getent hosts searxng` and a Claude Code login still succeed.
- B4: run with `--memory` different from the running VM and confirm the warning fires.
