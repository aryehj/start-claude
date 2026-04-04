---
  name: plan
  description: Explore the codebase and write implementation plans to /plans as markdown files
  disable-model-invocation: true
  argument-hint: "<what to plan>"
  allowed-tools:
    - Read
    - Glob
    - Grep
    - Bash
    - Write
    - Agent
    - AskUserQuestion
  ---

  The user wants you to create an implementation plan. Your job is to explore just enough to write a clear, actionable plan that a Claude Sonnet could follow to implement the work. $ARGUMENTS describes what to plan.

  ## Process

  1. **Understand the request.** Read $ARGUMENTS carefully. If the request is ambiguous or missing critical details, ask clarifying questions using AskUserQuestion and stop — do not write a plan until you have enough information.

  2. **Light exploration.** Read relevant files, grep for key patterns, and understand the current state. Keep this focused — you are planning, not implementing. Do not modify any source code.

  3. **Write the plan.** Create a single markdown file in the `plans/` directory at the project root (create the directory if it doesn't exist). Name the file with a short kebab-case slug describing the work (e.g.,
  `add-caching.md`, `fix-auth-race-condition.md`). If $ARGUMENTS describes multiple independent concerns, organize them as separate phases within this one file — do not create multiple files.

  ## Plan format

  ```markdown
  # <Title>

  ## Status

  - [ ] Phase 1: <short label>
  - [ ] Phase 2: <short label>
  - [ ] Phase 3: <short label>
  <!-- one checkbox per phase; mark [x] as phases complete during implementation -->

  ## Context

  What exists today and why this change is needed. Reference specific files and line numbers.

  ## Goals

  Bulleted list of what "done" looks like.

  ---

  ## Phase 1: <Label>

  ### Steps

  Numbered steps. Each step should be concrete and reference specific files/functions to create or modify. A step like "update the config" is too vague — say which file, which section, what changes.

  ### Files

  Bulleted list of files created or changed in this phase.

  ### Testing

  How to verify this phase works. Specific commands, behavior to check, edge cases.

  ---

  ## Phase 2: <Label>

  <!-- repeat structure above -->

  ---

  ## Notes

  Any caveats, risks, open questions, or alternative approaches considered.

  If the work is a single cohesive concern with no natural phase breakdown, use one phase and omit the phase numbering from the Status checklist — just list the individual steps as checkboxes instead.

  Rules

  - Output only questions or a plan file. At the end of your turn, the only visible results should be clarifying questions to the user OR a new .md file written to plans/. Do not produce both in the same turn.
  - Write for Sonnet. Unless the user says otherwise, assume a Claude Sonnet will implement this plan. Be explicit about what to do and where — don't assume the implementer has deep context. Include file paths, function names,
  and concrete descriptions of changes.
  - One file, always. All concerns go in a single plan file, organized as phases. Never create multiple plan files for one /plan invocation.
  - Don't over-explore. Read what you need to write a good plan, then write it. This is not a research task.
  - Don't implement. You are writing a plan, not code. Do not edit any source files outside of plans/.
  - Reference the current state. Ground the plan in what actually exists — cite files, line numbers, existing patterns. Don't plan against an imagined codebase.
