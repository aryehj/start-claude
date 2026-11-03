# Safe version updates: SearXNG, OpenCode/Pi, sandbox-runtime

## Status

- [ ] Phase 1: Refresh + rollback path for claude-agent, and a SearXNG bump
- [ ] Phase 2: Bump sandbox-runtime 0.0.73 → current, re-tested under Apple Containers (Opus recommended)
- [ ] Phase 3: Launch-time staleness warnings

## Context

There are three version pins in scope:

| Component | Where | How it is pinned today | As of 2026-11-03 |
|---|---|---|---|
| SearXNG | `start-agent.sh:378-381` (`SEARXNG_IMAGE`) | Digest taken 2026-10-05 (ADR-048) | Upstream has no versioned releases; `latest` is rebuilt from master several times a week |
| OpenCode + Pi | `dockerfiles/claude-agent.Dockerfile:52` | `@latest`, frozen by the cached image. `autoupdate: false` is forced at `start-agent.sh:1476-1478` | Image has opencode-ai 1.19.5 and pi 1.5.0. Both are the current npm `latest` |
| sandbox-runtime | `start-claude.sh:283-289` | Exact pin `0.0.73` (ADR-047) | npm `latest` is 0.0.81 (8 releases newer) |

The user chose to **keep `@latest`** for OpenCode/Pi rather than pin them in the Dockerfile. So "updating" them means forcing a fresh npm layer on purpose. `--rebuild` may not do that today. It runs `docker image rm` (`start-agent.sh:583-588`) and then `docker build` without `--no-cache` (`:1256`), so the build cache can still return the old npm layer. `--rebuild` also asks whether to delete the VM and recreates everything, which is heavier than this needs.

The following are **out of scope**, by the user's choice: research.py's unpinned SearXNG and Vane, the `mcp[cli]<2` ceiling, Debian bookworm → trixie, and Claude Code / Node / npm / uv.

Existing live-check conventions to follow: host-driven scripts in `tests/` that write logs to `.verify/` (e.g. `tests/verify-omlx-key-handling.sh`). Live inference tests use omlx `gemma-4-e4b-it-6bit` only, because larger models cause RAM problems on the host. Host commands are wrapped in a script in the repo and run with one short line, not pasted as multi-line blocks.

## Goals

- `start-agent.sh` gets one opt-in command that refreshes OpenCode/Pi. It tags the current image as a rollback point, rebuilds only from the npm layer onward, and recreates the container. A second command restores the previous image.
- A SearXNG bump is scripted: resolve the current upstream digest, rewrite `SEARXNG_IMAGE` and its date comment, reset, and verify. Rollback is a git revert of that one line plus `--reset-container`.
- sandbox-runtime moves to the current release only after a live `start-claude.sh --rebuild` shows that sandboxed Bash still works.
- Each change is accepted only after three live checks pass (see Phase 1): SearXNG returns JSON results from several engines through tinyproxy; OpenCode loads the injected `opencode.json` without config errors; and an end-to-end `opencode run` against omlx/gemma-4-e4b-it-6bit makes a SearXNG MCP tool call.
- On launch, each script prints one line when a component it owns has a release more than N days newer than the one in use (default 30, overridable by env var). The line names the command that refreshes it. The check is throttled and runs in the background, so a warm reattach stays fast (ADR-045).

## Approach

**Use a cache-bust build-arg, not `--no-cache`.** Declare an `ARG` immediately before the npm install at Dockerfile:52 and reference it in that `RUN`. Passing a fresh value (e.g. a timestamp) reruns that layer and every later one. The apt, Node, Claude Code and uv layers stay cached, so a refresh does not also upgrade the OS packages. The arg is needed whether or not unknown (1) below finds that the cache survives `image rm`, so it does not depend on that result. Still record the finding: it decides whether the docs should say that `--rebuild` alone does *not* refresh the tools.

**Version checks run on the host, with lag thresholds, not "anything newer exists".** For SearXNG, "a newer digest exists" is true almost every day, and OpenCode also ships every few days. So each check compares publish dates: the newest upstream release against the one in use. It warns only when the gap is more than N days. The macOS host has unrestricted egress, so the checks call npm and Docker Hub directly. Nothing goes through the agent's proxy, and the container allowlist does not change. Results are cached in a host-side state file with a timestamp, at most one network check per day. Each check has a short timeout and fails silently, in the same background-job style as the inference probe. "In use" comes from the source pin for sandbox-runtime and SearXNG. For OpenCode/Pi it is the versions recorded when the image is built (Phase 1, step 3), because the source says `@latest`.

## Phase 1: Refresh + rollback for claude-agent, and a SearXNG bump

### Steps

1. **Resolve unknown (1):** on the host, run `--rebuild` and keep the VM. Check whether the npm `RUN` step shows as `CACHED` in the build output. Record the result in the ADR (step 7).
2. Add the cache-bust `ARG` before Dockerfile:52, as described in Approach. Update `tests/test_dockerfile.py`: `@latest` stays, and the ARG must sit before the npm line and be referenced by it.
3. Record the installed tool versions per image. After a successful build, run `opencode --version` and `pi --version` once in a throwaway container and save the output where Phase 3 can read it cheaply on every launch. Options are a host-side file under `.sandbox_config/` or a file baked into the image and read back with `docker image inspect`. Pick whichever avoids a container start on a warm reattach.
4. Add a `start-agent.sh` refresh flag, named in the style of the existing flags (e.g. `--refresh-tools`). It must:
   - tag `claude-agent:latest` as `claude-agent:previous`, replacing any older `previous`;
   - build with a fresh cache-bust value;
   - recreate the container the way `--reset-container` does;
   - print the old → new versions.

   Make it mutually exclusive with `--rebuild` and `--reset-container`, and validate that in the same place as the existing exclusions (`start-agent.sh:182-190`). Add a restore flag (e.g. `--restore-previous-image`) that retags `previous` → `latest` and resets the container. It should fail clearly if `previous` does not exist.
5. Add `scripts/bump-searxng.sh`, or put it next to the other host scripts if there is a better home. On the host it runs `colima ssh -p claude-agent` to pull `docker.io/searxng/searxng:latest`, reads `RepoDigests`, and rewrites `SEARXNG_IMAGE` plus the "known good on <date>" comment at `start-agent.sh:378-381`. It must keep the format that `tests/test_agent_sh.py::test_searxng_image_pinned_by_digest` enforces. It does not commit. The commit happens after verification.
6. Add `tests/verify-tool-refresh.sh`, host-driven, logging to `.verify/`, which runs the three acceptance checks from Goals inside the running agent container:
   - **SearXNG:** `curl --noproxy searxng 'http://searxng:8080/search?q=…&format=json'` from the agent container. Pass if there are non-empty `results` from at least two distinct engines, and no unexpected entries in `unresponsive_engines`. An engine that newly needs a host missing from the allowlist shows up here.
   - **OpenCode config:** the injected config loads without errors. Find the right subcommand under `opencode debug` (see Unknowns).
   - **End-to-end:** `opencode run` against the omlx provider with `gemma-4-e4b-it-6bit`, using a prompt that forces the `searxng` MCP tool. Pass if the session output shows the tool call and a non-empty answer. Also check that `pi --version` runs.
7. Run both procedures live: bump SearXNG, then run `--refresh-tools`. OpenCode/Pi are already current, so the refresh mainly proves the mechanism. Run the verify script after each. Write an ADR extending ADR-048 (refresh/restore flags, build-arg cache bust, SearXNG bump script, the build-cache finding) and update the README flag reference and CLAUDE.md's start-agent.sh bullets.

## Phase 2: Bump sandbox-runtime (start-claude.sh)

A diff of the published tarballs (0.0.73 vs 0.0.81, `dist/sandbox/linux-sandbox-utils.js`) shows that the bwrap invocation changed a lot:

- It always passes `--unshare-user` plus `--cap-drop ALL`. When the caller is uid 0 it adds `--cap-add CAP_SETFCAP`.
- It refuses to sandbox when it runs as uid 0 without `CAP_SETFCAP` in its bounding set (`CAP_SETFCAP_MISSING_MESSAGE`).
- It probes `bwrap --unshare-user --dev-bind / / true`.
- It may pass `--disable-userns`. That only happens when weaker mode is off, so it does not apply here.
- With `enableWeakerNestedSandbox`, it still uses `--bind /proc /proc`, which is the branch ADR-047 depends on.

The container runs as root under Apple Containers with `--cap-add SYS_ADMIN --cap-add NET_ADMIN` (ADR-043). Whether `CAP_SETFCAP` is in that bounding set is the deciding question.

### Steps

1. Inside a running `start-claude.sh` container, check `CapBnd` in `/proc/self/status` for bit 31 (`CAP_SETFCAP`). If it is missing, add `--cap-add SETFCAP` next to the existing `--cap-add` flags and extend ADR-043's rationale. The cap only has effect inside the per-project microVM, which is the same reasoning ADR-043 already uses.
2. Bump the pin at `start-claude.sh:289` to the current npm `latest`. Look it up when you implement; it was 0.0.81 on 2026-11-03. Update the comment at `:283-288` with what changed. The user runs `start-claude.sh --rebuild` on the host.
3. Live checks in the rebuilt container:
   - a sandboxed Bash command runs;
   - a write outside the allowed paths is denied;
   - a non-allowlisted domain is blocked;
   - an allowlisted domain is reachable;
   - `/` cannot be remounted rw (the ADR-047 hardening still holds).

   Script these as a host-runnable check if they can be done non-interactively (e.g. `claude -p` with a fixed prompt). Otherwise give the user a short checklist. If the sandbox cannot start, capture the sandbox-runtime debug output, revert the pin, and stop.
4. Record the result in ADR-047, as an amendment or a new ADR that supersedes it.

## Phase 3: Launch-time staleness warnings

### Steps

1. Write one shared, stdlib-only, Python 3.9-safe helper (`check_versions.py`, alongside `merge_global_settings.py`) that both launchers call with the host `python3`. Given component names and their in-use versions or digests, it:
   - returns one warning line per component whose newest upstream release is more than N days newer than the one in use;
   - caches results in a state file with a timestamp, so it calls the network at most once a day;
   - times out quickly and prints nothing on any error.

   Sources:
   - **npm** — `registry.npmjs.org/<pkg>`: `dist-tags.latest` and the `time` map give publish dates for both versions. Verified reachable and in that shape.
   - **Docker Hub** — the tags API for `searxng/searxng:latest` gives its digest and `last_updated`. Get the pinned digest's date from the "known good on" comment, or from the tag listing.
2. `start-agent.sh` runs the helper for SearXNG, OpenCode and Pi as a background job started early, and prints its output just before attaching. Each line names the refresh command: `scripts/bump-searxng.sh` for SearXNG, and `--refresh-tools` for OpenCode/Pi. `start-claude.sh` does the same for sandbox-runtime and names the Phase 2 procedure (bump the pin, then `--rebuild`). Name the lag env var in the existing `CLAUDE_AGENT_*` style, plus a switch to turn the check off.
3. Unit tests in `tests/` for the helper's pure logic: lag computation, cache freshness, malformed or missing registry responses, and silence on timeout. Use fixture JSON with no network.
4. Update the README and CLAUDE.md, and add a short ADR (or a section of the Phase 1 ADR) explaining the lag-threshold choice.

## Unknowns / To Verify

1. **Does the build cache survive `docker image rm`?** This decides how to document `--rebuild`; the cache-bust arg is needed either way. Verify in Phase 1, step 1.
2. **Is `CAP_SETFCAP` in the Apple Containers bounding set?** This decides whether Phase 2 needs a new `--cap-add`. Verify in Phase 2, step 1. If it is present and the sandbox still fails, the problem is somewhere else in the 0.0.74–0.0.81 changes. Bisect the versions one at a time before giving up.
3. **Which OpenCode subcommand reports config errors?** `opencode --help` lists `debug`, but the subcommands under it were not checked. Run `opencode debug --help` in the container. If nothing validates config, the end-to-end `opencode run` is the config check. Affects Phase 1, step 6.
4. **Docker Hub API shape and the pinned digest's date.** The container cannot reach `hub.docker.com` (proxy, 000), so the tags-API fields (`digest`, `last_updated`) and whether that `digest` equals the manifest-list digest in `RepoDigests` were not checked. Verify from the host before writing the SearXNG half of the Phase 3 helper. If the digests do not match, compare by date only.
5. **SearXNG settings compatibility with a newer image.** The seed uses `use_default_settings` with `keep_only` engines and `outgoing.proxies` (`start-agent.sh:830-856`), so it mostly inherits upstream defaults. Renamed keys or engines would show up as missing engines in the Phase 1 verify script. The changelog was not reviewed.

## Notes

- OpenCode and Pi are not actually stale today (1.19.5 and 1.5.0 are npm `latest`). Of the three, only sandbox-runtime is clearly behind. SearXNG is 4 weeks behind a rolling `latest`.
- Pi shares the npm layer with OpenCode, so a refresh always moves both. The restore flag rolls back both.
- `claude-agent:previous` keeps one image's worth of extra disk in the VM. Only one generation is kept.
