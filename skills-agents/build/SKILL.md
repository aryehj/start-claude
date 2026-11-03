---
name: build
description: Execute the active phase of a plan written by /scope. Work through each step with a TODO file, atomic commits, and tests before committing.
disable-model-invocation: true
argument-hint: "<plan file path or slug>"
allowed-tools:
  - Read
  - Edit
  - Write
  - Glob
  - Grep
  - Bash
  - WebSearch
  - WebFetch
---

The user wants you to implement a plan previously written by `/scope`. The argument points to the plan — a path, filename, or slug under `plans/`. Your job is to execute the *active phase* (the first unchecked entry in the plan's Status checklist) carefully, with a TODO file, commits, and tests.

## Process

1. **Locate the plan.** Resolve the argument to a file under `plans/`. If ambiguous or missing, list candidates and ask the user in plain text which one to run. Wait for the reply before continuing.

2. **Read the plan fully.** Read the entire plan file — context, goals, unknowns, and later phases all inform the active phase. Identify the *active phase*: the first unchecked `- [ ]` entry in the Status checklist. If all phases are checked, tell the user the plan looks complete and stop.

3. **Clarify if needed.** If anything in the active phase is genuinely ambiguous — unresolved unknowns, decisions punted to implementation time — ask the user in plain text and wait for the reply. Do not ask questions the plan already answers.

4. **Create a TODO file.** Before doing any edits, copy the active phase's numbered steps into `plans/<slug>.todo.md` as a checklist. Check items off in place as you complete them.

5. **Write tests first.** If the phase has testable behavior, write tests that define "done" before writing any implementation. Run them — confirm they fail. If the phase has no testable behavior (pure docs, config with no logic), say so and skip. Do not write implementation before the failing tests exist.

6. **Work the steps.** Check off each item in the TODO file as you finish it. Do your own research with Read/Bash/WebFetch when a step depends on external facts — do not write confident-looking placeholder code. If a step reveals new work, add items to the TODO file.

7. **Commit when tests pass.** Run the test suite. Commit only when it passes. Bundle coherent units — don't commit every tiny edit, and don't let edits pile up across many files without a checkpoint commit.

8. **Clean tree before finishing.** Run `git status`. Commit any remaining changes, or list each uncommitted file with a one-line reason for leaving it.

9. **Mark the phase done.** Change the active phase's `- [ ]` to `- [x]` in the plan file. Commit the update.

10. **Test it yourself first.** Run every check you can yourself — the test suite, the CLI, the script, `curl` against a local service. Leave for the user only what you cannot run, and give a one-line reason for each.

11. **Script the user's checks.** Put all remaining manual checks in one script inside the project directory (not `/tmp`). It prints PASS/FAIL per check and saves its output to a log file in the project. Give the user one short command to run it, and ask them to reply "done". Then read the log yourself instead of asking them to paste output.

12. **Final report.** Run the full test suite one last time. Report: what changed, the exact test command and result, and, if manual checks remain, the one-line command to run the script.

## Rules

- **Stop between phases, not between steps.** Finish the active phase completely before stopping. Only pause mid-phase if the plan is wrong, you need a decision from the user, or you risk running out of context.
- **No scope creep.** Do not bundle in unrelated cleanup or refactors. Stick to what the active phase describes.
- **Respect the plan.** If you disagree with it, say so and ask — don't quietly deviate.
- **Do not run /wrap.** That is a separate skill for after the whole plan is done.
