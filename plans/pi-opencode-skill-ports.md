# Pi / OpenCode Skill Ports (scope, build, wrap)

## Status

- [x] Phase 1: Validate that an adapted SKILL.md actually works on Gemma-class models via Pi
- [x] Phase 2: Port the remaining two skills using the validated pattern
- [x] Phase 3: Seed the ports into new sandboxes via `start-agent.sh`

## Context

The repo's three core dev skills live at `skills/{scope,build,wrap}/SKILL.md` (mirror of `~/.claude/skills/`). They were written for Claude Code + Sonnet/Opus and lean on Claude-Code-only tools (`AskUserQuestion`, `Agent`, `TaskCreate`/`TaskUpdate`).

Pi (`@earendil-works/pi-coding-agent`, README at `/usr/lib/node_modules/@earendil-works/pi-coding-agent/README.md`) and OpenCode both implement the [Agent Skills](https://agentskills.io) standard and read `SKILL.md` files directly. Read paths:
- **Pi:** `~/.pi/agent/skills/`, `~/.agents/skills/`, `.pi/skills/`, `.agents/skills/`
- **OpenCode:** `~/.config/opencode/skills/`, `~/.claude/skills/`, `~/.agents/skills/` (per `https://opencode.ai/docs/skills/`)

`~/.agents/skills/` is the path both tools share — the natural home for a single shared variant.

Target models are Gemma-3-27B-class — substantially weaker than Sonnet, with ~128k usable context. The current skills assume strong meta-cognition ("identify the most consequential assumption," "restructure around uncertainty," "decorative phases" diagnosis) that these models won't execute reliably.

`start-agent.sh` already seeds Claude Code skills into new sandboxes per ADR-005; this plan extends that pattern.

## Goals

- One shared set of small-model SKILL.md files at repo path `skills-agents/{scope,build,wrap}/SKILL.md`, loadable by both Pi (`/skill:scope`) and OpenCode (via the `skill` tool).
- Content adapted: meta-reasoning steps cut, judgment-heavy branches collapsed to literal rules, prose trimmed, Claude-Code-only tool references replaced with inline equivalents.
- Task tracking via a TODO file the model edits, not via the missing `TaskCreate` tool.
- `start-agent.sh --init-sandbox` seeds these into `$SANDBOX/.sandbox_config/agents/skills/`, bind-mounted to `/root/.agents/skills/` so both Pi and OpenCode find them.
- One phase actually validated end-to-end on Gemma via Pi before the other two are written.

## Approach

The motivation is content adaptation, not format translation — both target tools already read SKILL.md natively. So the work is mostly editorial: strip Claude-isms, collapse judgment-heavy branches into literal rules, swap missing tools for file-based equivalents. The risk is whether a Gemma-class model can actually execute even the adapted skills usefully; that risk dominates the others, so Phase 1 produces one skill (scope, the most judgment-heavy and highest-risk) and validates it on real Pi+Gemma traffic before the rest of the work commits to the adaptation pattern.

Sharing one variant between Pi and OpenCode is viable because both implement the Agent Skills standard. Per-tool deviations get handled with one-line conditionals in the skill body, not separate files.

## Unknowns / To Verify

1. **OpenCode `~/.claude/skills/` double-load.** OpenCode reads both `~/.claude/skills/` and `~/.agents/skills/`. If both contain `scope/`, OpenCode may list both, confusing the model. Verify by dropping a same-named SKILL.md in each and running `opencode` — check the startup skill listing. Affects Phase 3 (seeding paths). If OpenCode dedupes by name, no action; if it lists both, either rename the small-model variant (`scope-small`) or gate OpenCode away from `~/.claude/skills/` via config.
2. **Frontmatter compatibility.** Claude Code's SKILL.md frontmatter uses `disable-model-invocation`, `model`, `argument-hint`, `allowed-tools`. The Agent Skills standard (per OpenCode docs) only specifies `name`, `description`, `license`, `compatibility`, `metadata`. Pi's behavior on unknown fields is undocumented. Verify by loading a SKILL.md with the Claude-Code-only fields in `pi --skill <path>` and watching for warnings; if Pi rejects or mishandles, strip to the standard fields. Affects Phase 1.
3. **Pi `/skill:name` argument passing.** Claude Code skills use `$ARGUMENTS`. Pi's skill invocation is `/skill:name` — whether arguments after the name are passed through (and via what placeholder) is not in the README excerpt read. Check `docs/skills.md` in the Pi package (`/usr/lib/node_modules/@earendil-works/pi-coding-agent/docs/skills.md`) before writing the adapted scope skill, which needs a "what to plan" argument. If Pi has no equivalent, the adapted skill instructs the model to ask the user for the input as its first action.
4. **Gemma availability in this sandbox.** Phase 1 needs a Gemma-3-27B-class model reachable from Pi. Confirm `pi --list-models` (or equivalent) surfaces one via the configured providers, or that Ollama on the host has it pulled. If not, choose the closest available stand-in (e.g., a small Qwen) and note the substitution in the validation report.

## Phase 1: Validate the adaptation pattern works on Gemma

**Why a phase:** if a stripped-down `scope` skill still produces nonsense on Gemma, the whole port is wasted and Phases 2–3 should not run. This phase resolves that.

### Steps

1. Resolve Unknowns 2 and 3 by reading `/usr/lib/node_modules/@earendil-works/pi-coding-agent/docs/skills.md` and doing a one-off `pi --skill` load test with a stub SKILL.md containing the Claude-Code-only frontmatter fields. Decide which frontmatter to keep.

2. Write `skills-agents/scope/SKILL.md`, derived from `skills/scope/SKILL.md` with these adaptations:
   - Replace `AskUserQuestion` with explicit "ask the user in plain text and wait for the reply" instructions.
   - Replace `Agent` (subagent dispatch) with "do the research yourself with `read`/`bash`/`webfetch`."
   - Replace `TaskCreate`/`TaskUpdate` with "maintain a TODO checklist at `plans/<slug>.todo.md`; check items off in place as you complete them."
   - **Cut** the "restructure around uncertainty" step (current step 6) — collapse the process to: clarify → light exploration → draft → commit.
   - **Cut** the "decorative phases" / "transcribed artifacts" anti-pattern sections — replace with one-line rules ("default to a flat checklist; only split into phases if a later step depends on confirming an earlier result first").
   - Compress the plan format spec from prose to a literal template the model copies and fills in.
   - Target ~50–80 lines total (current is ~150). Aim for fewer than 6 numbered rules in the Rules section.

3. Wire it up locally: drop the new SKILL.md at `/root/.agents/skills/scope/SKILL.md` (or per Unknown 3's outcome, the path Pi actually reads from in this container) and start `pi` with the smallest Gemma-class model available. Confirm the skill is listed at startup and that `/skill:scope` (or whatever the invocation is) loads its body.

4. Run scope on a small real planning task (suggest: "plan adding a `--quiet` flag to `start-agent.sh`"). Observe whether the model: (a) actually asks clarifying questions, (b) writes a plan file to `plans/`, (c) follows the simplified format, (d) commits it. Capture the failures.

5. Iterate the SKILL.md until the small-model run produces a usable plan, or conclude the approach doesn't work on this model class and stop. **This is the gate.** Do not start Phase 2 until the user sees a working scope run and confirms.

### Acceptance criteria

- A real Gemma (or stand-in) session with Pi produces a plan file from `/skill:scope` that the user judges acceptable for that small task. "Acceptable" means: roughly the right shape, no fabricated specifics, the user could hand it to a stronger model for execution.
- The validation transcript is captured (e.g., committed under `experiments/` or pasted into a notes file) so Phase 2 has a reference for what "works" looks like on this model class.

## Phase 2: Port build and wrap

### Steps

1. Write `skills-agents/build/SKILL.md` applying the same adaptation rules as scope, plus:
   - Replace the test-fallback chain (acceptance criteria → goals → infer from steps) with one rule: "the first task is to write tests for the active phase's goals; if the phase has no testable behavior, say so and skip."
   - Collapse the spike/research-task carve-out into one sentence.
   - Drop the ~3-files-uncommitted heuristic; replace with "commit when tests pass."
   - Keep the "stop between phases, don't stop between steps" rule — it's load-bearing and literal enough for small models.
   - TODO tracking: the model maintains `plans/<slug>.todo.md` (copied from the plan's Status checklist of the active phase on first run, then ticked off in place).

2. Write `skills-agents/wrap/SKILL.md`:
   - Keep the 5-step structure (it's already mechanical).
   - Drop step 5 (review pass) entirely or demote to a one-line "skim the diff and call out anything obvious; do not fix." Sonnet-grade review judgment is not realistic on Gemma.
   - Inline-replace tool gaps as in scope/build.
   - Target ~40–60 lines.

3. Verify both load in Pi (listing at startup, `/skill:build` and `/skill:wrap` invocable). Quick smoke test: have the model run `/skill:wrap` against a trivial change in this repo and see if it touches the right files.

## Phase 3: Seed via start-agent.sh

### Steps

1. Add a mount in the `docker run` block of `start-agent.sh`: `$SANDBOX/.sandbox_config/agents/skills/:/root/.agents/skills/`. Mirror the granular-bind-mount style already in use; do not collapse into a parent mount.

2. Extend `--init-sandbox` to create `$SANDBOX/.sandbox_config/agents/skills/` and to copy `skills-agents/*` from the repo into it. Match the per-skill-directory clobber semantics of the existing Claude Code skill sync (ADR-005): each skill dir replaces its target wholesale; local-only skill dirs in the sandbox are left untouched; fetch/copy failures warn but do not abort init.

3. If Unknown 1 confirmed OpenCode double-lists from `~/.claude/skills/` and `~/.agents/skills/`, apply the chosen mitigation (rename the variants or disable OpenCode's Claude-skills read path via `opencode.json`).

4. Update CLAUDE.md: add one Layout entry (`skills-agents/` — small-model ports of scope/build/wrap for Pi and OpenCode) and one Key-decisions bullet (`**Small-model skill ports seeded via `~/.agents/skills/`.** Both Pi and OpenCode read this path; one shared variant covers both. See ADR-NN.`). Append a corresponding ADR.

5. Run `start-agent.sh --rebuild` (or `--reset-container`) on a throwaway sandbox to confirm the skills land at `/root/.agents/skills/` and are listed at Pi and OpenCode startup.

## Notes

- The existing `skills/` directory and `~/.claude/skills/` sync are untouched. Claude Code keeps its full-fat versions; this plan only adds the small-model siblings.
- Naming: `skills-agents/` (not `skills-small/` or `skills-pi-opencode/`) because the path it maps to is `~/.agents/skills/`. The name reflects the destination, not the audience.
- If Phase 1 validation fails outright, the right answer is to either (a) raise the model class requirement and document that ("these skills assume Qwen-3-72B-class or better"), or (b) abandon the port. Do not paper over Phase 1 failures by writing thinner skills that smaller models can technically execute but that produce useless output.
