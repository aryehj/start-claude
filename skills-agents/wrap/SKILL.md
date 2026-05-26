---
name: wrap
description: Post-implementation housekeeping — update CLAUDE.md, README.md, append ADR.md, mark completed plans, and skim the diff for obvious issues.
disable-model-invocation: true
argument-hint: "[optional notes]"
allowed-tools:
  - Read
  - Edit
  - Write
  - Glob
  - Bash
---

You have just finished implementing a major change. Perform the following steps in order. Read every file before editing it.

## 1. Update CLAUDE.md

Read `CLAUDE.md`. Update it to accurately reflect the current state of the project — layout, what the code does, key decisions, and how to make changes. If none exists, create one. Keep it concise and useful for an LLM working in this repo. Do not pad with boilerplate.

When adding to a **Key decisions** section, use one-liner bullets: `- **<heading>** — <one-sentence summary>. See ADR-NN.` Rationale belongs in ADR.md, not here.

## 2. Update README.md

Read `README.md`. Update it so it accurately describes the project for a human reader — what it is, how to set it up, how to use it, what's inside. If none exists, create one. Match the existing tone and style.

## 3. Append to ADR.md

Review the work done in this session. If any high-consequence architectural or design decisions were made — trade-offs, hard-to-reverse choices, things future contributors need to understand — append new entries to `ADR.md` following the existing numbering and format. If no high-consequence decisions were made, skip this step — do not fabricate ADRs.

## 4. Rename completed plan files

Look in `plans/` for any plan file that was implemented in this session. Rename it by prefixing the filename with `implemented - `. Example: `add-caching.md` → `implemented - add-caching.md`. Only rename plans actually completed. Skip if none apply.

## 5. Skim the diff

Skim the diff of changes made in this session. Call out anything obviously wrong — missed cases, wrong variable, dead code, critical paths with no tests. Do not auto-fix judgment calls; report them. If there is nothing to flag, say so in one line.

## Rules

- Read before writing. Do not guess at file contents.
- Preserve existing tone, structure, and style in each file.
- Only document what actually changed — no speculative or aspirational content.
- If the user provided notes in the argument, factor them into the updates.
