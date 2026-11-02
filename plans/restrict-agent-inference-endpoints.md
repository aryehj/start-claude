# Restrict start-agent inference access to an endpoint allowlist

## Status

- [x] Add the in-VM inference proxy: install, config generation, guarded push, probe and hash integration (Opus recommended)
- [ ] Rewire the firewall: remove the direct host carve-out, open only the proxy, stop tinyproxy reaching the host (Opus recommended)
- [ ] Repoint the container and client configs at the proxy, and remove the real key from them
- [ ] Warn on reattach when a container predates the proxy
- [ ] Update static tests and the in-container and cross-VM smoke tests
- [ ] Docs: ADR-050, README, CLAUDE.md, `templates/global-claude.md` (Haiku ok)
- [ ] Live verification on a Mac host (see Unknowns)

## Context

`start-agent.sh:890` RETURNs all container traffic to `$HOST_IP:$INFERENCE_PORT`, so agents reach every route on the inference server. Reading the omlx source (v0.7.3, `HEAD` of `jundot/omlx`) shows that this includes routes that reach the Internet or change the server:

| Route | Effect | Auth |
|---|---|---|
| `POST /v1/web/search`, `POST /v1/web/fetch` (`omlx/api/websearch_routes.py`) | Server-side web access | inference key or sub key |
| `POST /v1/mcp/execute` (`omlx/api/mcp_routes.py`) | Runs configured MCP tools | inference key or sub key |
| `/admin/api/*` (`docs/admin-api.md`) | Hugging Face and ModelScope download and upload, settings changes, sub-key creation, logs | main key only (a few routes accept sub keys) |

Sub keys therefore do not solve this: they still work on `/v1/web/*` and `/v1/mcp/execute`. Toggling omlx settings on the host is not enough either, because the agent holds the main key today and could toggle them back.

The Ollama backend (the default) has the same gap through `/api/pull` and `/api/push`.

## Goals

- From the agent container, only these inference routes work. Everything else is refused before it reaches the host, whatever credentials the agent presents.
  - omlx: `POST /v1/chat/completions` (also carries image analysis), `GET /v1/models`, `POST /v1/messages`, `POST /v1/messages/count_tokens`
  - Ollama: `POST /v1/chat/completions`, `GET /v1/models`, `GET /api/tags`
- The agent has no direct network path to the host's inference port, including through tinyproxy.
- The real `OMLX_API_KEY` is absent from the container: not in its env, `opencode.json`, or Pi's `models.json`, for any provider entry.
- OpenCode and Pi keep working, including streaming and vision requests, with no user configuration.

**Out of scope, by decision:** the agents' own Internet paths are unchanged. That means the tinyproxy allowlist, OpenCode `webfetch`, SearXNG websearch and Claude Code's web tools. `research.py` and `start-claude.sh` are also untouched. Claude Code is not being configured to use omlx either; this only makes `/v1/messages` reachable.

## Approach

Add an nginx reverse proxy inside the Colima VM as the only route to inference, listening on `$BRIDGE_IP:$INFERENCE_PORT`. The VM is the existing boundary the agent can't modify (ADR-010), so the rule stays out of reach of the thing it constrains. Keeping the same port number means `INFERENCE_PORT` keeps its current meaning.

nginx rather than a tinyproxy reverse mode: tinyproxy's `ReversePath` matches prefixes and can't filter by method, and exact path-plus-method matching is the whole point here. The proxy is default-deny, with a catch-all that returns 403 and one exact-match location per allowed route.

The proxy also holds the omlx key. It overwrites client credentials on every request, so a leaked or guessed key in the container is irrelevant, and the agent has nothing worth exfiltrating to an allowlisted host.

## Steps

### Proxy (`start-agent.sh`, alongside the tinyproxy block at lines 766-859)

- Install `nginx` in the VM, following the tinyproxy install pattern. Remove the default site so nothing listens on port 80. Use `restart` rather than `start` on first bring-up, for the same reason given at `start-agent.sh:844-851`: the package's postinst auto-starts the service with its default config.
- Generate the config on the host from `BACKEND`, with the allowlist from Goals. Requirements:
  - Listen on `$BRIDGE_IP:$INFERENCE_PORT` only.
  - Use `location =` exact matches plus a method restriction, with a catch-all 403. Do not use prefix locations: `/v1/models` must not admit `/v1/models/{id}/load`, and `/v1/messages` must not admit anything beneath it.
  - Send a fixed upstream path from each location, not the client's raw URI. nginx matches on the normalized path but would otherwise forward the raw form, so a traversal or encoded variant could match one route and be forwarded as another. Keep query strings working, since Anthropic clients send `/v1/messages?beta=true`.
  - Refuse WebSocket upgrades (omlx exposes `/v1/realtime`; the catch-all covers this path, but do not forward `Upgrade` headers).
  - Support long-running streams: HTTP/1.1 upstream, response and request buffering off, read timeout at least as long as tinyproxy's 600s.
  - No nginx body-size cap, because base64 images are large and omlx enforces its own limit (`omlx/api/body_limit.py`).
- Credentials: for omlx, overwrite `Authorization` with the host's key and clear `x-api-key` on every request. omlx prefers the Bearer token (`verify_api_key`, `omlx/server.py:371`). If `OMLX_API_KEY` is unset, send neither. For Ollama, clear both.
- Secret handling: the config holding the key is root-only (0600) in the VM, and the key travels over stdin as in `write_tinyproxy_push`, never on an argv. Refuse to start with a clear error if the key contains characters that nginx would interpret (quotes, `$`, whitespace, backslash, `;`), rather than trying to escape them.
- Extend the batch probe at `start-agent.sh:595-625` with nginx install status, active status and stored hash, so warm reattach stays within the SSH budget (ADR-045). Gate the push on the hash and active status, as at line 856. The hash covers the config, including the key, so rotating the key triggers a push. Keep the hash file root-readable only.
- Mirror the guard from commit `ec71c4c`: before installing, the VM-side script must check that the catch-all is present and that `nginx -t` passes, and otherwise leave the old config and old hash in place and fail. A push failure must abort the run. Failing open is not acceptable here.

### Firewall (`start-agent.sh:869-930`)

- Delete the `CLAUDE_AGENT` RETURN for `$HOST_IP:$INFERENCE_PORT` at line 890.
- Add a `CLAUDE_AGENT_IN` RETURN for `$BRIDGE_IP:$INFERENCE_PORT`, next to the tinyproxy rule at line 902.
- Block tinyproxy from reaching `$HOST_IP`, using an `OUTPUT` chain with an owner match on the `tinyproxy` user, in the same atomic `iptables-restore` commit. tinyproxy runs in the VM, so its traffic never crosses `CLAUDE_AGENT`. Its filter is by hostname only, and `FilterURLs No` means any allowlisted name that resolves to the host would otherwise give an unfiltered path to the inference port. This also covers future user edits to the allowlist.
- Push the proxy before applying the firewall, so there is no window where a healthy setup has neither path. This must hold on the `--reload-allowlist` path too (line 936 exits right after the firewall).

### Container and client config

- Set `OLLAMA_HOST` / `OMLX_HOST` to `http://$BRIDGE_IP:$INFERENCE_PORT` (`start-agent.sh:1411-1423`) and stop forwarding `OMLX_API_KEY` into the container. `$BRIDGE_IP` is already in `NO_PROXY`. Drop `$HOST_IP` from `NO_PROXY`/`no_proxy` since nothing legitimate goes there now.
- In the OpenCode and Pi injectors (`start-agent.sh:1082-1084`, `1226-1228`), change `runtime_url` to the proxy address. Keep discovery as is: `probe_urls` runs on the macOS host and needs the real key.
- Always write a placeholder `apiKey`. `setdefault` at lines 1166 and 1298 would keep a stale real key, so overwrite it. Also scrub non-active entries: `ollama` and `omlx` provider entries coexist in `opencode.json`, so a real key in the inactive entry survives if only the active one is rewritten. Rewrite the base URL of those entries as well.
- Update the startup summary at line 1557 to show the proxied address.

### Migration

`docker run -e` values are fixed at create time, so an existing container keeps its old `OMLX_HOST` and its real `OMLX_API_KEY`. In `attach_existing`, next to the `--dns` check at line 1519, inspect the container's env. If it holds `OMLX_API_KEY` or an inference URL pointing at `$HOST_IP`, warn prominently that inference is broken and the key is still exposed until `start-agent.sh --reset-container`. Do not auto-remove the container (ADR-048).

### Tests

- `tests/test_agent_sh.py`:
  - Invert the assertion at line 497 (`OMLX_API_KEY` must no longer be forwarded).
  - Assert that no `CLAUDE_AGENT` rule targets `$HOST_IP`, and check `test_firewall_chain_applied_atomically` (line 456) against the new rules.
  - Assert that the generated allowlist is exactly the Goals set per backend, with exact-match locations and a catch-all.
  - Assert that the guard exists, and that the placeholder overwrite covers both injectors.
- `tests/test-agent-firewall.sh` (rename the Ollama carve-out test at lines 83-102, and update `test_env_wiring`, which requires `OLLAMA_HOST` regardless of backend):
  - Allowed routes succeed. Streaming chat works. An unauthenticated request succeeds, which proves injection.
  - Refused: `/v1/web/search`, `/v1/web/fetch`, `/v1/mcp/execute`, `/admin/api/global-settings`, `/v1/responses`, `/v1/models/x/load`, `/v1/models/status`, `/api/pull`, the wrong method on an allowed path, and traversal and percent-encoded variants of denied paths.
  - Direct `$HOST_IP:$INFERENCE_PORT`, `host.docker.internal:$INFERENCE_PORT` (it maps to `$HOST_IP`), and the same URL through tinyproxy are all blocked. For the tinyproxy case, clear `NO_PROXY`/`no_proxy` and pass `--noproxy ''`: curl honours `NO_PROXY` even with `-x`, so a host listed there goes direct and the test measures the wrong path.
  - `OMLX_API_KEY` is absent from the env, and the key does not appear in `opencode.json` or `models.json`.
- `tests/test-cross-vm-isolation.sh:136-160`: `ECONNREFUSED` no longer proves the carve-out, because nginx answers with a 502 when the backend is down. Rewrite the test to expect the proxy.

### Docs

- ADR-050: the decision, why not sub keys or omlx settings, why nginx, and what remains open by design (Out of scope above).
- README: egress paths at lines 402-406, the omlx auth statement at 275, and the examples at 489 and 520-521 (no key needed now).
- `CLAUDE.md`: start-agent key decisions.
- `templates/global-claude.md:65`: state which endpoints are available.

## Verified against the live server (2026-11-02)

Run from the agent container against the installed omlx **0.7.3**, the same version as the source read for this plan.

- **Remote images are rejected.** `http://` and `file://` image URLs on `/v1/chat/completions`, and an Anthropic `url` image source on `/v1/messages`, each returned 400 "Remote URLs and local file paths are not supported". omlx's README is out of date on this. A base64 image was analysed correctly on the vision model `gemma-4-e4b-it-6bit`. Still add this as a smoke test to catch regressions on upgrade, skipped when no vision model is available.
- **Chat loads models automatically.** A chat request succeeded with no model loaded and no load call, so blocking `/v1/models/{id}/load` is safe.
- **OpenCode and Pi stay inside the allowlist.** Both were run through a logging proxy that replaced client credentials with the real key, using a placeholder `apiKey` in throwaway configs. Each completed a prompt and called only `POST /v1/chat/completions`. Neither called `/v1/models` at runtime, because discovery happens on the macOS host. This was a text prompt with no tool calls or images from either client.
- **Streaming works, and `x-api-key` is accepted** on `/v1/messages/count_tokens?beta=true`, so clearing that header in the proxy matters.
- **tinyproxy currently refuses the host:** `403 Filtered` for both `http://192.168.5.2:8000` and CONNECT to the host IP. The owner-match rule is still needed, because the current protection is only the hostname filter.
- **The real key is in both client configs today** (`opencode.json` at `provider.omlx.options.apiKey`, and Pi's `models.json`), confirming the scrub is needed.

## Unknowns / To Verify

These can't be checked from inside the container.

- **Server-side tool execution with MCP configured.** This server has no MCP servers (`/v1/mcp/servers` is empty, `/health` reports `mcp: null`), so this could not be tested live. At `HEAD`, the only caller of MCP tool execution is the `/v1/mcp/execute` route, so a model-emitted tool call should return to the client unexecuted. Re-test this if an MCP server is ever added to omlx.
- **Package and boot behaviour on the Colima VM.** Needs a Mac host: confirm `nginx` installs from the VM's apt sources (the comments suggest Ubuntu 24.04), and that a VM reboot recovers. nginx can fail at boot if `docker0` isn't up yet when it binds `$BRIDGE_IP`; the active-status gate should re-push and restart on the next run, but verify this.
- **The implementation itself.** Run `tests/test-agent-firewall.sh` in a container recreated after the change, and check the nginx access log during a real OpenCode and Pi session, including one with tool calls and one with an image.

## Notes

- Because the proxy filters by route, a new omlx release that adds an internal network feature to an allowed route would not be caught. Re-check the callers listed above when upgrading omlx.
- Filtering request bodies (for example, rejecting `image_url` values that aren't data URIs) was rejected. It would need scripting in the proxy and would break streaming pass-through. Pin a verified omlx version instead.
