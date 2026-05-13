# Phase 6.6: Question-type classifier + per-type expansion config

This is a single-phase plan that adds **Phase 6.6** to `local-research-harness.md`, between Phase 6.5 (expansion tuning — done) and Phase 7 (synthesis). After landing, the implementer should also append a `- [x] Phase 6.6: ...` line to that plan's Status checklist.

## Status

- [ ] Phase 6.6: question-type classifier + per-type expansion config

<!-- mark [x] when complete. No effort annotation: prompt wording (classifier + per-type configs) is the product and materially affects downstream quality. -->

## Context

Phase 6.5 landed `keyword-distillation` and `include_seed=False` as the new defaults in `lib/expand.py:57-64` and `lib/pipeline.py:24`. Per `tests/local-research/eval/expansion_tuning/RESULTS.md`:

- **Medical/research fixtures improved dramatically.** q3 (cyclist medial knee pain) went from 0 → 15/15 PubMed in top-15; long-form-runner (plantar fasciitis) went from consumer health blogs → 15/15 PubMed; creatine stayed at 15/15 PubMed.
- **Non-research fixtures regressed.** finance-team (business benchmarking) returns 1 result with the new defaults — short keyword expansions don't match business pages once the seed is dropped. recent-news (US 2024 election vote margin) returns irrelevant content or rate-limit-induced PubMed fallback. Baseline (generic prompt + include_seed=True) was correct for both.

RESULTS.md Follow-up 1 and Follow-up 4 are explicit: the winning config is question-type-specialized, and orchestration must pre-classify questions and route to per-type expansion configs before reaching `gather_sources`. Without this, the harness is partly broken for ~40% of the fixture set Phase 9 will eventually regress against.

This phase is upstream of every search call and has a larger impact than any individual lever tested in Phase 5 or 6.5. It lands before Phase 7 (synthesis) so the synthesis layer sees a candidate set that's appropriate to the question type.

## Goals

- Restore finance-team and recent-news baseline source-set quality without losing q3 / creatine / long-form-runner gains from Phase 6.5.
- Add `lib/classify.py` — a stdlib + omlx-only module that calls `EXPAND_MODEL` with `temperature=0` and returns a `{label, config}` dict from a small fixed taxonomy.
- Route each per-branch query through the classifier before expansion, so different branches in the same session can use different expansion configs.
- Cover the classifier behaviour with mocked unit tests and at least one omlx-backed integration test against the four representative fixtures.

## Approach

The classifier sits at the `gather_sources` call site, not inside it. `lib/orchestrate.py:48-64` and `lib/branch.py:30-59` already iterate over branch queries; this phase adds one classification call per branch, then threads the resulting config (`prompt_name`, `include_seed`) into a thin `gather_sources` signature extension. Keeping classification out of `gather_sources` itself preserves its testability and matches how `EXPAND_ROUTING` was wired in Phase 6.5 (env-var read inside the function, but per-call shape).

The classifier is **LLM-based** (Gemma via `EXPAND_MODEL` — already used for expansion, no new model dependency). Heuristic classification was considered and rejected because the four representative fixtures span vocabularies that overlap (e.g., "creatine" + "long-term safety" looks medical but is also a short consumer question, while q3's long natural-language question doesn't contain unambiguous research-vocabulary triggers). Gemma at `temperature=0` is deterministic per query and the classification prompt is one-shot — adds ~1s per branch, negligible compared to search + fetch latency.

The taxonomy starts at **two labels** (`research`, `general`) — the minimum that maps to the two distinct winning configs from Phase 6.5. The implementer should add `factual` or `business` as separate labels only if the 2-class version doesn't separate finance-team from recent-news cleanly during regression. Smaller taxonomies are easier for small models to follow reliably.

## Unknowns / To Verify

1. **Gemma's classification reliability under `temperature=0` for the four fixture types.** Whether the small 8-bit Gemma-E4B used for expansion can hold a 2- or 3-label classification cleanly across the fixture range. Verify in Step 4 by running `classify()` against q3, creatine, long-form-runner, finance-team, recent-news and checking that labels match expectations. If Gemma over-labels everything `research`, expand the taxonomy or harden the prompt. Affects Step 1 and Step 4.

2. **Whether `lib/expand.py`'s env-var-driven prompt selection (`EXPAND_PROMPT_NAME`) should remain alongside a new per-call `prompt_name` arg.** Phase 6.5's tuning harness depends on the env var; production should use the per-call arg. Recommend keeping both — per-call arg takes precedence when present, env var is the fallback default. Affects Step 2.

3. **Whether to classify the seed query once at session start vs. per-branch.** Per-branch is safer (branches drift from the seed type as rounds accumulate — a research seed can produce a factual sub-question) but adds one omlx call per branch per round. Recommend per-branch; revisit only if Phase 9 latency budgets bite. Affects Step 3.

---

## Phase 6.6: Question-type classifier + per-type expansion config

### Steps

1. **Add `lib/classify.py`.** Module-level constants for: the classification prompt template, the label enum (`research`, `general` to start), and the per-label config dict mapping each label to `{prompt_name, include_seed, n_expansions}` derived from Phase 6.5 winners:
   - `research` → `{prompt_name: "keyword-distillation", include_seed: False, n_expansions: 4}`
   - `general` → `{prompt_name: "generic", include_seed: True, n_expansions: 4}`

   Public function: `classify(query: str) -> dict` returning `{"label": str, "config": dict}`. Calls `omlx.chat(EXPAND_MODEL, ..., temperature=0)` with a prompt that asks for one of the labels and parses the first whitespace-stripped token. Unknown / malformed output falls back to `research` (the Phase 6.5 default — biased toward the documented win for the longest-tail fixture shape rather than the conservative pre-6.5 baseline).

2. **Extend `lib/expand.py:expand()` to accept a per-call `prompt_name` override.** Current signature is `expand(query: str, n: int = 4) -> list[str]`. New: `expand(query, n=4, prompt_name: str | None = None)`. When `prompt_name` is provided, it takes precedence over the module-level `_ACTIVE_PROMPT` (which keeps the existing env-var behaviour for the tuning harness). Update the docstring's prompt-selection priority order to reflect the new `prompt_name` arg as priority 0.

3. **Extend `lib/pipeline.py:gather_sources()` to accept `prompt_name`.** Current signature already has `include_seed`; add `prompt_name: str | None = None` and thread it into the `expand.expand()` call. Default `None` so existing callers and tests continue to work. Do not call `classify()` inside `gather_sources` — keep it pure.

4. **Wire classification into `lib/orchestrate.py`.** In the branch-iteration loop at `orchestrate.py:55-64`, call `classify(b["query"])` once per branch, then pass the resulting config into `gather_sources`:
   ```python
   cls = _classify_mod.classify(b["query"])
   result = _pipeline_mod.gather_sources(
       b["query"],
       exclude_urls=state.seen_urls,
       prompt_name=cls["config"]["prompt_name"],
       include_seed=cls["config"]["include_seed"],
       n_expansions=cls["config"]["n_expansions"],
   )
   ```
   Log the classified label per branch alongside the existing `branch <q>: N ranked` line so a session transcript shows which config each branch used.

5. **Tests — unit (mocked).** Add `tests/local-research/test_classify.py`:
   - Mock `omlx.chat` to return `"research"`, `"general"`, `"  research  \n"`, `"unknown_label"`, and `""`. Assert the label is parsed correctly, whitespace-tolerant, and that malformed output falls back to `research`.
   - Assert the per-label config dict has all three keys for every label in the taxonomy (defensive against partial updates later).
   - Determinism: assert `temperature=0` is passed to `omlx.chat`.

6. **Tests — integration (omlx-backed, gated).** Add a single test in `test_classify.py` marked with `@pytest.mark.integration` or guarded by `pytest.importorskip` + `OMLX_BASE_URL` env presence, that calls real `classify()` against the four representative fixtures from `tests/local-research/eval/expansion_tuning/` and asserts:
   - q3 (`"A cyclist develops stubborn medial knee pain..."`) → `research`
   - creatine (`"Is creatine safe to take long term"`) → `research`
   - long-form-runner (long plantar-fasciitis question) → `research`
   - finance-team (business benchmarking question) → `general`
   - recent-news (election vote margin question) → `general`

   If Gemma misclassifies any fixture, address per Unknown #1: harden the classification prompt or expand the taxonomy. Do not silently lower test expectations.

7. **Update `tests/local-research/test_source_bias.py`** to reflect that `include_seed` and `prompt_name` now arrive via classify() in production. Existing tests that directly invoke `gather_sources` keep working (the new args default to `None`); add at least one test that mocks `classify()` and asserts the right `prompt_name` / `include_seed` flow through to the `expand` and `search` mock calls.

8. **Regression: run the four fixtures end-to-end** against the full pipeline with classification wired in. Compare top-15 URLs against the Phase 6.5 winning state captured in `RESULTS.md`:
   - q3 / creatine / long-form-runner: top-15 PubMed counts must hold (15/15, 15/15, 15/15).
   - finance-team: top-15 must contain ≥5 of the Phase 5 baseline business sources (companysights.com, cfohub.com, getaleph.com, etc.).
   - recent-news: top-15 must contain ≥5 reference / news sources (Wikipedia, presidency.ucsb.edu, CNN election results, Cook Political, 270towin), not PubMed.

   Record the run output as `tests/local-research/eval/expansion_tuning/classify-regression.md` (short — table of fixture × label × top-15-summary). This is the audit trail equivalent to Phase 6.5's `RESULTS.md` for this phase.

### Acceptance criteria

- `classify()` is deterministic (same input → same label) when called repeatedly against the same omlx server.
- `general`-classified queries route to `generic` prompt with `include_seed=True`; `research`-classified queries route to `keyword-distillation` with `include_seed=False`.
- The four representative fixtures classify as documented in Step 6, end-to-end.
- finance-team and recent-news no longer return PubMed-fallback or single-result top-15.
- q3, creatine, long-form-runner retain their Phase 6.5 PubMed-rich top-15.
- `classify-regression.md` exists and documents the per-fixture before/after.

---

## Notes

- **Scope discipline.** This phase deliberately does not touch rate-limit hardening (Phase 6.5 Follow-up 2) or the PubMed-vs-clinical-guidelines reweighting concern (Phase 6.5 Follow-up 3). Both are real but separable; Follow-up 2 is a Phase 9 / `--batch` concern, and Follow-up 3 is a synthesis-weight concern that belongs near Phase 7.
- **Taxonomy growth path.** If the 2-class version classifies cleanly on the four fixtures but later fixtures (Phase 9's q1–q6 regression set) split poorly, the natural expansion is `factual` (short reference queries like recent-news) and `business` (professional/operations queries like finance-team). Add labels in `lib/classify.py` first; add corresponding entries to the per-label config dict; only touch the classification prompt if the model needs explicit examples per label.
- **Why classify per-branch, not per-session.** Branches drift from the seed: a `research` seed about cycling knee pain can produce a follow-up branch like "common knee-pain self-diagnosis guides" that's general/factual in shape. Classifying once at session start would lock the wrong config for that branch. Per-branch classification adds ~1s per branch (negligible vs search + fetch).
- **`SCHOLARLY_MODE` was already removed in Phase 6.5.** `EXPAND_ROUTING` (per-position routing via env var) still exists in `lib/pipeline.py:53-57` but the Phase 6.5 winner is the empty list (`[]`). This phase leaves `EXPAND_ROUTING` alone — per-type routing is a separate concept from per-expansion-position routing, and Phase 6.5's data showed all-default routing wins for both research and general queries.
- **Holding Phase 6.5 wins constant.** This phase changes the *upstream* config-selection layer, not the prompts themselves. If a Phase 6.6 finding suggests the keyword-distillation prompt itself needs revision, capture it in `classify-regression.md` as a follow-up and do not retroactively edit Phase 6.5.
