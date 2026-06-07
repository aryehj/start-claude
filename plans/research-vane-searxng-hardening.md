# research.py — harden Vane/SearXNG bring-up against silent failures

## Status

- [ ] Phase 1: self-healing container start + `--restart unless-stopped`
- [ ] Phase 2: script-controlled Vane SearXNG URL (config.json authority)
- [ ] Phase 3: unit tests, ADR, docs + banner

## Context

A debugging session surfaced three distinct failures that all presented as "Vane can't reach SearXNG," none of which `research.py` reported. All three trace to the same root weakness: **`research.py` trusts the existing container/config state and `docker start` blindly, swallowing errors and printing "Research environment ready" regardless.**

1. **Dead SearXNG container, silently.** `research-searxng` was `Exited (137)` with `err=RWLayer of container ... is unexpectedly nil` — a dangling Docker storage layer left by an unclean Colima VM restart (the repo's own recent commit `4580176 "fix: only restart VM when sandbox mount is actually missing"` is the kind of VM churn that produces it). `ensure_searxng_container` (`research.py:887-891`) runs `docker start` with **no `check=True`** and unconditionally prints "started (existing container)", so a container that cannot start is reported as up. The only repair for `RWLayer nil` is `docker rm -f` + recreate; `docker start` can never fix it.

2. **Stale `docker run` config on existing containers.** `ensure_vane_container` / `ensure_searxng_container` only `docker run` when the container is *absent*; otherwise they `docker start`. So `docker run`-time settings never refresh on an existing container — this is exactly why the omlx fix (commit `0d0ddf9`, changing `--add-host host.docker.internal:host-gateway` → `:{config.host_ip}` at `research.py:910`) required a manual `docker rm -f research-vane` to take effect. The same staleness strands any proxy/env/network change.

3. **Wrong SearXNG URL, unvalidated.** Vane (`vane:slim-latest`, a Perplexica fork) **ignores the `SEARXNG_API_URL` env var** set at `research.py:912` and instead reads `search.searxngURL` from its persisted `~/.research/vane-data/data/config.json`. That field is user-entered via the UI and was set to `http://host.docker.internal:8080` — the macOS host, where SearXNG's port is *not* published (it is a sibling container on `research-net`, reachable only as `http://research-searxng:8080`). Confirmed this session: `docker exec research-vane node -e "fetch('http://research-searxng:8080/healthz')"` → `REACHABLE 200`, while the configured URL pointed at the host. The `SEARXNG_API_URL` env var gives false confidence (the ADR-014 "name the silent failure mode" anti-pattern).

This plan makes the bring-up self-healing for (1)/(2) and script-authoritative for (3). It does **not** change the VM, firewall, denylist, or network topology — all of those checked out correct during diagnosis (proxy IP `172.17.0.1:8888` matched the bridge gateway; Vane env had correct `NO_PROXY`/proxies; both containers on `research-net`).

## Goals

- A crashed or storage-corrupted `research-searxng`/`research-vane` is **auto-recreated** on the next `research.py` run instead of silently staying down. A `docker start` that fails (or yields a non-running container) triggers `docker rm -f` + a fresh `docker run`.
- Both containers get `--restart unless-stopped` so transient crashes and VM reboots self-recover without re-running the script.
- `docker run`-time fixes (e.g. the omlx `--add-host`) reach existing installs without a manual `docker rm` — recreation happens automatically when a start fails; a follow-on (optional) is recreate-on-config-drift.
- Vane's SearXNG URL is **controlled by `research.py`**, not by what the user types in the UI: on every bring-up, if `config.json` exists and `search.searxngURL != http://research-searxng:8080`, it is corrected (idempotently, preserving all other fields) and Vane is restarted to re-read it.
- The misleading `SEARXNG_API_URL` env var is removed (it is a no-op that implies the URL is wired when it isn't).
- Pure helpers are unit-tested in `tests/test_research.py`; the decision is recorded as an ADR.
- No regressions to `--rebuild`, `--reset`/teardown, or the denylist fast-path.

## Unknowns / To Verify

1. **Vane config schema stability.** Confirm `search.searxngURL` is the authoritative field across `vane:slim-latest` builds and that the env var `SEARXNG_API_URL` is genuinely ignored. Strong evidence this session (config.json value overrode the env), but pin it before deleting the env var — keep the env var if a Vane version is found that consumes it as a fallback.
2. **Pre-seeding a partial config.json.** On a brand-new install, `config.json` does not exist until the user completes UI setup (it carries `version`, `setupComplete`, `modelProviders`, etc.). Verify whether writing a minimal `{"search":{"searxngURL":"..."}}` *before* first launch is merged by Vane or rejected/overwritten. If safe, pre-seed so the UI shows the correct URL pre-filled; if not, restrict to **correct-only-if-exists** (lower risk, the chosen default below).
3. **`docker start` failure signal for `RWLayer nil`.** Verify the dangling-layer case returns a **non-zero exit** from `docker start` (so a returncode check suffices). If instead it can return 0 and the container exits immediately (crash-on-boot), the self-heal must also inspect `{{.State.Running}}` after a short settle. Test against the actual reproduced failure (`docker start research-searxng` on a layer-corrupted container).
4. **`--restart unless-stopped` interaction with lifecycle.** Confirm it does not interfere with `rebuild_teardown` (`research.py:967-986`, which already `docker rm -f`s) or leave containers auto-restarting after the user intends them stopped. `unless-stopped` (not `always`) is chosen specifically so an explicit `docker stop` sticks.
5. **Restart-to-apply ordering.** Patching `config.json` only takes effect when Vane re-reads it at startup. Verify the chosen wiring (correct config → then start/recreate, or restart-if-changed-and-running) actually reloads it; a no-op `docker start` on an already-`Up` container will not.
6. **Config path.** `~/.research/vane-data/data/config.json` (i.e. `paths.vane_data_dir / "data" / "config.json"`, since the volume mount is `{vane_data_dir}:/home/vane/data` at `research.py:916`). Confirmed this session; encode as a `Paths` property.

---

## Phase 1: self-healing container start + `--restart unless-stopped`

Make `ensure_searxng_container` and `ensure_vane_container` recover automatically from a start failure, and survive transient crashes via a restart policy. Highest-value phase — it directly fixes the dead-container class (failure #1) and the stale-run-config class (failure #2). Independent of Phase 2.

### Steps

1. Add a subprocess wrapper `docker_container_running(name: str) -> bool` near `docker_container_exists` (`research.py:632-637`): `docker inspect -f '{{.State.Running}}' <name>` → `True` only on `"true"`. Used to distinguish "started" from "started then crashed" (Unknown #3).
2. Extract a shared helper `start_or_recreate(name: str, create: Callable[[], None]) -> bool` (returns `True` if newly created):
   - If `docker_container_exists(name)`: run `docker start <name>` with `check=False`, capturing output. If returncode == 0 **and** `docker_container_running(name)`, print "started (existing container)" and return `False`.
   - On failure (non-zero, or not running): print a `warning:` line including the captured stderr (so `RWLayer ... unexpectedly nil` is *visible*, not swallowed), then `docker rm -f <name>` and fall through.
   - Call `create()` (the fresh `docker run`) and return `True`.
3. Rewrite `ensure_searxng_container` (`research.py:869-892`) to delegate to `start_or_recreate(CONTAINER_SEARXNG, _create)`, where `_create` is the existing `docker run` block. Add `"--restart", "unless-stopped"` to that `docker run`.
4. Rewrite `ensure_vane_container` (`research.py:895-929`) the same way: `start_or_recreate(CONTAINER_VANE, _create)`, add `"--restart", "unless-stopped"` to its `docker run`.
5. Keep the fresh-`docker run` paths `check=True` (a brand-new run that fails is still a hard error — but now it's the *only* hard-failing path, after the self-heal attempt).

### Files

- `research.py` (new `docker_container_running`, new `start_or_recreate`, rewritten `ensure_searxng_container` + `ensure_vane_container`)

### Testing

- `uv run pytest tests/test_research.py` — no pure helpers change here, but confirm nothing regresses.
- Manual repro on the host: reproduce a dead container (`docker rm -f research-searxng` mid-run, or stop the VM uncleanly), then `./research.py` and confirm the warning prints and the container is recreated `Up`.
- Manual: kill `research-searxng` (`docker kill`) and confirm `--restart unless-stopped` brings it back without re-running the script; then `docker stop` it and confirm it *stays* stopped.

## Phase 2: script-controlled Vane SearXNG URL

Take the SearXNG URL out of the user's hands. Fixes failure #3 and removes the misleading env var.

### Steps

1. Add a `Paths` property `vane_config_file` (`research.py:62-92`): `return self.vane_data_dir / "data" / "config.json"`.
2. Add a **pure helper** `patch_vane_searxng_url(config_text: str, desired: str) -> Optional[str]`:
   - Parse JSON; read `config["search"]["searxngURL"]` (tolerate missing `search` key).
   - If it already equals `desired`, return `None` (no change — idempotent, mirrors `mutate_temperature` in ADR-030 and the settings.yml drift check at `research.py:854-866`).
   - Otherwise set it and return `json.dumps(config, indent=2)`.
   - On `json.JSONDecodeError`, return `None` and let the caller warn (do not clobber a file we can't parse).
3. Add `ensure_vane_searxng_url(paths: Paths) -> bool` (returns `True` if the file changed):
   - Desired value = `f"http://{CONTAINER_SEARXNG}:8080"`.
   - If `paths.vane_config_file` does not exist, return `False` (per Unknown #2, correct-only-if-exists is the safe default; revisit pre-seeding only if verified safe).
   - Read → `patch_vane_searxng_url` → if a new body is returned, write it and print `==> Vane SearXNG URL corrected: <old> → <desired>`; return whether changed. On parse failure, print a `warning:` pointing the user at Settings → SearXNG URL.
4. Wire into `main()` (`research.py:1068-1074`): call `ensure_vane_searxng_url(paths)` and capture the changed flag. After `ensure_vane_container`, if the config changed **and** the container was not newly created (a fresh create already reads the file), `docker restart research-vane` so it re-reads config.json (Unknown #5). Simplest robust ordering: correct config *before* `ensure_vane_container`, and have the start path restart when the changed flag is set.
5. Remove the `-e SEARXNG_API_URL=...` line from `ensure_vane_container` (`research.py:912`) — Vane ignores it (Unknown #1). Keep `NO_PROXY=research-searxng,...` (still required so the SearXNG and host calls bypass Squid, per ADR-029).
6. Update the closing banner (`research.py:1076-1085`) to drop any implication the user must configure SearXNG, and state that SearXNG is auto-wired to `http://research-searxng:8080`.

### Files

- `research.py` (new `Paths.vane_config_file`, `patch_vane_searxng_url`, `ensure_vane_searxng_url`; `main()` wiring; env-var removal; banner)

### Testing

- Unit (Phase 3 adds them): `patch_vane_searxng_url` on no-change / change / missing-`search`-key / malformed-JSON inputs.
- Manual: set `search.searxngURL` to `http://host.docker.internal:8080` in `~/.research/vane-data/data/config.json`, run `./research.py`, confirm the correction prints, Vane restarts, and a UI search succeeds. Then re-run and confirm it's a silent no-op (idempotent).

## Phase 3: unit tests, ADR, docs + banner

Lock in the pure-helper behavior and record the decision.

### Steps

1. Add tests to `tests/test_research.py` for `patch_vane_searxng_url`:
   - already-correct → returns `None`.
   - wrong host (`host.docker.internal:8080`) → returns body with `research-searxng:8080`, all other keys preserved (assert `modelProviders` untouched).
   - missing `search` key → adds it (or returns `None` per chosen semantics — match the implementation).
   - malformed JSON → returns `None`, no exception.
2. (If Unknown #3 forces a `State.Running` check) keep the docker-touching helpers out of unit tests — they require a daemon; cover them via the manual repro steps instead, consistent with the existing test split (pure helpers only).
3. Add **ADR-041** to `ADR.md`: "research.py controls Vane's SearXNG URL and self-heals container start." Capture: the three failure modes, why `docker start` was insufficient (RWLayer nil, stale run-config), why config.json is authoritative over `SEARXNG_API_URL`, the `unless-stopped` (not `always`) choice, and the correct-only-if-exists default for config.json.
4. Update `CLAUDE.md` "research.py key decisions" with one bullet for self-healing start and one for script-controlled SearXNG URL.
5. Update `README.md` if it documents the "configure LLM at localhost:3000" first-run step — clarify SearXNG needs no manual configuration.

### Files

- `tests/test_research.py`
- `ADR.md`
- `CLAUDE.md`
- `README.md`

### Testing

- `uv run pytest tests/test_research.py` — all pass, including the new cases.

## Notes

- **Scope discipline.** Do not touch the VM, `RESEARCH` iptables chain, Squid, denylist composition, or `research-net` — all verified healthy during diagnosis. This plan is confined to container lifecycle and Vane config.
- **Phase independence.** Phase 1 and Phase 2 fix different failures and can land/ship separately; Phase 1 first, since it's the broader safety net and unblocks the stale-run-config class (including making future `docker run` changes like the omlx fix self-applying).
- **ADR-018 patterns.** Keep the JSON-patching logic as a pure string→string helper (`patch_vane_searxng_url`) separate from the file/subprocess wrappers, so it's unit-testable without a Docker daemon — same split as `render_searxng_settings` / `denylist_to_squid_acl`.
- **Don't over-engineer the self-heal.** A returncode check on `docker start` plus a single `State.Running` confirmation is enough; avoid retry loops or health-poll timeouts unless Unknown #3 shows crash-on-boot is common. The `RWLayer nil` case is a hard non-zero start failure.
- **`config.json` is user state.** Never rewrite fields other than `search.searxngURL`; never delete the file. If it can't be parsed, warn and leave it for the user — matching how `denylist-overrides.txt` and UI-entered LLM config are treated as user-owned.
