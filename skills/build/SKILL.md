---
name: build
description: Execute a plan written by /scope — work through the active phase with tasks, atomic commits, and test suggestions
disable-model-invocation: true
argument-hint: "<plan file path or slug>"
allowed-tools:
  - Read
  - Edit
  - Write
  - Glob
  - Grep
  - Bash
  - Agent
  - AskUserQuestion
  - TaskCreate
  - TaskList
  - TaskUpdate
  - TaskGet
  - WebSearch
  - WebFetch
---

The user wants you to implement a plan previously written by `/scope`. $ARGUMENTS points to the plan — a path, filename, or slug under `plans/`. Your job is to execute the *active phase* (the first unchecked entry in the plan's Status checklist) carefully, with appropriate task tracking and commits.

## Process

1. **Locate the plan.** Resolve $ARGUMENTS to a file under `plans/`. If ambiguous (multiple matches) or missing, list candidates and ask the user which one via AskUserQuestion. If $ARGUMENTS is empty, list unimplemented plans (files in `plans/` not prefixed with `implemented`) and ask which to run.

2. **Read the plan fully.** Read the entire plan file, not just the active phase — context, goals, unknowns, and later phases all inform how to do the current phase well. Identify the *active phase*: the first unchecked `- [ ]` entry in the Status checklist. If all phases are checked, tell the user the plan looks complete and stop. Plans may follow either the legacy format (per-phase `### Files` / `### Testing`) or the updated format (optional `### Acceptance criteria`, no Files/Testing); both are valid and existing plans are not migrated.

3. **Clarify before committing to an approach.** Use AskUserQuestion for anything genuinely ambiguous about the active phase: unresolved items in the plan's Unknowns section that block this phase, decisions the plan punted to implementation time, or assumptions you'd otherwise have to guess at. Do not ask questions the plan already answers. Do not ask cosmetic questions. Batch questions in a single AskUserQuestion call when possible — don't drip-feed. If the phase is straightforward and unambiguous, skip this step.

4. **Sanity-check the phase scope before starting.** If the active phase looks like it would obviously not fit in one session — many files, many VM/network roundtrips, many independent decisions — stop and propose a sub-phase split to the user. Do not try to force an oversized phase through. Running out of context mid-phase is the failure mode this skill exists to prevent.

5. **Plan the tasks. TaskCreate is mandatory.** Before doing any edits, create a task list via TaskCreate with one task per numbered step in the plan's active phase (or per meaningful unit of work if the plan isn't numbered). This is non-negotiable — even for short phases. The task list is how the user sees progress in the TUI; skipping it is skipping their visibility.
   - If you hit a factual unknown (library behavior, API shape, config option) that would take more than a quick grep to resolve, add a **research task** or **spike task** before the dependent work. Mark them clearly in the title (e.g., "Research: does X library support Y?").
   - **Tests come first.** The first non-spike, non-throwaway task in the list must be writing tests that pin down the phase's "done" state. Derive that state via this fallback chain: phase-level `### Acceptance criteria` if present → plan-level `## Goals` if not → infer from the phase's Steps as a last resort. Tests live in `tests/` at the project root — create that directory if it does not exist. Spikes, research, and intentionally-throwaway exploratory code are exempt and may run before the test task; mark those tasks clearly (e.g., "Spike:" / "Research:") so the rule stays auditable. If the phase has no testable behavior at all (pure docs, config tweak with no logic) — either as you assess it, or as the plan declares directly via Acceptance criteria (e.g., "docs only — no code-level assertions") — state that explicitly in your opening update and skip the test task. Do not invent ceremonial tests.

6. **Work the tasks.** Mark each task in-progress before starting it and completed immediately when done — do not batch status updates. If a task reveals new work, add tasks rather than expanding the current one silently. If the plan turns out to be wrong about something load-bearing, stop and surface it to the user before forging ahead.
   - **Order of execution:** any spikes/research first, then the test task, then production code. Do not write production code before the tests for it exist. The tests should fail (or be skipped pending implementation) at the moment they are written; that's the signal you're testing real behavior, not retrofitting assertions to whatever the code happens to do.

7. **Commit at checkpoints, not just at the end. Run the tests before each commit.** After completing a task or group of tasks that represents a coherent, potentially-revertible unit, run the project's test suite (whatever runner the project uses — `uv run pytest tests/`, `npm test`, `go test ./...`, etc., scoped to or including `tests/`). **Commit only after the tests pass.** If they fail, fix the underlying issue and re-run before committing — do not commit a red tree, do not skip or `xfail` failing tests just to get the commit through.
   - Mid-phase checkpoint rule: **if you have substantive edits across more than ~3 files with nothing committed, stop, run tests, and commit a checkpoint before continuing.** This bounds the blast radius of running out of context and gives the user a recoverable state.
   - Bundle trivial changes; don't commit every tiny edit.
   - Don't commit broken / half-finished states.
   - Follow the project's commit style (check recent `git log` for tone; respect CLAUDE.md instructions such as "no Co-Authored-By lines").
   - Do not push.
   - If the project genuinely has no test runner yet, the *first* commit of the phase is the one that establishes one (test file under `tests/` plus whatever minimal config the runner needs). Subsequent commits in the same session are then governed by the tests-pass-before-commit rule.

8. **Before declaring the phase done, the working tree must be clean — or every dirty file explicitly accounted for.** Run `git status` at the end. Either commit the remaining changes, or in your final report list every uncommitted file with a one-line reason for leaving it (e.g., "unrelated to this phase, found incidentally", "needs user decision before commit"). Do not silently end with a dirty tree.

9. **Update the plan's Status checklist.** When the active phase is complete, mark its checkbox `[x]` in the plan file. Do not mark later phases. Do not rename the plan file — that's `/wrap`'s job.

10. **Final test run, then report.** Before ending the session, run the full test suite one more time and confirm it is green. Then end your turn with a short summary of what changed, the exact test command that was run and its result (e.g., "`uv run pytest tests/` — 14 passed"), and a **Manual verification** section for anything the automated tests cannot cover (dev-server flows, UI clicks, external services, credential-gated paths). For each such behavior, give the user a concrete way to check it: exact command, URL, UI flow, or log line.

## Rules

- **Stop between phases. Don't stop between steps unless something forces it.** A phase boundary is a hard stop — finish the active phase, run the final test, report, and end the turn. Do not roll into the next phase even if it looks quick; if the user wants it, they'll invoke `/build` again. Between steps *within* the active phase, keep going. Stop only when:
  - You hit a major problem (the plan is wrong, a load-bearing assumption broke, the work is not implementable as written).
  - You need to surface a consequential question — one whose answer changes how the rest of the phase looks, not a cosmetic preference.
  - You need the user to do something (interactive login, paste a credential, manually verify a UI flow, decide a tradeoff).
  - You are at meaningful risk of running out of context before the phase completes.

  Routine task transitions, normal test runs, intermediate commits, and "this turned out trickier than I thought" don't count. Pushing through mechanical steps without checking in is the whole point of a phase.
- Respect the plan. The plan is the source of truth for what to build. If you disagree with it, say so and ask — don't quietly deviate.
- Don't over-scope. Resist bundling in unrelated cleanup, refactors, or "while I'm here" changes. Those belong in `/wrap` or a separate task.
- Ground before guessing. When a step depends on external facts (library versions, API shapes), verify with Read/Grep/Bash/WebFetch rather than writing confident-looking placeholder code.
- Surface blockers early. If a task reveals that the plan is wrong or a phase is not implementable as written, stop and tell the user before writing code against a broken premise.
- Do not run `/wrap`. Housekeeping (CLAUDE.md, README.md, ADR.md, renaming the plan file) is a separate skill the user invokes when the whole plan is done.
