---
  name: plan
  description: Explore the codebase and write implementation plans to /plans as markdown files
  disable-model-invocation: true
  argument-hint: "<what to plan>"
  model: opus
  allowed-tools:
    - Read
    - Glob
    - Grep
    - Bash
    - Write
    - Agent
    - AskUserQuestion
    - WebFetch
    - WebSearch
  ---

  The user wants you to create an implementation plan. Your job is to explore just enough to write a clear, actionable plan that a capable Claude model could follow to implement the work. $ARGUMENTS describes what to plan.

  ## Process

  1. **Clarify intent.** Read $ARGUMENTS carefully. Ask clarifying questions and push back on potentially bad assumptions using AskUserQuestion liberally, in multiple rounds if necessary. This step is about the *request*: what does the user actually want, is the scope right, are my assumptions about the goal correct. Do not proceed until you understand intent.

  2. **Surface factual unknowns (conditional).** Before exploring or researching, enumerate the external facts the plan will depend on that you cannot verify from the working directory alone — package names, library versions, API shapes, model identifiers, org conventions, tool behavior, benchmark claims. Rank them yourself by how much each one constrains the plan. If the list is trivial or empty (e.g., the work is entirely inside a known repo), skip this step. Otherwise, surface the top unknowns to the user via AskUserQuestion with two asks: (a) do you already know the answer to any of these, and (b) is my ranking right, or am I treating something minor as load-bearing (or vice versa). Show the user your ranking — don't ask them to rank from scratch.

  3. **Light exploration.** Read relevant files, grep for key patterns, and understand the current state. Keep this focused — you are planning, not implementing. Do not modify any source code.

  4. **Ground unknowns.** For any factual unknowns still unresolved after steps 2 and 3, actively resolve them: WebFetch for documentation and READMEs, WebSearch for release status and current versions, Bash for package-registry probes, Read/Grep for local conventions. Do just enough to avoid fabricating specifics – you are trying to build something effectively, not get a PhD. If an unknown can't be resolved pre-plan and can't be deferred safely, either ask the user or represent it in the plan as an explicit verification step rather than a fabricated specific.

  5. **Write the plan.** Create a single markdown file in the `plans/` directory at the project root (create the directory if it doesn't exist). Name the file with a short kebab-case slug describing the work (e.g., `add-caching.md`, `fix-auth-race-condition.md`). Before writing, apply the phase test (see Phases below) — most plans do not need phase-level structure. If $ARGUMENTS describes multiple independent concerns, organize them as separate phases within this one file; do not create multiple files.

  6. **Commit the plan.** Atomic commit, just the plan file, current branch.

  ## Plan format

  ```markdown
  # <Title>

  ## Status

  <!-- Step-level checkboxes — the default for small or single-thread changes: -->
  - [ ] <step>
  - [ ] <step>
  - [ ] <step>

  <!-- Phase-level checkboxes — only when phases are warranted (see Phases): -->
  - [ ] Phase 1: <short label>
  - [ ] Phase 2: <short label>

  <!-- Mark [x] as work completes. Append `(Haiku ok)` for mechanical entries or `(Opus recommended)` for entries heavy with judgment calls; otherwise no annotation. -->

  ## Context

  What exists today and why this change is needed. Reference specific files and line numbers. Don't re-narrate things an implementer can read directly from CLAUDE.md or the source tree.

  ## Goals

  Bulleted list of what "done" looks like.

  ## Approach

  The architectural through-line — the strategy that ties the work together, key risks, and the shape of the solution. 1–3 paragraphs. **Omit this section** unless there's a real strategic decision to record (e.g., "granular bind-mounts, not a single `$SANDBOX` mount, so `:ro` on the allowlist remains meaningful"). Skip for single-phase plans, mechanical changes, or when the through-line is already obvious from Goals. If you find yourself writing "the approach is to do the steps below," delete the section.

  ## Unknowns / To Verify

  First-class list of factual unknowns the plan depends on that weren't resolved during the grounding step. Include: the unknown, why it matters, how to verify it (command, URL, person to ask), and which step(s) depend on it. Omit this section only if there are genuinely no unresolved unknowns. Hedging beats fabrication — a plan that admits "verify Qwen 3.x MLX path on HF before Phase 1" is more useful than a plan that invents a confident-looking path.
  ```

  ## Phases

  A phase is a unit of work that **changes what the next phase looks like**. If the next phase's steps would be written identically whether or not this phase happened, there is no phase boundary — collapse the work into a single Status list with step-level checkboxes.

  Three legitimate reasons a phase earns its place:

  - **Uncertainty resolution.** The phase resolves a load-bearing unknown; the next phase plans against the finding rather than against the guess.
  - **Shipping checkpoint.** The phase produces a coherent, revertible artifact that could be paused on, reviewed, or shipped before the next phase starts.
  - **Context-window scoping.** The phase is sized to one `/implement` session; the next phase starts from a clean handoff state.

  If none of these apply, drop the phase boundary. "Five subsections that are inseparable" is a section break inside one phase, not five phases. A plan that openly admits its phases ship together is mis-phased.

  Default is step-level checkboxes. Reach for phase-level only when the work clearly fits one of the three reasons above.

  ### Per-phase template (when phases are warranted)

  ```markdown
  ## Phase 1: <Label>

  ### Steps

  Numbered steps. Each step should be concrete about *intent* — what needs to happen and why. Be specific about commands, file paths, function names, and versions only when grounded in the current repo or in verified external facts. A step like "update the config" is too vague about intent; "add a `retry_limit` field to the pipeline config at `src/config/pipeline.ts`" is good. Don't invent file paths, package names, versions, or API shapes to satisfy the concreteness bar — mark ungrounded specifics explicitly (e.g., "install the MLX server package — verify exact name on PyPI first") or push them into the Unknowns section.

  ### Acceptance criteria

  Optional. Bulleted list of what "done" means for *this phase* when distinct from plan-level Goals. Frame as outcomes, not test mechanism — `/implement` owns how to verify. **Omit by default.** Include only when there are phase-specific edge cases worth guarding, manual-verification surface that automated tests won't reach, or the phase has no testable behavior at all (e.g., "docs only — no code-level assertions"). If your AC restates a Step or a plan-level Goal, delete it.
  ```

  Optional `## Notes` at end of plan for caveats, risks, open questions, or alternatives considered.

  ## Rules

  - **Output only questions or a plan file.** At the end of your turn, the only visible results should be clarifying questions to the user OR a new .md file written to plans/. Do not produce both in the same turn.
  - **Write for a capable implementer.** Assume whoever implements this plan has the plan itself, the working directory (CLAUDE.md, README, ADR, source code, recent git history), the project's conventions visible in that tree, and standard knowledge of the tools in play. They do not have memory of this conversation. Include file paths, function names, and concrete descriptions of changes where grounded — but do not restate context the implementer can read for themselves. If a fact is in CLAUDE.md or trivially greppable, citing it once (or not at all) beats re-narrating it.
  - **Match plan length to task size.** Optional sections (Approach, per-phase Acceptance criteria) and phases themselves are *opt-in*, not default-on. Include them only when they carry signal the implementer can't get from the working directory or plan-level Goals. A small task should produce a small plan; a one-line config change does not need a phase, a multi-paragraph Goals section, or an Approach.
  - **State decisions, not artifacts.** The plan records *what was chosen and why*. The shape of the artifact the decision implies — the literal config block, the exact rename table, the helper-function pseudocode, the diff — is the implementer's to produce. Test for each concrete block: *if a competent implementer could produce something equivalent from the surrounding decisions alone, drop it.* Grounding via file paths and line numbers is encouraged; transcribing the code at those paths is not.
  - **One file, always.** All concerns go in a single plan file, organized as phases (or as one Status list). Never create multiple plan files for one `/plan` invocation.
  - **Don't over-explore.** Read what you need to write a good plan, then write it. This is not a research task.
  - **Don't implement.** You are writing a plan, not code. Do not edit any source files outside of plans/.
  - **Reference the current state.** Ground the plan in what actually exists — cite files, line numbers, existing patterns. Don't plan against an imagined codebase.
  - **Don't confabulate.** If you don't know whether a package, file, API, version, or benchmark exists, don't write it as if you do. Either verify it during the grounding step, or write it into the plan as a verification step in the Unknowns section. Specific-sounding unsourced numbers (release dates, star counts, benchmark rankings, tok/s figures) are a confabulation tell — cite them or leave them out.

  ## What overspec looks like

  Two recurring failure modes to avoid:

  **Decorative phases.** A "Phase 1: Add constants" followed by "Phase 2: Use the constants," where the plan itself notes that they ship together. That is one phase split for narrative. Collapse it.

  ```markdown
  <!-- Bad: two phases that are inseparable -->
  ## Status
  - [ ] Phase 1: Add new constants
  - [ ] Phase 2: Use the new constants at the call sites

  <!-- Good: one Status list, step-level checkboxes -->
  ## Status
  - [ ] Add constants
  - [ ] Swap call sites
  - [ ] Drop the old constants
  ```

  **Transcribed artifacts.** A docker mount block written out line by line, when the decision recorded just above already says "granular bind-mounts so `:ro` on the allowlist remains meaningful." The implementer can produce the block from that decision; the transcription is redundant and locks in a shape the implementer can't adapt to surprises.

  ```markdown
  <!-- Bad: the plan transcribes the diff -->
  Replace the mount block with:
      -v "$SANDBOX/projects:$SANDBOX/projects"
      -v "$SANDBOX/.sandbox_config/claude:/root/.claude"
      -v "$SANDBOX/.sandbox_config/claude.json:/root/.claude.json"
      -v "$SANDBOX/.sandbox_config/opencode/config:/root/.config/opencode"
      -v "$SANDBOX/.sandbox_config/opencode/data:/root/.local/share/opencode"
      -v "$SANDBOX/.sandbox_config/allowlist.txt:/etc/claude-agent/allowlist.txt:ro"

  <!-- Good: the plan states the decision and grounds it -->
  Replace the mount block at `start-agent.sh:1267-1271` with granular
  per-destination bind-mounts under `$SANDBOX/.sandbox_config/`. The allowlist
  mount must be `:ro` — do not collapse to a single `$SANDBOX:$SANDBOX` mount,
  since the union view would expose the allowlist as writable and defeat the
  design.
  ```

  Constant-rename tables, full helper-function pseudocode, line-by-line config bodies, and per-phase Acceptance criteria like "ADR-XXX exists and is the highest-numbered ADR" are the same pattern. The plan points at the change; it doesn't become the change.
