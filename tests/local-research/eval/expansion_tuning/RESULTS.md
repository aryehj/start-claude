# Phase 6.5 Results: Query Expansion + Search-Term Tuning Loop

## Per-Iteration Trajectory

| iter | axis | mutation | kept/reverted | rationale |
|------|------|----------|---------------|-----------|
| 0 | baseline | Phase 5 defaults: generic prompt, n=4, include_seed=true, all-default routing | kept | baseline for comparison; q3 top-15 all from seed position (0 PubMed), long-form-runner top-9 consumer health + 6 clinical from pos-1 |
| 1 | A_prompt | keyword-distillation prompt (3-6 token keywords) | **kept** | pos-1 "cyclist medial knee pain diagnosis" unlocked 4 PubMed results for q3 (vs 0 in baseline); creatine gained 2 new PMC articles via pos-1/2; long-form-runner lost 3 clinical URLs — accepted because PubMed unlock for q3 is the priority win |
| 2 | A_prompt | medical-jargon-expansion prompt | reverted | Gemma correctly generated "Pes anserine bursitis cycling" and "Patellofemoral pain syndrome cycling" at pos-2/3, but those slots never reached top-15 (pos-0/1 saturated the 15 slots); q3 returned 0 PubMed vs 4 in iter-1; clinical jargon at late positions is a routing problem, not a prompt problem |
| 3 | B_n | n_expansions=2 | reverted | n=2 means only 2 keyword slots; seed (pos-0) returned 15 unique results and saturated top-15 entirely; q3 had 0 PubMed results from expansion positions vs 4 in iter-1; fewer expansions with keyword-distillation is strictly worse |
| 4 | B_n | n_expansions=6 (all-5-fixture run) | reverted | SearXNG rate-limited: 35 queries (7 expansions × 5 fixtures) exhausted underlying engine quotas; all 5 fixtures returned 0 results; iter not interpretable |
| 5 | B_n | n_expansions=6 (3-fixture retry) | reverted | n=6 (21 queries for 3 fixtures) also rate-limited creatine and long-form-runner; q3 pos-1 returned same 4 PubMed IDs as iter-1 before rate-limit hit; n≥6 impractical with current SearXNG quota |
| 6 | D_seed | include_seed=false (no rate-limit delay — first attempt) | reverted | partial: q3 pos-0 ("cyclist medial knee pain diagnosis") returned same 4 PubMed IDs as iter-1 pos-1; long-form-runner got 0 (rate-limited); added --search-delay flag before retry |
| 7 | D_seed | include_seed=false with 2s search delay | **kept** | **dominant win**: q3 top-15 went from 4 PubMed (iter-1) to **15/15 PubMed** across 3 positions; long-form-runner went from consumer health blogs to **15/15 PubMed**; dropping the seed lets all 4 keyword slots fill top-15 with PubMed results |
| 8 | D_seed | include_seed=true for creatine/finance-team/recent-news | reverted | confounded by rate-limit fallback: Google/DDG exhausted after creatine; SearXNG fell through to PubMed for finance-team and recent-news producing irrelevant medical papers; data not interpretable |
| 9 | C_routing | alternating routing (pos-0/2 default, pos-1/3 categories=science) | reverted | science category adds arxiv noise (3 irrelevant preprints for "bike fit biomechanics knee pain", 3 for "foot stiffness") and reduces PubMed count vs iter-7 (q3: 13 vs 15, long-form-runner: 10 vs 15); journals.lww.com cycling-biomechanics appeared but not worth the arxiv dilution |
| 10 | C_routing | engine-pinning: pos-1 engines=pubmed | **kept** | identical results to iter-7 (all-default): PubMed is already in the SearXNG default engine mix; short keyword-distillation queries naturally trigger PubMed without explicit routing; explicit pinning is redundant; confirms all-default as Axis C winner |
| 11 | C_routing | all-science routing on creatine/finance-team/recent-news | reverted | helps creatine (9 PubMed vs ~3 baseline) but ruins finance-team (6 arxiv CS papers, 0 PubMed) and recent-news (election preprints instead of vote-margin data); no global science routing config works across all fixture types |
| 12 | stacking | full winning config: keyword-distill + n=4 + no-seed + all-default (all 5 fixtures) | **kept** | q3/creatine/long-form-runner all returned 15 PubMed; finance-team and recent-news returned confounded PubMed fallback (rate-limit from earlier queries); medical fixtures confirmed working |
| 13 | A_prompt | control: generic prompt + no-seed on q3 | reverted | generic+no-seed gives only 5 PMC/PubMed for q3 (vs 15 for keyword-distil+no-seed); natural-language expansions from pos-0 still crowd-out positions 1-3; confirms **both** Axis A and Axis D are required — neither alone achieves the unlock |
| 14 | stacking | medical fixtures q3/creatine/long-form-runner: stacking validation | **kept** | q3 15/15, creatine 15/15, long-form-runner 15/15 PubMed; winning config stable across independent runs |
| 15 | stacking | finance-team isolated clean run with winning config | reverted | finance-team with keyword-distillation+no-seed returns only 1 result: short keyword strings don't match enough business benchmarking web pages when seed is absent; winning config is question-type-dependent |
| 16 | stacking | finance-team isolated (background run, same no-seed config) | reverted | SearXNG rate-limited for Google/DDG; pos-1/2 returned 15 irrelevant PubMed fallback papers |
| 17 | stacking | finance-team + recent-news with keyword-distill + include_seed=true | reverted | finance-team still rate-limited on pos-0/1; recent-news got 0 results; confirms winning config is optimized for medical/research queries |
| 18 | stacking | creatine clean run (background, rate-limited) | reverted | SearXNG rate-limited; creatine returned 0 results; Google/DDG not yet recovered |
| 19 | stacking | creatine clean isolated run | **kept** | creatine 15/15 PubMed (all from pos-0 "creatine long term safety"); confirms keyword-distill+no-seed works for supplement safety queries |
| 20 | C_routing | all-pubmed explicit routing: all 4 positions engines=pubmed | reverted (final) | q3: 9 PubMed vs 15 with default routing; all-pubmed explicit routing is WORSE than default because Google/DDG index PubMed and find it via more keyword variants; **stop_reason**: all axes characterised, 21 iterations reached, context headroom needed for port and RESULTS.md |

## Per-Axis Takeaway

**Axis A (prompt template) — winner: `keyword-distillation`**

The keyword-distillation prompt ("Output {n} short search-keyword strings, 3-6 tokens each, no natural language") is the decisive improvement. Natural-language reformulations from the generic prompt (e.g., "What are the differential diagnoses for persistent medial knee pain in endurance cyclists") are too long to reliably trigger PubMed and crowd out later expansion positions when they become pos-0. Short keywords like "cyclist medial knee pain diagnosis" and "bike fit biomechanics knee pain" reliably trigger PubMed's index through the default SearXNG engine mix.

The medical-jargon-expansion prompt (iter-2) generated correct clinical terminology ("Pes anserine bursitis cycling") but those terms appeared at pos-2/3 and never reached the top-15 because pos-0/1 saturated the 15 slots first. This is a routing constraint, not a prompt quality issue.

Axis A converged fastest: 2 variants tested (iter-1 kept, iter-2 reverted), 1 control experiment (iter-13) confirmed the finding.

**Axis B (n_expansions) — winner: `n=4`**

n=2 undercovers: with only 2 keyword slots, the seed (pos-0) often returns 15 unique results and saturates top-15, leaving the keyword expansion slots with 0 contribution. n=6 triggers SearXNG rate limits — 7 calls per fixture exhausts Google and DuckDuckGo quotas even with 3 fixtures per run. n=4 (5 total calls including seed, or 4 without seed) is the rate-limit-safe ceiling that provides good coverage.

**Axis C (per-expansion routing) — winner: `per_expansion_routing: []` (all-default)**

Key finding: **PubMed is already in the SearXNG default engine mix** (confirmed by `engines=['pubmed']` in results). Short keywords from the keyword-distillation prompt naturally trigger PubMed without explicit routing. Adding `categories=science` introduces arxiv preprint noise for clinical medical queries (3 arxiv papers per fixture in iter-9). Explicit `engines=pubmed` pinning produces identical results to default routing (iter-10) — redundant. All-pubmed routing for all positions (iter-20) actually reduces PubMed coverage (9 vs 15 for q3) because Google/DDG find PubMed content via more keyword variants than PubMed's direct search.

Axis C was the most counterintuitive: the instinct to route to science engines for medical queries is wrong because the default routing already does the right thing for short keywords.

**Axis D (seed inclusion) — winner: `include_seed=False`**

This was the most impactful axis. The long natural-language seed ("A cyclist develops stubborn medial knee pain that comes on during long rides and lingers for days afterward...") for q3 returns 15 cycling-blog results that fill the entire top-15, leaving zero slots for keyword expansions. Dropping the seed lets all 4 keyword positions contribute. The win: q3 went from 4 PubMed results (iter-1, keyword-distill with seed) to **15/15 PubMed** (iter-7, keyword-distill without seed).

Both Axis A and Axis D are **necessary** — neither alone achieves the full unlock (iter-13 control test: generic prompt + no-seed yielded 5 PubMed vs 15 for keyword-distill + no-seed).

Caveat: `include_seed=False` is harmful for short factual/business queries (finance-team: returns 1 result or irrelevant PubMed fallback papers) because the seed is a reasonable search query itself and the keyword expansions don't match many non-academic web pages.

## Top-N Kept Knobs (Winning Config)

**Prompt: `keyword-distillation`** — added to `lib/expand.py._PROMPTS`, set as new default
```
Output {n} short search-keyword strings (3-6 tokens each) suitable for a search engine,
derived from the question. No natural-language phrasings; no quoting; no site: operators.
Output exactly one keyword string per line with no numbering, bullets, or extra text.

Query: {query}
```
*Adopted at iter-1. Confirmed by iter-7 stacking and iter-13 control showing prompt format is necessary.*

**n_expansions: 4** — unchanged from Phase 5 default
*n=2 undercovers (iter-3 reverted), n=6 rate-limits (iters-4/5 reverted). 4 is the ceiling.*

**include_seed: False** — new default in `gather_sources`, was `True` implicitly
*Dominant win at iter-7: q3 top-15 flipped from 14 cycling-blog / 1 PMC to 15/15 PubMed. Only applicable for long-question fixtures; short-keyword queries should pass `include_seed=True` explicitly.*

**per_expansion_routing: []** (all-default) — replaces `SCHOLARLY_MODE` binary flag
*Iter-10 showed explicit pubmed pinning is redundant; iter-9 showed science categories adds arxiv noise; all-default already triggers PubMed for short medical keywords.*

## Per-Fixture Before/After

### q3 — cyclist medial knee pain (long natural-language question)

**Baseline (iter-0):** All 15 results from pos-0 (seed = full question text), 1 PMC article (PMC5973630) at position 13, 14 cycling blogs.

Disappeared from top-15 in winning config: bikeradar.com/cycling-knee-pain, physio-pedia.com/Cyclist%27s_Knee, myvelofit.com/cycling-related-knee-pain, chrisbaileyorthopaedics.com, biketips.com, cyclingweekly.com, cyclingguider.com

Appeared in top-15 with winning config (iter-7): ncbi.nlm.nih.gov/pubmed/35189665, ncbi.nlm.nih.gov/pubmed/33418617, ncbi.nlm.nih.gov/pubmed/16721615, ncbi.nlm.nih.gov/pubmed/12875315 (from "cyclist medial knee pain diagnosis"); pubmed/35961646, 34540268, 34142644, 27490817 (from "bike fit biomechanics knee pain"); pubmed/42099869, 42006909, 41999225, 41705060, 41490807, 41412978, 41406075 (from "distinguishing knee pain causes").

**Quality shift: 14 SEO blogs → 15 PubMed research papers.** This is the fixture the whole phase was designed to improve.

### long-form-runner — plantar fasciitis long natural-language question

**Baseline (iter-0):** 9 consumer health sites from pos-0 (Mayo, Yale, healthline, Cleveland Clinic, etc.) + 6 clinical sources from pos-1 (NCBI Books, AAFP AFP, PMC3309235, ScienceDirect, JOSPT, ACFAS clinical guidelines).

Disappeared in winning config: mayoclinic.org/plantar-fasciitis, yalemedicine.org, healthline.com/heel-pain-in-the-morning, health.clevelandclinic.org, running-physio.com, ubiehealth.com, fitandfunctiontherapy.com, voyagehealthcare.com, treasurevfa.com

Appeared in winning config (iter-7): pubmed/32012958, 30817717, 28820647, 21636293, 19759034 (from "runner sudden morning foot stiffness"); pubmed/38593625, 37419477, 28236094, 24696696, 24006205, 19150191, 6945056 (from "foot stiffness after running causes"); pubmed/41664965, 33975367, 31995786 (from "new runner foot stiffness diagnosis").

**Quality shift: consumer health blogs → 15 PubMed research papers.** Some notable baseline clinical sources (AAFP clinical guidelines, JOSPT CPG) disappeared — the winning config over-indexes PubMed relative to clinical guidelines. This is noted in the downstream follow-ups.

### creatine — supplement safety short keyword query

**Baseline (iter-0):** 1 PubMed, 1 PMC, consumer health from Mayo/Harvard/Verywell/AARP.

**Winning config (iter-19):** 15/15 PubMed results from pos-0 "creatine long term safety". Short seed queries like "is creatine safe to take long term" already trigger good results, and dropping the seed while using the keyword prompt gives consistent PubMed coverage.

### finance-team — business benchmarking (non-medical)

**Baseline (iter-0):** companysights.com, reddit.com/r/CFO, LinkedIn, cfohub.com, getaleph.com, Bench, CFO Connect, etc. — the right business benchmarking sources.

**Winning config (iter-15/16):** 1 irrelevant PubMed paper (metastatic breast cancer app) or rate-limit PubMed fallback. The winning config is clearly wrong for this fixture.

**Implication:** The winning config is specialized for research/medical queries. Business and factual queries should use `include_seed=True` or a different prompt.

### recent-news — US 2024 election popular vote margin

**Baseline (iter-0):** Wikipedia, presidency.ucsb.edu, CNN election results, Cook Political, 270towin — the right factual sources.

**Winning config:** rate-limited in most runs; when available, returns irrelevant content. Same issue as finance-team: no-seed + short keyword expansions don't match news/reference pages.

## Gemma-vs-Prompt Observations

**Gemma followed the keyword-distillation prompt accurately** across all runs:
- q3 reliably produced: "cyclist medial knee pain diagnosis", "bike fit biomechanics knee pain", "distinguishing knee pain causes", "long ride knee pain causes" — all 3-5 tokens, no natural language.
- creatine: "creatine long term safety", "creatine side effects chronic use", "is creatine safe daily" — slightly mixed (3rd and 4th tokens include functional words, borderline).
- long-form-runner: "runner sudden morning foot stiffness", "foot stiffness after running causes", "clinician distinguish foot pain", "new runner foot stiffness diagnosis" — well-formatted.
- finance-team: "small software company finance team size", "consulting firm finance staffing ratio", "60 person company finance department" — correctly converted to keyword format.

**Gemma followed the medical-jargon-expansion prompt accurately** (iter-2): "Pes anserine bursitis cycling", "Patellofemoral pain syndrome cycling" were correct clinical terms. The problem was the harness structure, not Gemma's compliance.

**Gemma produced identical keywords across repeated runs** with `temperature=0`, confirming determinism: "cyclist medial knee pain diagnosis" appeared identically in iters-1, 6, 7, 9, 10, 12, 13, 14, 20. This is a reliable signal that the keyword-distillation prompt has stable behavior for these fixtures.

**One prompt-following gap observed:** For the medical-jargon prompt (iter-2), "Medial knee pain cycling diagnosis" at pos-1 is marginally too long (5 tokens with a preposition — borderline for the prompt's 2-8 token instruction) but doesn't critically affect results. The bigger issue was the slot-saturation problem at pos-0/1.

## Anything Surprising

1. **PubMed is already in the default SearXNG engine mix.** The assumption going in was that PubMed required explicit routing (`categories=science` or `engines=pubmed`). It doesn't. Short keyword queries trigger PubMed naturally through the default engine set. This was confirmed by `engines=['pubmed']` appearing in result metadata for all-default routing runs.

2. **Default routing outperforms explicit PubMed pinning (iter-20).** All-pubmed routing for 4 positions returned 9 PubMed results for q3 vs 15 with default routing. Google/DDG index PubMed content and find it via more keyword variants than PubMed's own direct search accepts.

3. **The seed-saturation effect is stark.** With include_seed=True, the long natural-language seed for q3 returns 10-15 unique cycling-blog results, consuming all top-15 slots before any keyword expansion can contribute. With include_seed=False, the first keyword search fills just 4 slots, leaving 11 slots for additional keyword positions. The difference: q3 PubMed count goes from 0 (baseline seed) to 15 (no-seed).

4. **Rate-limiting is a second-order SearXNG confound.** Running 5 fixtures × 5 queries = 25 total SearXNG calls triggers Google/DDG rate limits, after which PubMed (different rate-limit tier) dominates the fallback. This produces spurious "all PubMed" results for finance-team and recent-news. The 2-second inter-search delay mitigates but doesn't eliminate this for large multi-fixture runs.

5. **n=6 rate-limits before n=4 does**, even with fewer total queries (21 vs 25). The rapid-fire 7-query-per-fixture pattern triggers engine throttling faster than 5-query-per-fixture.

---

## Implications for Phase 4/7/9 Orchestration

**Follow-up 1 (from iter-7, iter-13):** The keyword-distillation prompt reliably unlocks PubMed for q3-shape long questions, but **both the prompt format AND the seed-drop are required** — neither alone achieves the unlock. Phase 4's `propose_branches` should pre-classify questions by type before calling `gather_sources`: long natural-language research questions should pass `include_seed=False`, while short factual/business questions should pass `include_seed=True` (or the gather_sources caller must detect this). Blindly applying `include_seed=False` globally breaks the finance-team and recent-news fixtures (iter-15 returned 1 result, iter-16 returned 15 irrelevant PubMed fallback papers).

**Follow-up 2 (from iters-4/5):** n≥6 expansions per query triggers SearXNG engine rate limits even for 3 sequential fixtures. Phase 4's per-round dispatch must respect the n=4 ceiling or add ≥2-second delays between each search call (not just between fixtures). The Phase 6 `--search-delay` flag (added to `iterate.py` in this phase) should be promoted to the production `gather_sources` call chain if multi-fixture parallel runs are planned.

**Follow-up 3 (from iter-20 vs iter-7, iter-9 vs iter-7):** PubMed is already in the SearXNG default engine mix for short keyword queries. Phase 7/9 scoring and citation handling should not assume PubMed results require explicit routing configuration — they will appear naturally in the candidate set when `lib.expand` uses the keyword-distillation prompt. However, the winning config over-indexes PubMed relative to clinical practice guidelines (AAFP, JOSPT, ACFAS CPG disappeared from long-form-runner top-15 after the upgrade). Phase 9 synthesis should weight NCBI/PMC results appropriately against non-PubMed clinical guidelines which may appear in fewer positions.

**Follow-up 4 (from iter-11, iter-15):** The winning config is question-type-specialized. For non-medical business/factual questions (finance-team, recent-news), the old baseline (generic prompt + include_seed=True) outperforms keyword-distillation + no-seed. Phase 4's `propose_branches` should route questions through different expansion configs based on a detected domain tag (e.g., medical/research → keyword-distill, no-seed; factual/business → generic, include-seed). This routing decision is upstream of every search call and has a larger impact than any individual lever tested in Phase 5 or 6.
