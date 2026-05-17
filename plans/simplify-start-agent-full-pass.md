# Simplify start-agent.sh: full pass

## Status

- [ ] Phase 1: Extract the inline allowlist seed to `templates/agent-allowlist.txt`
- [ ] Phase 2: Unify the OpenCode + Pi inference-config injection
- [ ] Phase 3: Extract the iptables `firewall-apply.sh` heredoc to a template
- [ ] Phase 4: Compact the argument parser
- [ ] Phase 5: Decompose the top-level script body into a `main` driver
- [ ] Phase 6: Small cleanups (Opus recommended)

Annotations: phases 1, 3, 4 are mostly mechanical (Haiku ok); phases 2 and 5 carry real judgment calls; phase 6 needs taste.

## Context

`start-agent.sh` is 1548 lines today. Roughly:

- Lines 27–171: argument parsing — every long option is written twice (`--foo=` and `--foo VALUE`), ~140 lines.
- Lines 337–633: an inline ~287-line allowlist heredoc seeded once per sandbox.
- Lines 638–650, 919–971: two more inline Python / shell heredocs (filter-file generator, firewall-apply script).
- Lines 1121–1259 (OpenCode) and 1261–1386 (Pi): two near-identical Python heredocs that probe Ollama/omlx and write a JSON config. Both re-implement `discover_models()` and both run on the hot path.
- The entire body from line 173 down is flat top-level code; there is no `main`. Helper functions exist for VM bring-up, but the orchestration around them is imperative top-level.

The repo already has a `templates/` directory used the same way this plan extends it: `templates/global-claude.md` and `templates/research-denylist-*.txt` are seeded into sandbox state by a few-line copy block. The pattern is well established.

A predecessor `plans/_paused - simplify-start-agent.md` enumerated some of these wins (its Steps 5 and 6, both unchecked). This plan supersedes that one — it covers Steps 5 and 6 plus everything else, and the paused file should be moved to `plans/_implemented/` once this lands.

Tests live at `tests/test_agent_sh.py` and `tests/test_dockerfile.py`. They are static text checks against the script body, so most refactors here will require coordinated test updates rather than runtime-test rewrites.

## Goals

- start-agent.sh drops from ~1550 lines to ~700–900 without losing features.
- Inline heredocs >50 lines live in `templates/` and are seeded/copied, not embedded.
- One shared probe-and-writer path for the inference-provider config, used by both OpenCode and Pi.
- Top-level body reads as a sequence of named operations called from `main`, not 1300 lines of imperative orchestration.
- No regressions: existing tests pass, output files (allowlist, opencode.json, models.json, settings.json, AGENTS.md, settings.json under .claude) are byte-equivalent to today's modulo intentional minor changes called out per phase.

## Approach

The script's structural problems are layered: heredoc-as-data, duplication, and lack of decomposition. Tackle them in that order because each phase shrinks the script enough to make the next one easier to reason about. Phase 1 alone removes ~280 lines of literal data and is fully revertible; Phase 2 collapses the largest semantic duplication; Phases 3–5 reshape what's left. The heredoc-shape decision deferred during planning (inline shared heredoc vs `lib/` Python file) is settled in Phase 2 by whichever shape falls out cleanly when both writers must coexist — record the choice in the resulting ADR.

Behavior preservation is "loose": small surface changes are acceptable when they make the code cleaner (e.g. dropping the unreachable `else: entry.setdefault("apiKey", "omlx")` arm, normalizing provider-key naming between opencode and pi, switching positional argv to a single env-dict). Every such change must be flagged in the commit message and, where it affects the JSON config shape, smoke-tested against a real Ollama and a real omlx server before merge.

## Unknowns / To Verify

- **OpenCode and Pi config-file shape divergence.** OpenCode keys models as `{id: {name: id}}`; Pi keys as `[{id: ...}]`. Verify these are the actual shapes each tool consumes today (read the current `~/.config/opencode/opencode.json` and `~/.pi/agent/models.json` on the host after running) — phase 2 has to keep both shapes, only the probe is shared. Phase 2 step 1.
- **Pi's `defaultProvider` / `defaultModel` schema.** The current code writes flat top-level keys; confirm against Pi's own documented schema (read `node_modules/@earendil-works/pi-coding-agent/` or its README via `raw.githubusercontent.com`) before changing the writer. Phase 2 step 2.
- **`ScheduleWakeup` deferred-tool availability is irrelevant here** — listed only to confirm no unknown tool dependency.
- **Whether the paused predecessor plan ever fired Steps 5/6 partially.** `git log --all --follow plans/_paused\ -\ simplify-start-agent.md` should confirm; if it did, reconcile before starting Phase 1.

---

## Phase 1: Extract the inline allowlist seed

### Steps

1. Create `templates/agent-allowlist.txt` containing the body between `cat > "$ALLOWLIST_FILE" <<'ALLOWLIST'` and the closing `ALLOWLIST` marker (currently `start-agent.sh:340-627`).
2. Replace the heredoc block in `start-agent.sh` with a copy-from-template seed that mirrors the `GLOBAL_CLAUDEMD_TEMPLATE` pattern at `start-agent.sh:1057-1069`: seed-if-missing, overwrite on `$RESEED_ALLOWLIST`, error if the template file is missing.
3. Update `tests/test_agent_sh.py` to drop any test that greps for allowlist contents inside `_SCRIPT_TEXT` (the contents have moved); add a small test that asserts `templates/agent-allowlist.txt` exists and is non-empty.
4. Update `CLAUDE.md`'s "Layout" section to list the new template file.

### Acceptance criteria

- Diff between the previously-seeded `~/.../.sandbox_config/allowlist.txt` and a freshly-seeded one is empty.
- `start-agent.sh --reseed-allowlist` overwrites the sandbox copy from the template.

---

## Phase 2: Unify the OpenCode + Pi inference-config injection

### Steps

1. Verify the two output shapes (OpenCode: `provider.<key>.models` as dict; Pi: `providers.<key>.models` as list-of-dicts) by reading the live files in any active sandbox. Document the divergence as a one-paragraph ADR appended to `ADR.md`.
2. Decide the heredoc shape — either (a) a single Python file at `lib/inject-inference-config.py` invoked twice from the script with a `--target {opencode,pi}` flag, or (b) one merged inline heredoc that writes both files from one probe. Pick whichever leaves fewer lines of duplication after the merge. Record the choice in the same ADR.
3. Implement the shared probe: one `discover_models(backend, probe_urls, api_key)` that returns a normalized list of model IDs. Each writer (opencode and pi) consumes that list and shapes its own output.
4. Switch the writers' argv interface from positional `sys.argv[N]` to a single `json.loads(os.environ["INJECT_CFG"])` dict assembled in `start-agent.sh`. This is the out-of-scope finding from the recent `/simplify` pass and is the cleanest way to eliminate the `len(sys.argv) > N` defensive fallback chain.
5. Drop the unreachable `else: entry.setdefault("apiKey", "omlx")` branch in the pi writer (BACKEND is validated to `ollama|omlx` upstream).
6. Normalize the pi provider key — opencode uses per-backend keys (`"ollama"` / `"omlx"`); pi hard-codes `"local"`. Pick one convention; if pi must stay on `"local"` for its persisted-defaults logic, document why in the ADR rather than as an inline comment.
7. Update `tests/test_agent_sh.py` pi-block tests to match the new layout (the `_pi_inject_block()` helper just needs its sentinel string updated).

### Acceptance criteria

- One round-trip to the inference server per `start-agent.sh` run, not two. (Verify with `tcpdump`/`netstat` or `colima ssh` traffic counting before and after.)
- Both `opencode.json` and `models.json` continue to load cleanly in their respective tools after a cold sandbox run.

---

## Phase 3: Extract the iptables `firewall-apply.sh` heredoc

### Steps

1. Move the body of the `cat > "$TMP_WORK/firewall-apply.sh" <<FWEOF ... FWEOF` block at `start-agent.sh:923-969` to `templates/firewall-apply.sh.tpl`. Keep the `FWEOF` (unquoted) semantics — host variables are still interpolated.
2. Use `envsubst` (or a sed-pass) at the seed site to perform the substitution, or keep the `cat <<FWEOF` form but `source` the template via `eval "$(cat templates/firewall-apply.sh.tpl)"`. Pick the form that produces the cleanest diff and doesn't require adding `envsubst` to the macOS host dependency surface.
3. Update `tests/test_agent_firewall.sh` if it greps for firewall rule strings in `start-agent.sh` rather than in the rendered file.

### Acceptance criteria

- Output of `iptables -S CLAUDE_AGENT` inside the VM is byte-identical to the pre-change output.

---

## Phase 4: Compact the argument parser

### Steps

1. Replace the 30-line case ladder at `start-agent.sh:133-164` with a single loop that handles both `--foo=value` and `--foo value` forms via one `${1#*=}` extraction. Reference patterns: each option becomes a one-line entry that maps the long name to its target variable.
2. Remove the redundant `${var:?--foo requires a value}` checks where the loop body already guarantees presence; keep them only at the boundary between option-with-value and option-without-value detection.
3. Centralize the deprecated `--enable-local-search` warning in a dedicated `deprecated_flags=(...)` table so future deprecations don't need a new ladder entry.

### Acceptance criteria

- All existing flag combinations (`--memory=8`, `--memory 8`, `--rebuild`, `--init-sandbox PATH`, etc.) parse identically.
- Running `start-agent.sh --help` produces the same usage text.
- `bash -n start-agent.sh` passes.

---

## Phase 5: Decompose the top-level script body into a `main` driver

### Steps

1. Identify the natural operation blocks in the top-level body (the `# ── ... ──` banner comments are already a faithful outline). For each block, define a function named after the banner (e.g. `seed_global_claudemd`, `bring_up_vm`, `apply_firewall`, `inject_inference_config`, `run_container`, `attach_existing`).
2. Move each block's body into its function. Keep shared globals (constants, parsed flags, derived paths like `$BRIDGE_IP`, `$HOST_IP`) as script-scope variables set by an `init_state` function called first.
3. Define a `main` at the bottom that calls each function in the existing order. Top-level should be: `set -euo pipefail` → trap setup → variable declarations → function definitions → `main "$@"`.
4. Update `tests/test_agent_sh.py`'s `_BLOCKS` extraction if it relies on top-level positional grep (the existing pattern grabs `IMAGE_TAG`-bearing `docker run` blocks — verify it still finds them inside a function body).

### Acceptance criteria

- Script behavior unchanged: a clean-sandbox cold run, a warm reattach, `--rebuild`, `--reload-allowlist`, and `--init-sandbox` all produce identical user-visible output (modulo line numbers in error messages).

---

## Phase 6: Small cleanups

### Steps

1. Collapse `vm_ssh` (`start-agent.sh:683-689`) into `vm_sh` by giving the latter a `--argv` mode, or drop one of the two — they currently differ only in whether the caller is responsible for `printf %q`-quoting.
2. Drop the `mkdir -p "$(dirname "$ALLOWLIST_FILE")"` at `start-agent.sh:338` if `init_sandbox` is now guaranteed to have created it (verify by checking `find_sandbox_root` invariants).
3. Remove the `os.makedirs(os.path.dirname(path), exist_ok=True)` calls inside the inference-config writers — the shell already creates these dirs via the every-invocation `mkdir -p` at line 1050.
4. Audit comment hygiene across the script: delete WHAT comments and change-narrating comments ("Like the opencode block above", "Re-runs every invocation so model lists stay fresh" is a WHY — keep), keep WHY comments that explain hidden constraints (the colima-ssh quoting note at `vm_ssh`, the `BRIDGE_IP` traffic-flow note in iptables, the searxng-ignores-HTTPS_PROXY note in CLAUDE.md).

### Acceptance criteria

- No measurable behavior change. Run the full `tests/` suite and a cold-sandbox smoke test.

---

## Notes

- **Predecessor plan.** `plans/_paused - simplify-start-agent.md` covers a subset of this (its unchecked Steps 5/6 land as parts of Phases 1 and 6 here). When this plan is fully checked, move the predecessor to `plans/_implemented/` and add a one-line note that it was superseded.
- **ADRs.** Phase 2 produces at least one new ADR (heredoc shape, provider-key convention). Phase 3 may produce one (template-rendering choice). Phases 1, 4, 5, 6 are mechanical enough to skip ADRs unless something surprising falls out.
- **Risk concentration.** Phase 5 is the riskiest single phase — it touches every block. Land Phases 1–4 first, sit on them for a session of normal use, then start Phase 5. Phase 6 should be last so it can clean up anything Phases 1–5 left behind.
- **Tests as a brake.** The existing static tests (`tests/test_agent_sh.py`) are useful guardrails but several use weak substring matching. As each phase moves code around, prefer to tighten the affected test to anchor on structure (function name, heredoc body, etc.) rather than to relax it to match the new text.
