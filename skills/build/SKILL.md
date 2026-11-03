---
name: build
description: Execute a plan written by /scope — run its phases in fresh subagents, verifying between them, until a human gate, a real blocker, or the end of the plan
disable-model-invocation: true
argument-hint: "<plan file path or slug> [--one]"
allowed-tools:
  - Read
  - Edit
  - Write
  - Glob
  - Grep
  - Bash
  - Agent
  - AskUserQuestion
  - ToolSearch
  - TaskCreate
  - TaskList
  - TaskUpdate
  - TaskGet
  - WebSearch
  - WebFetch
---

The user wants you to implement a plan previously written by `/scope`. $ARGUMENTS points to the plan — a path, filename, or slug under `plans/` — optionally followed by `--one`.

You are the **orchestrator**. The per-phase procedure lives in `${CLAUDE_SKILL_DIR}/phase.md`. You run phases through it — inline or in fresh worker subagents — verify each result yourself, and keep going until something genuinely needs the human. The human's time is for substantive judgment and checks no agent can run, not for typing `/build` again between mechanical phases.

## Process

1. **Locate the plan.** Resolve $ARGUMENTS to a file under `plans/`. If ambiguous or missing, list candidates and ask via AskUserQuestion. If empty, list unimplemented plans (files in `plans/` not prefixed with `implemented`) and ask which to run.

2. **Read the plan fully** and identify the remaining work: every unchecked `- [ ]` entry in Status. If none remain, tell the user the plan looks complete and stop.

3. **Work out the run.** Starting from the first unchecked entry, the run is every consecutive entry up to and **including** the first human gate, or to the end of the plan. With `--one`, the run is just the first unchecked phase.
   - A **human gate** is an entry marked `(human gate: …)`. Legacy plans have no markers: treat a phase as a gate if its label or text calls for human review, testing, or a decision; when in doubt, treat it as a gate.
   - A flat step-level Status list is one unit of work.
   - **Oversized units.** If a phase or flat list would obviously not fit in one fresh session (many files, many slow roundtrips, many independent pieces), split it into worker-sized chunks yourself, each startable from the repo and plan alone, and record the split as indented sub-entries in Status. Ask the user only if choosing the split is itself a judgment call.

4. **Set up the task list.** The task list is how the user watches progress in the TUI; the plan file is the durable record across sessions. Load the task tools if they aren't loaded yet — they are deferred: ToolSearch with `select:TaskCreate,TaskUpdate,TaskList,TaskGet`. If they are unavailable, say so in one line and carry on; the plan file's checkboxes still track progress.
   - **Orchestrated:** create one task per unit in the run (e.g. "Phase 2: <label>"). Mark each in-progress when you dispatch it and completed only after step 7 verifies it. Add a task when you split a unit or re-dispatch to fix something. Workers don't keep task lists — yours is the single view.
   - **Inline:** `phase.md` covers it — one task per step.

5. **Choose inline or orchestrated.**
   - **Inline** when the run is a single unit: read `${CLAUDE_SKILL_DIR}/phase.md` and follow it yourself in inline mode. You can ask the user questions directly.
   - **Orchestrated** when the run has two or more units: dispatch each to a worker subagent, one at a time, in order (step 6). Don't do phase work yourself in this mode — keep your context for verifying and deciding.

6. **Dispatch a worker** with the Agent tool (`general-purpose`). The prompt must give: the absolute plan path; the exact Status line to execute; "Read `${CLAUDE_SKILL_DIR}/phase.md` and follow it in **worker** mode"; and anything you settled since the plan was written (answers from the user, resolved blockers, defaults to honor). Model: `opus` for entries annotated `(Opus recommended)`; otherwise omit the override. Wait for the result before dispatching the next — phases are sequential.

7. **Verify, don't trust.** After each worker reports:
   - Run the test suite yourself and confirm it's green.
   - `git log` shows the reported commits; `git status` is clean or every dirty file is accounted for.
   - The phase's Status checkbox is ticked.
   - Skim `git diff <before>..HEAD --stat` and the riskiest hunks for drift from the plan or scope creep.
   - Read `DEFAULTS TAKEN` and `PLAN DRIFT`.

   If verification fails on something mechanical (a red test, a missed tick, an uncommitted file), send it back: dispatch a worker with the specific problem, or fix a trivial miss yourself.

8. **Decide whether to continue.**
   - `DONE`, verified, no drift, not a gate → dispatch the next unit.
   - `BLOCKED` on a factual or mechanical question → settle it yourself (research, read the code, diagnose the failure), then re-dispatch with the answer.
   - `BLOCKED` on a substantive judgment call, or `PLAN DRIFT` that changes later phases → stop and put the question to the user via AskUserQuestion, using the worker's options. On an answer, record it (in the plan if it changes the plan) and continue the run.
   - The unit just finished was a human gate, or the worker returned `NEEDS-HUMAN` → read the script the worker wrote, run whatever part of it you can, then stop and hand off (step 9). After the user replies "done", read the log the script wrote and continue from there. Don't ask them to paste output.
   - End of the run (`--one`, or end of plan) → stop and report.

9. **Report.** End with:
   - Which entries completed this session, and their commits.
   - The exact test command and its result.
   - Any defaults workers took that the user may want to revisit.
   - If stopped at a gate: what the human needs to judge or check, the one-line command to run the script, and what to reply. If stopped on a question, the question.
   - What remains in the plan.

## Rules

- **Stop only for the human's part.** Human gates, substantive judgment calls, broken load-bearing assumptions, `--one`, or the end of the plan. Routine phase transitions, normal test runs, mechanical failures you can diagnose, and "this turned out trickier than I thought" are not reasons to stop.
- **Respect the plan.** It is the source of truth. Don't approve worker output that quietly deviates from it; surface the disagreement instead.
- **Don't over-scope.** No unrelated cleanup or refactors — those belong in `/wrap` or a separate task.
- **Do not run `/wrap`. Do not push.** Housekeeping (CLAUDE.md, README.md, ADR.md, renaming the plan file) is a separate skill the user invokes when the whole plan is done.
