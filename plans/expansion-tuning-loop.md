# Phase 6.5: Query expansion / search-term tuning loop (agent-as-loop)

This is a single-phase plan that adds **Phase 6.5** to `local-research-harness.md`, between Phase 6 (SearXNG config — done) and Phase 7 (synthesis). After landing, the implementer should also append a `- [ ] Phase 6.5: ...` line to that plan's Status checklist.

## Status

- [x] Phase 6.5: query expansion + search-term tuning loop (agent-as-loop)

<!-- mark [x] when complete. No effort annotation: same shape as Phase 6 — sonnet-high recommended because agent judgement IS the metric. -->

## Context

Phase 6's `RESULTS.md` (`tests/local-research/eval/searxng_config/RESULTS.md`) identified query formulation — not SearXNG config — as the binding constraint on candidate-set quality:

- PubMed returns **0 results** for q3's full-question text ("A cyclist develops stubborn medial knee pain…") regardless of weight or timeout. Short keyword variants ("medial knee pain cycling") yield 15–19 PubMed hits.
- The `categories=science` route surfaces clinical-orthopaedics content (journals.lww.com saphenous-nerve articles) that the general-category route never sees, even with the winning Phase 6 config.
- Phase 6's `oa_doi_rewrite` plugin and `hostnames.high_priority` boosts only fire when the right URLs are *in the candidate set to begin with* — so expansion-side coverage is upstream of every Phase 5/6 lever.

What Phase 5 already landed (do not redo):

- `lib/expand.py` with three named prompts (`generic`, `scholarly-tilt`, `anti-seo`), selectable via `EXPAND_PROMPT_NAME` or overridable via `EXPAND_PROMPT`.
- `lib/search.py` with `categories`, `engines`, `pages` kwargs.
- `lib/pipeline.py:gather_sources()` with a binary `SCHOLARLY_MODE` env flag that routes non-seed expansions to `categories=science`.

What this phase changes:

- Replaces the binary `SCHOLARLY_MODE` flag with **per-expansion routing** (each expansion gets its own `categories` / `engines` override).
- Tunes `n_expansions` (currently hard-coded default 4 in `gather_sources`).
- Iterates on prompt-template content beyond the 3 Phase 5 variants — agent judgement-driven, not Phase 5's OFAT cells.

The variable is **expansion-side**: prompt text, `n`, per-expansion routing, and seed inclusion. SearXNG config (Phase 6) and source priors (Phase 5 Lever E) are held constant.

## Goals

- A winning expansion configuration committed as the new defaults in `lib/expand.py` (prompt) and `lib/pipeline.py` (routing + n).
- Per-expansion routing replaces the binary `SCHOLARLY_MODE` flag — no compat shim.
- An audit trail in `tests/local-research/eval/expansion_tuning/iterations.jsonl` of every iteration the agent ran, with rationale and kept/reverted per row.
- A `RESULTS.md` documenting trajectory, per-axis findings, and ≥3 downstream-orchestration follow-ups derived from specific iteration rows.
- The agent — not a Python driver — runs the loop, turn by turn, using the actual local model (`omlx` + `gemma-4-…-it`) so findings transfer to the production pipeline.

## Approach

Same shape as Phase 6: agent-as-loop, throwaway harness, raw-result-driven judgement, no auto-scoring. The harness reads a single config file the agent edits per iteration; nothing inside `lib/` changes during the loop. After convergence, port the winning config into `lib/expand.py` and `lib/pipeline.py` defaults.

The critical contrast vs Phase 6: this loop calls the **actual local model** per iteration to produce expansions, not Sonnet itself. That's the point — prompt-quality findings against Gemma 26B are what transfer to production. Sonnet is the judge looking at what Gemma actually emitted, not the rephraser. Per-iteration cost rises (~5–10s for expansion + a few SearXNG calls per fixture); the user has confirmed wall-clock isn't the constraint, **context window is** — so the agent should batch-summarise rather than re-Read large `iterations.jsonl` files mid-loop.

The other contrast: Phase 6 had one knob surface (`settings.yml`) and the harness just hit SearXNG. This loop has *two* knobs the agent can move (prompt + routing), so the per-axis discipline matters more — one axis per iteration, no stacking until each axis is independently characterised.

## Unknowns / To Verify

1. **omlx availability and model identity.** The harness needs to call the actual local model. Verify before starting: `omlx` is up at `$OMLX_HOST` (or `$OLLAMA_HOST` per `CLAUDE.md`), `EXPAND_MODEL` resolves (read `tests/local-research/lib/config.py`), and a one-shot `lib.omlx.chat(EXPAND_MODEL, [...])` returns text. If down, abort the loop and tell the user — do not silently fall back to Sonnet-as-rephraser.
2. **Existing `iterations.jsonl` format from Phase 6.** Reuse the row schema (`iter, ts, settings_sha, top_n, axis_touched, mutation_summary, rationale, kept_or_reverted, top_ranked_per_query`) plus add `expansions_per_query` (the actual Gemma output, indexed by fixture slug) and `config_sha`. `settings_sha` becomes `config_sha` (hash of the per-iteration config file).
3. **Additional fixtures.** User asked for 1–2 more fixtures beyond Phase 6's q3/creatine/finance-team. The plan suggests a draft set in step 1; the implementer is free to swap if they have a stronger candidate that hits the same gap profile. Constraint: at least one new fixture should stress a domain Phase 6 didn't cover (e.g., recent-news factual lookup or a non-medical training/biomechanics question), and at least one should be a long natural-language question (where short-keyword distillation is the relevant axis).
4. **Per-expansion routing data shape.** The current `SCHOLARLY_MODE` flag is binary. The new shape (a list of `{categories, engines}` dicts indexed by expansion position) needs to round-trip through the env layer cleanly — proposal is a JSON env var `EXPAND_ROUTING` (verify `os.environ.get` + `json.loads` behaves the same way `SCHOLARLY_MODE` does today; do not introduce a new config file in `lib/`).
5. **Whether `omlx.chat` is deterministic enough to compare iterations.** Two runs of the same prompt may produce different expansions if temperature > 0. Check whether `lib/omlx.py` sets seed/temperature; if not, the harness should pass `temperature=0` (or whatever `omlx`'s deterministic mode is) so iter-to-iter diffs are due to prompt/N changes, not sampling noise. If determinism isn't possible, the harness should run each fixture's expansion 3× and record all three (more context cost, but interpretable).

## Phase 6.5: query expansion + search-term tuning loop

### Steps

1. **Freeze the upstream baseline.** Run the harness (built in step 3) once with the Phase 5 defaults: `EXPAND_PROMPT_NAME=generic`, `n_expansions=4`, `SCHOLARLY_MODE` unset (no per-expansion routing), seed always included. Use 5 fixtures:
   - `q3` — full-question medical (carry over from Phase 6)
   - `creatine` — short-keyword medical-supplement (carry over)
   - `finance-team` — natural-language business benchmarks (carry over)
   - one **recent-news factual lookup** — implementer picks; criterion: a query with a specific verifiable fact (number, date, name) where Phase 6's general+science routing should not help. Suggested seed: `"2024 united states presidential election popular vote margin"`.
   - one **long-form non-medical natural-language question** — implementer picks; criterion: phrased like q3 (multi-clause, embedded sub-questions) but in a domain that doesn't trigger PubMed. Suggested seed: `"a runner who has never had heel pain before suddenly develops stiffness on the bottom of the foot in the morning that loosens up after walking — what are the most likely causes and how would a clinician distinguish between them?"`. (Yes, this is plantar-fasciitis-shaped on purpose — it tests whether the medical-jargon expansion path generalises beyond cycling.)
   
   Save iter-0 row to `tests/local-research/eval/expansion_tuning/iterations.jsonl`. The Phase 5 defaults are this loop's reference for "did the expansion change help".

2. **Enumerate the search space.** Four axes, in the order the agent should pick them up. Hold the SearXNG config (Phase 6 winner) and source priors (Phase 5 Lever E) constant the whole loop.

   - **A. Expansion prompt template.** Beyond the 3 in `lib/expand.py` (`generic`, `scholarly-tilt`, `anti-seo`), candidates worth trying:
     - **keyword-distillation**: "Output `{n}` short search-keyword strings (3–6 tokens each) suitable for a search engine, derived from the question. No natural-language phrasings; no quoting; no `site:` operators." — addresses the q3 PubMed-zero-results case directly.
     - **medical-jargon-expansion**: "If the question is medical, generate `{n}` expansions including at least one with the specific anatomical/diagnostic terminology a clinician would use (e.g., 'pes anserine bursitis' rather than 'inner knee pain', 'plantar fasciitis' rather than 'bottom of foot stiffness'). If the question is non-medical, fall back to general technical-vs-lay reformulations." — tests whether jargon-aware rewrites unlock the journals.lww.com tier.
     - **boolean-and-exclusion**: "Generate `{n}` expansions; at least one must use `-` exclusion to drop common SEO terms (`-best -guide -top`); at least one must use site-restriction (`site:gov OR site:edu OR site:nih.gov`)." — tests whether SearXNG passes operators through to underlying engines.
     - **mixed-register**: each expansion explicitly tagged with intended register (e.g., one keyword-only, one quoted-exact-phrase, one natural-language, one with operators). Lets per-expansion routing (axis C) stack on register-specific expansions cleanly.
   - **B. Number of expansions (`n`).** Currently 4. Try {1, 2, 6, 8}. Hypothesis: small n undertrains coverage; large n adds noise + cost without unlocking new domains. The PubMed rate-limit finding from Phase 6 means n>4 may push NCBI past its threshold — record this if observed.
   - **C. Per-expansion routing.** Replace the binary `SCHOLARLY_MODE` with a per-position routing list: `[{categories: null, engines: null}, {categories: "science", engines: null}, {categories: null, engines: "pubmed"}, ...]`. Combinations to try:
     - all-default (control, equivalent to `SCHOLARLY_MODE=`)
     - all-science (equivalent to `SCHOLARLY_MODE=1`, baseline)
     - alternating: position 0 default, position 1 science, position 2 default, position 3 science
     - engine-pinning: position 0 default, positions 1+ pinned to specific engines (`pubmed`, `arxiv`, `google scholar`) one engine each
     - register-aligned: paired with the **mixed-register** prompt, route keyword-only expansions to `engines=pubmed`, natural-language to default, operators to `engines=google` (operators rarely survive duckduckgo's parser)
   - **D. Seed inclusion.** Currently `expansions = [query, *expansions]` always. Try dropping the seed when the original is a long natural-language question (q3, the new long-form fixture) — hypothesis: the seed wastes a SearXNG slot returning the same SEO surface every time. Test against keeping it. This is a one-flag axis, fastest to characterise.

3. **Add the iteration harness.** `tests/local-research/eval/expansion_tuning/iterate.py` — stdlib + project-local imports only (`lib.expand`, `lib.search`, `lib.config`, `lib.omlx`). Same throwaway shape as `tests/local-research/eval/searxng_config/iterate.py`. Drop it after the phase; what survives is `iterations.jsonl`, `RESULTS.md`, and the ported defaults.

   Inputs:
   - `tests/local-research/eval/expansion_tuning/iter_config.json` — the agent edits this per iteration. Schema:
     ```json
     {
       "prompt_template": "Generate {n} ...\n\nQuery: {query}",
       "n_expansions": 4,
       "include_seed": true,
       "per_expansion_routing": [
         {"categories": null, "engines": null},
         {"categories": "science", "engines": null}
       ]
     }
     ```
     `per_expansion_routing` is indexed by post-`include_seed` position. If shorter than `n_expansions + (1 if include_seed else 0)`, missing slots default to `{categories: null, engines: null}`.
   - CLI flags: `--top-n` (default 15), `--axis-touched`, `--mutation-summary`, `--queries` (comma-separated subset of fixture slugs).

   Per invocation:
   - Read `iter_config.json`; compute `config_sha` (sha256 of the file content, first 12 chars).
   - For each fixture query: call `lib.expand.expand(query, n=cfg.n_expansions)` with the prompt set via `EXPAND_PROMPT` env var (override the in-module default temporarily); record the actual returned `expansions` list. If `include_seed` is False, drop position 0 before searching.
   - For each expansion at position `i`, look up routing via `cfg.per_expansion_routing[i]` (defaulting if missing) and call `lib.search.search(expansion, n=20, categories=routing.categories, engines=routing.engines)`.
   - Flatten + URL-dedupe + take top-N raw results per fixture (no rerank, no priors — same discipline as Phase 6: expose what the candidate set looks like before the rest of the pipeline processes it).
   - Determinism check (Unknowns #5): if `omlx.chat` doesn't accept a temperature kwarg, run expansion **3×** per fixture and record all three; agent judges with sampling noise visible. If it does, pass `temperature=0`.
   - Append a row to `iterations.jsonl`:
     ```
     {iter, ts, config_sha, top_n, axis_touched, mutation_summary, rationale, kept_or_reverted,
      expansions_per_query, top_ranked_per_query}
     ```
     `rationale` and `kept_or_reverted` are blank-on-write; agent patches them via Edit on the same turn (same protocol as Phase 6).
   - Stdout: one JSON summary line `{iter, config_sha, queries, expansion_counts, result_counts}`.

   Constraints:
   - No score column. No labels. No LLM judge inside the harness. The agent IS the judge.
   - `iter_config.json` is the *only* file the agent edits during the loop. `lib/` is untouched until step 5 (port the winner).
   - If `omlx` is unreachable, the harness fails fast with a clear error — do not silently fall back to Sonnet.

4. **Be the loop.** *You — the model executing this step — are the iteration driver.* Same protocol as Phase 6. Per turn:
   - **Read** the last row (or last few rows) of `iterations.jsonl` and the current `iter_config.json`. Skim `expansions_per_query` to see what Gemma actually produced last iteration; this is the evidence base alongside `top_ranked_per_query`.
   - **Decide** the next mutation. One axis per iteration. Order the agent should walk: A (prompt) → B (n) → D (seed inclusion, fastest) → C (routing). Once each axis has been independently characterised across all 5 fixtures, allow stacking.
   - **Edit** `iter_config.json`.
   - **Run** the harness with Bash:
     ```
     python3 tests/local-research/eval/expansion_tuning/iterate.py \
       --top-n 15 --axis-touched <axis> --mutation-summary "<one line>"
     ```
   - **Judge** the appended row: read `expansions_per_query` first (did Gemma follow the prompt?), then `top_ranked_per_query` (did the candidate set shift in the intended direction?). The agent's rationale should distinguish "Gemma ignored the prompt" from "Gemma followed the prompt but the candidate set didn't change" — both are findings, but they imply different next mutations.
   - **Patch** the row in-place: set `rationale` (one sentence; mechanical and specific) and `kept_or_reverted`. If reverted, restore `iter_config.json` to the prior winning state before the next turn.

   Discipline:
   - Each `rationale` is one auditable sentence (e.g., "keyword-distillation prompt + n=4 yielded 'medial knee pain cycling' which got 17 PubMed results vs baseline 0; q3 top-15 now has 4 PMC URLs in positions 1–6, kept").
   - One axis per iteration until each is independently characterised. Stacking comes after.
   - If the agent observes Gemma ignoring instructions repeatedly (e.g., emitting natural-language phrasings under a keyword-only prompt), record it as a finding and try a different prompt structure rather than escalating prompt aggressiveness — that's a downstream-pipeline signal worth capturing.
   - The agent is responsible for keeping `iterations.jsonl` reads short — context window is the budget. Re-reading the entire log every iteration is wasteful; rely on the audit trail discipline + occasional skim.

   Stop conditions, whichever fires first — record the reason on the final row's `stop_reason` field, then stop:
   - the agent declares the search saturated for the chosen axes,
   - **agent estimates ≤25% of context window remaining** — leave headroom for step 5 (port) and step 6 (RESULTS.md), both of which need fresh reads of multiple files.
   - 5 consecutive iterations with `kept_or_reverted: reverted` or `kept` with rationale "no meaningful change",
   - ≥30 iterations completed and each axis has been independently characterised.

5. **Commit the winning config in place.** Two files:
   - `tests/local-research/lib/expand.py` — if the winning prompt is one of the existing named templates, just change the default selected when no env var is set (currently `"generic"` at line 48). If it's a new template, add it to `_PROMPTS` with a descriptive name and update the default. No removal of the existing 3 unless they're proven dominated; this file is small and other phases may compare against them later.
   - `tests/local-research/lib/pipeline.py` — replace the `SCHOLARLY_MODE` env-flag block (lines 41–42, 56) with the per-expansion routing data structure. Read routing from a new env var `EXPAND_ROUTING` (JSON-encoded list of `{categories, engines}` dicts); if unset, fall back to the **winning baked-in default** discovered by the loop. No compat shim for `SCHOLARLY_MODE` — delete the old code path entirely (matches the project's no-compat-shims commit style). If the winning `n_expansions` differs from 4, update the `gather_sources` default at line 22.

   Update the `gather_sources` docstring to document the new routing data structure with one example. Do not introduce new files in `lib/`; keep the surface tight.

6. **Write the explanation.** `tests/local-research/eval/expansion_tuning/RESULTS.md`:
   - **Per-iteration trajectory.** Ordered list of `(iter, axis_touched, mutation_summary, kept_or_reverted, rationale)`. The `rationale` column is the trajectory; there is no score column.
   - **Per-axis takeaway.** Which of A/B/C/D contributed the most to the winning config; which axis converged fastest; which axis the agent kept the most mutations on.
   - **Top-N kept knobs.** The final winning prompt (full text), `n`, routing list, `include_seed` value — each with a one-sentence rationale citing the iteration row that adopted it.
   - **Per-fixture before/after.** For each of the 5 fixtures, name 2–3 specific URLs that disappeared and 2–3 that appeared in top-15. Pay particular attention to the q3-style long-question case: did keyword distillation actually unlock PubMed?
   - **Gemma vs prompt.** Specific instances where Gemma followed the prompt and where it didn't. This is the most novel signal in this phase — Phase 6 had no model-in-loop, so prompt-following data is a new artifact.
   - **Anything surprising.** Mutations that the agent expected to help and didn't, or vice versa.

7. **Write the downstream-orchestration follow-ups.** Append to `RESULTS.md` a section "Implications for Phase 4/7/9 orchestration" — derived from specific iteration rows, not generic priors. Examples of the *kind* of insight that should land here (don't fabricate — derive each one from what the iteration actually surfaced):
   - "the keyword-distillation prompt unlocks PubMed for q3-shape long questions but Gemma 26B emits natural-language ~30% of the time despite explicit instructions — Phase 4's `propose_branches` should pre-distill questions into keyword form before passing to expand, rather than relying on expand to do both jobs."
   - "alternating routing (default, science, default, science) with n=4 dominates per-engine pinning at the same n — the science engines duplicate each other's coverage when called individually, so per-engine pinning is wasted budget."
   - "engine-pinning to `engines=pubmed` for one expansion makes NCBI rate-limit on the second fixture query in a row — Phase 4's per-round dispatch needs ≥1s gap between PubMed-bound queries, or use a registered NCBI key."
   - "dropping the seed for long-question fixtures (D=False) made q3 results strictly better; Phase 4's branch-proposal output should similarly avoid carrying forward the seed verbatim into per-branch search calls when the seed is multi-clause."
   These are *follow-ups for the next planning turn*, not implementation tasks for this phase.

### Acceptance criteria

- `lib/expand.py` and `lib/pipeline.py` reflect the winning configuration. The `SCHOLARLY_MODE` env-flag code path is fully removed; `EXPAND_ROUTING` (or the agreed equivalent) is the new routing surface, with the winning routing as the no-env-var default.
- `iterations.jsonl` contains ≥20 iterations, each with `expansions_per_query`, `top_ranked_per_query`, `axis_touched`, `mutation_summary`, `rationale`, `kept_or_reverted`. The final row carries a `stop_reason` field. (≥20 not ≥30 — context-window stop is allowed to fire earlier.)
- The agent's end-of-loop summary in `RESULTS.md` calls out at least one fixture where the winning expansion config produced a qualitatively better top-15 than the iter-0 baseline (specific URLs cited).
- `RESULTS.md` exists with: per-iteration trajectory, per-axis takeaway, top-N kept knobs, per-fixture before/after, Gemma-vs-prompt observations, and ≥3 concrete downstream-orchestration follow-ups derived from specific iteration rows.
- The harness is throwaway; `iterate.py` and `iter_config.json` may stay in-tree for reproducibility but are not imported by anything in `lib/`.
- Existing tests still pass (`python3 -m pytest tests/test_research.py tests/test_agent_sh.py`). If a Phase 5 test asserts the `SCHOLARLY_MODE` flag exists, update or remove that assertion as part of the port — this is intentional behaviour change, not regression.

### Notes

- Holds Phase 5's source-priors and Phase 6's SearXNG config constant. If a Phase 6.5 win conflicts with a Phase 5/6 default (e.g., the winning prompt makes `boost_domains` mostly redundant), record it in `RESULTS.md` as a follow-up; do not retroactively edit Phase 5/6 in this phase.
- The agent IS the judge. There is no auto-labeler, no score number gating decisions, and no LLM rubric classifier. The novel signal vs Phase 6 is **what the actual local model produces under each prompt** — `expansions_per_query` is the row field that captures this.
- Iteration record is the audit trail. `iterations.jsonl` should be readable end-to-end as the trajectory; if a future plan revisits expansion tuning, it should be able to reconstruct the trajectory from prose rationales + the `expansions_per_query` and `top_ranked_per_query` lists.
- Out of scope for this phase (deferred to future planning):
  - Per-engine query formulation (sending different `q=` strings to different engines from the same expansion). User explicitly scoped this out for now; revisit if Phase 9 regression eval shows engine-side query-shape gaps that per-expansion routing alone doesn't close.
  - Trained query-quality classifier / learned routing.
  - Multi-turn expansion (expand → critique → re-expand).
  - Gemma-31B vs Gemma-26B comparison on expansion (this is the **B/Lever B from Phase 5** axis, already characterised).
- Context-window discipline: the agent should treat each iteration's `Read`/`Edit`/`Bash` triplet as the unit, summarise findings into the row's `rationale` field rather than into the conversation, and stop the loop early if context starts feeling tight. Better to land 20 well-recorded iterations + a clean port + RESULTS.md than 35 iterations and no port.
