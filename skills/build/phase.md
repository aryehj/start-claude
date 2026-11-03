# Executing one phase of a /scope plan

This is the per-phase procedure for `/build`. You are running it either **inline** (the main session, talking to the user) or as a **worker** (a subagent the `/build` orchestrator dispatched with a plan path and a phase). The orchestrator's prompt tells you which. The plan file and git are the only shared state — you have no memory of earlier phases beyond what they record.

## Procedure

1. **Read the plan fully.** The whole file, not just your phase — context, goals, unknowns, and later phases all inform how to do this one well. Plans may follow the legacy format (per-phase `### Files` / `### Testing`) or the current one (optional `### Acceptance criteria`); both are valid. Check `git log` since the plan was added for work earlier phases already did.

2. **Resolve ambiguity.** Anything genuinely ambiguous — an unresolved Unknown that blocks this phase, a decision the plan punted to implementation time — must be settled before committing to an approach. First try to settle it yourself: Read/Grep/Bash/WebFetch for factual questions. What remains is a judgment call:
   - **Inline:** ask via AskUserQuestion, batched in one call. Don't ask what the plan already answers; don't ask cosmetic questions.
   - **Worker:** never ask the user (you can't). If you can proceed on a clearly-safe default, do so and note it in your report. Otherwise stop before writing code and return `BLOCKED` (see Report).

3. **Track and order the work.** The plan file is the durable record: tick step-level checkboxes in Status as each completes.
   - **Inline:** also keep a task list so the user can watch progress in the TUI — one task per numbered step in the phase (or per meaningful unit if it isn't numbered), plus any spike/research tasks. The tools are deferred: if TaskCreate isn't loaded, load it with ToolSearch `select:TaskCreate,TaskUpdate,TaskList,TaskGet`. Mark each task in-progress before starting it and completed as soon as it's done, not in batches. If the tools are unavailable, say so in one line and post a short progress line per step instead.
   - **Worker:** don't keep a task list — the orchestrator's list is the user's single view. Tick the plan's checkboxes.

   Work in this order:
   - **Spikes / research first,** when a step depends on a factual unknown that takes more than a quick grep (library behavior, API shape). Throwaway exploratory code is fine here.
   - **Then tests.** Write tests that pin down the phase's "done" state before production code. Derive "done" from phase-level `### Acceptance criteria` if present → plan-level `## Goals` if not → the phase's Steps as a last resort. Tests live in `tests/` at the project root. They should fail (or skip pending implementation) when first written. If the phase has no testable behavior (pure docs, config with no logic), say so explicitly and skip — don't invent ceremonial tests.
   - **Then production code.** If a step reveals new work, add it to the plan's Status rather than silently expanding the current step.

4. **Commit at checkpoints. Run the tests before each commit.** After a coherent, revertible unit of work, run the project's test suite (`uv run pytest tests/`, `npm test`, `go test ./...` — whatever the project uses). **Commit only when it passes.** Fix failures rather than skipping or `xfail`ing them.
   - If more than ~3 files have substantive uncommitted edits, stop, test, and commit a checkpoint before continuing. This bounds the damage of running out of context.
   - Bundle trivial changes; don't commit broken or half-finished states.
   - Follow the project's commit style (recent `git log`, CLAUDE.md rules such as "no Co-Authored-By lines"). Do not push.
   - If the project has no test runner yet, the phase's first commit establishes one.

5. **Test it yourself first.** Before declaring the phase done, run every check you can run yourself — not just the unit suite: run the CLI, start the server and `curl` it, run the script, drive a headless browser, run `claude -p` against a scratch repo, inspect the generated config. The human tests only what you can't, and each such check needs a one-line reason you can't run it (e.g., "needs the macOS host: colima is not reachable from this container", "needs a GUI", "needs a credential I don't have"). "It would take a while" or "it's an integration test" is not a reason.

6. **Script the human checks.** If any check remains for a human, don't hand over prose or commands to paste. Write **one script** covering all of the phase's human-only checks:
   - Put it inside the project directory — never the scratchpad or `/tmp`, which the user's machine may not see. Reusable checks are committed under `tests/`; one-offs go in a gitignored path (check `.gitignore`; add an entry if none fits).
   - The user runs it with one short command (e.g. `tests/verify-foo.sh`). No multi-line blocks, no inline comments, no variables the user must set first — the script sets or discovers what it needs and prints a clear error if a precondition is missing.
   - It checks the results itself and prints `PASS`/`FAIL` per check, ending with a summary line.
   - It writes its full output to a gitignored log file in the project (e.g. `tee` into `.verify/<name>.log`), so you read the results directly — the user just replies "done". Don't ask them to paste output.
   - Run whatever part of it you can yourself (syntax check with `bash -n`, the precondition logic) before handing it over.

7. **Leave a clean tree.** Run `git status`. Commit remaining changes, or list every uncommitted file with a one-line reason for leaving it (e.g., "needs user decision before commit"). Never end silently dirty.

8. **Tick the phase.** When the phase is complete, mark its Status checkbox `[x]` and commit that. Don't tick later phases. Don't rename the plan file — that's `/wrap`'s job.

9. **Final test run, then report.** Run the full suite once more and confirm it's green.

## Report

**Inline:** end with a short summary — what changed, the exact test command and result (e.g. "`uv run pytest tests/` — 14 passed"), and, if human checks remain, the one-line command to run the script and what to reply.

**Worker:** your final message is read by the orchestrator, not the user. Return exactly this shape:

```
STATUS: DONE | BLOCKED | NEEDS-HUMAN
PHASE: <the Status line you executed>
COMMITS: <short hash + subject, one per line>
TESTS: <exact command> — <result>
SELF-CHECKS: <what you verified beyond the suite, one line each>
DEFAULTS TAKEN: <judgment calls you made on a safe default, or "none">
BLOCKER: <only if BLOCKED: the question, why you can't settle it, 2–4 options with your recommendation>
HUMAN CHECKS: <only if NEEDS-HUMAN: script path, the one-line command, log path, and why each check needs a human>
PLAN DRIFT: <any load-bearing plan assumption that turned out wrong, or "none">
```

Use `NEEDS-HUMAN` only when step 5 left checks you genuinely couldn't run. Use `BLOCKED` when you stopped before finishing.

## Rules

- **Respect the plan.** It is the source of truth. If it's wrong about something load-bearing, stop and surface it (inline: tell the user; worker: `BLOCKED` with `PLAN DRIFT`) — don't quietly deviate or write code against a broken premise.
- **Don't over-scope.** No unrelated cleanup, refactors, or "while I'm here" changes.
- **Ground before guessing.** Verify external facts with Read/Grep/Bash/WebFetch rather than writing confident-looking placeholder code.
- **Keep going between steps.** Within a phase, stop only for a load-bearing problem, a consequential judgment call, something only the user can do, or real risk of running out of context (then checkpoint-commit first and report what's left).
- **Do not run `/wrap`. Do not push.**
