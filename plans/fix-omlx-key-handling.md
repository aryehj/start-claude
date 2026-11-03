# Fix omlx key handling in start-agent.sh

## Status

- [x] Keep the inference proxy's omlx key when a later run lacks `OMLX_API_KEY` (Opus recommended)
- [x] Take the omlx key off the argv in the startup reachability probe
- [ ] Live check against omlx on the Mac (human gate: needs the macOS host, colima and a running omlx): `tests/verify-omlx-key-handling.sh`

## Context

Two findings from a `/code-review high` of the `local-api-restriction` branch, both in the inference-proxy work (ADR-050, `plans/implemented - restrict-agent-inference-endpoints.md`). Read ADR-050 first: the proxy holds the omlx key, so the container never does.

**1. A later run can drop the key from the proxy.** `generate_inference_proxy_conf` (`start-agent.sh` ~909) rebuilds the nginx config on *every* run from the current shell's `BACKEND` and `OMLX_API_KEY`:
- omlx: `auth='""'` unless `OMLX_API_KEY` is set (~914).
- The SHA-256 of the config is compared with the hash stored in the VM (~966, ~1035). The hash covers the key, so a missing key gives a different hash. The script then pushes the config and reloads nginx.

The failure: the user creates the container with `--backend=omlx` and the key exported. Later they run `start-agent.sh` or `start-agent.sh --reload-allowlist` from a terminal without `OMLX_API_KEY`. The proxy now sends `Authorization ""`, and every omlx request from the live session returns 401. Before ADR-050, the key lived in the container's create-time env and survived such runs.

A related case: running without `--backend=omlx` (the default is `ollama`, ~293) switches the proxy and the `CLAUDE_AGENT_IN` rule to Ollama's port. Decide whether that is intended, since backend is a per-run choice. At minimum, a run that doesn't name a backend shouldn't silently break an omlx session.

**2. The omlx probe puts the key on an argv.** At ~742, `OMLX_CURL_ARGS+=(-H "Authorization: Bearer $OMLX_API_KEY")` goes through `probe_retry vm_ssh curl …`. `vm_ssh` (~469) `printf %q`-joins its args into a script, and `vm_sh` (~480) pipes that script to `colima ssh -- bash` over stdin. So the key is not on the host's argv, but it is on `curl`'s argv inside the VM, where `ps` shows it. (The review said it was visible on the host too. It isn't: that part was wrong.) That contradicts the "never on an argv" rule that the nginx push (~960, stdin) and `write_tinyproxy_push` follow. The host-side discovery Python (~1279, ~1454) takes the key from the environment, not argv, which is fine.

## Goals

- A run without `OMLX_API_KEY` never downgrades a proxy config that currently holds a key. It either keeps the existing config or stops with a clear message, and it never prints the key.
- The key appears on no argv in the VM. It already appears on none on the host.
- Rotating the key with `OMLX_API_KEY=new start-agent.sh --backend=omlx` still triggers a push (the hash still covers the key).
- The warm-reattach SSH budget stays the same (ADR-045). Fold any new VM fact into the existing batch probe (`vm-probe.sh`), not a new `colima ssh`.

## Approach (suggested, not binding)

- **Finding 1.** Have the batch probe report whether the installed config holds a non-empty key. A grep for `Authorization "Bearer ` is enough; report only yes/no, never the value. If this run has no key and the installed config has one, skip the regeneration and push, and warn that the existing key is kept. The behavior for a backend switch without an explicit flag is yours to choose; record it in ADR-050 or a new ADR.
- **Finding 2.** The probe at ~742 only checks reachability. Either drop the auth header and treat any HTTP response, 401 included, as reachable (`curl -s -o /dev/null -w '%{http_code}'`, where `000` means unreachable), or have the VM-side script write the header to a 0600 temp file and pass `curl -H @file`. Plain `curl -H @-` won't work, because `vm_sh` already uses stdin for the script. Prefer dropping the header: it is simpler, and the probe exists to warn when omlx can't be reached, not to check the key.
- **Tests.** Add static checks to `tests/test_agent_sh.py`, the file for start-agent.sh invariants. For example: no `$OMLX_API_KEY` expansion inside any `vm_ssh`/`vm_sh`/`colima ssh` argument list. For the downgrade rule, extract the decision into a small function so it can be tested without a VM.
- Run `bash -n start-agent.sh` and `uv run --with pytest pytest tests/`. Don't use `sed -i` on the scripts: it drops the executable bit on this bind mount, and `tests/test_file_modes.py` will fail.

## Unknowns / To Verify

- Whether omlx answers an unauthenticated `GET /v1/models` with 401, or with something else, when a key is configured. This only matters for the drop-the-header option. Phase 2 made this moot: the probe takes any HTTP answer as reachable. The live check records the code.

## Live check (Phase 3)

One script covers Phases 1 and 2: `tests/verify-omlx-key-handling.sh`, run on the Mac. Its header lists the checks. It needs no key. Its precondition is a container already started with `OMLX_API_KEY=<key> start-agent.sh --backend=omlx`. It writes its log to `.verify/verify-omlx-key-handling.log`. The script leaves two checks out: a backend switch to Ollama and back, which would drop the key from a live session, and key rotation. Unit tests in `tests/test_agent_sh.py` cover both.

## Notes

- Live omlx tests must use `gemma-4-e4b-it-6bit` only. Larger models cause RAM problems on the Mac.
- Host-side checks go in a script under `tests/` that the user runs with one short command. Follow the pattern of `tests/test-agent-firewall.sh` and `tests/test-cross-vm-isolation.sh`.
