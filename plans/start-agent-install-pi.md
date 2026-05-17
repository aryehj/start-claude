# Add `pi` (pi.dev) to start-agent.sh

## Status

- [ ] Install `pi` in `dockerfiles/claude-agent.Dockerfile`
- [ ] Add `.sandbox_config/pi/` to `init_sandbox()` and document it in the help/sandbox-layout sections of `start-agent.sh`
- [ ] Export `PI_CONFIG_DIR` alongside `OPENCODE_CONFIG_DIR` and ensure the directory exists during the per-sandbox seed step
- [ ] Generate `models.json` + `settings.json` under `$PI_CONFIG_DIR/agent/` using the same probe-and-discover logic that drives opencode
- [ ] Bind-mount `$PI_CONFIG_DIR` to `/root/.pi` on `docker run`
- [ ] Update `README.md` and `CLAUDE.md` to mention pi alongside opencode where appropriate

## Context

`start-agent.sh` already provisions a sibling LLM CLI (OpenCode) on top of the same Colima VM, container image, local-inference backend (Ollama/omlx), and per-sandbox state layout. The relevant prior art:

- Install line: `dockerfiles/claude-agent.Dockerfile:51` (`npm install -g opencode-ai@latest`).
- Per-sandbox config dirs declared in `init_sandbox()` at `start-agent.sh:185-190`.
- `OPENCODE_CONFIG_DIR` / `OPENCODE_DATA_DIR` set at `start-agent.sh:307-308`; `mkdir -p` at `start-agent.sh:1047`.
- Config injection: the python heredoc at `start-agent.sh:1118-1256` writes `opencode.json` after probing the local inference backend, including model discovery, default-model selection (honoring `CLAUDE_AGENT_DEFAULT_MODEL`), and provider auth wiring.
- Bind-mounts: `start-agent.sh:1415-1416`.

Pi (`@earendil-works/pi-coding-agent`) is a Node-based agentic coding CLI from the pi-mono repo. Its config lives at `~/.pi/agent/{auth.json, models.json, settings.json}`. Unlike OpenCode it has no plan/build/small agent split — a single active model selected via `/model` and persisted in `settings.json`. Auth and arbitrary provider definitions live in `models.json` under a single top-level `providers` map, with per-provider `baseUrl`, `api`, `apiKey`, `compat`, and `models[]` entries.

User decisions (from clarifying questions):

- **State dir:** mirror opencode — `$SANDBOX/.sandbox_config/pi/` bind-mounted to `/root/.pi` inside the container.
- **Model selection:** honor `CLAUDE_AGENT_DEFAULT_MODEL` only; the plan/exec/small env vars stay opencode-only.
- **MCP / SearXNG:** out of scope for this plan; pi gets local inference + auth-state persistence, nothing more.
- **Install method:** npm, not the curl-pipe installer.

## Goals

- `pi` is on `$PATH` inside the container after `start-agent.sh --rebuild`.
- A first run in a fresh sandbox lands pi pre-configured with a `local` provider pointing at the active Ollama/omlx backend, with discovered models populated and a sensible default selected.
- `pi`'s auth (after `pi login` for any cloud providers the user wants to add) and any user edits to `models.json` survive container recreation, just like opencode state does.
- Existing sandboxes pick up the new `.sandbox_config/pi/` directory automatically on the next invocation, without requiring users to re-init.

## Approach

The opencode injection step is the model: a python heredoc that reads existing state, probes inference, merges discovered models into a config dict, and writes back. Pi's split between `models.json` (providers) and `settings.json` (active model) is the main divergence — the implementation must update both files, not one. Reuse the same probe URLs and discovery logic already executed for opencode's heredoc; passing the discovered model list and the chosen default model into a second, smaller pi-specific writer is cleaner than duplicating the probe.

Pi's `compat` object varies by `api` family. For an Ollama/omlx OpenAI-compatible endpoint, `"api": "openai-completions"` is correct; the minimal `compat` flags Ollama needs (`supportsDeveloperRole: false`, `supportsReasoningEffort: false`) are documented upstream. Confirm before writing — the source of truth is `packages/coding-agent/src/core/model-registry.ts` in the pi-mono repo.

Existing sandboxes without `.sandbox_config/pi/` should be handled by the same `mkdir -p` line that already creates `$OPENCODE_CONFIG_DIR` at `start-agent.sh:1047` — that line runs every invocation, so adding `$PI_CONFIG_DIR/agent` there gives backfill for free. Do not require a `--reseed` or `--init-sandbox` rerun.

## Unknowns / To Verify

- **Exact field name(s) used to record the default model in `~/.pi/agent/settings.json`.** The relevant code is in `packages/coding-agent/src/core/settings-manager.ts` (`setDefaultModel` / `setDefaultModelAndProvider`, around lines 595-614). Verify whether the persisted shape is `{ "defaultModel": "<modelId>", "defaultProvider": "<providerId>" }` or a combined `"providerId/modelId"` string before writing the file. Affects the config-injection step.
- **Minimal valid `compat` block for an Ollama-style OpenAI-compatible endpoint.** Confirm against `model-registry.ts` (TypeBox schema near lines 78-198) and the example in `docs/custom-provider.md`. Affects the same step.
- **Pi's Node-version floor (`>=20`) vs the image's installed Node.** The Dockerfile installs `setup_lts.x` (line 29); Node LTS is currently >=20, so this should be fine, but a `node -v` check inside the freshly built image is worth running once.
- **Whether `npm install -g @earendil-works/pi-coding-agent@latest` puts the binary at `pi` on PATH** (vs e.g. `pi-coding-agent`). Confirm with `npm view @earendil-works/pi-coding-agent bin` or a build-and-`which pi` smoke test. The README claims `pi`; verify before updating user-facing docs.

## Notes

- Pi reads provider-specific API keys directly from process env (e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). No additional plumbing is needed for those — if the user wants pi to use a cloud provider, they set the env var on the host before running `start-agent.sh` and rely on the existing env-forwarding pattern. Out of scope for this plan, but worth a sentence in the README change.
- The deprecated `@mariozechner/pi-coding-agent` package name should not be used.
