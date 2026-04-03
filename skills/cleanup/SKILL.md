---
name: cleanup
description: Post-implementation housekeeping — update CLAUDE.md, README.md, append ADR.md, and mark completed plans
disable-model-invocation: true
argument-hint: "[optional notes]"
allowed-tools:
  - Read
  - Edit
  - Write
  - Glob
  - Bash
---

You have just finished implementing a major change. Perform the following housekeeping steps. Read every file before editing it. Do all four steps — do not skip any.

## 1. Update CLAUDE.md

Read the current `CLAUDE.md` (or note its absence). Update it to accurately reflect the current state of the project — layout, what the code does, key decisions, and how to make changes. If no `CLAUDE.md` exists, create one following the same structure. Do not pad it with boilerplate; keep it concise and useful for an LLM working in this repo.

## 2. Update README.md

Read the current `README.md` (or note its absence). Update it so it accurately describes the project for a human reader — what it is, how to set it up, how to use it, what's inside. If no `README.md` exists, create one. Match the existing tone and style if one exists.

## 3. Append to ADR.md

Review the work done in this session. If any high-consequence architectural or design decisions were made (trade-offs, things that would be hard to reverse, choices that future contributors need to understand), append new ADR entries to `ADR.md`. Follow the existing numbering and format:

```
## ADR-NNN: Title

**Date:** YYYY-MM-DD
**Status:** Accepted

### Context
Why was this decision needed?

### Decision
What was decided and how?

### Consequences
What follows from this decision?
```

If no `ADR.md` exists, create one with the header `# Architecture Decision Records` and then the entries. If no high-consequence decisions were made in this session, skip this step — do not fabricate ADRs.

## 4. Rename completed plan files

Search for plan files that were used in this session. Look in:
- `.claude/plans/`
- `/plan/`
- Any path matching `**/plan/**/*.md` or `**/plans/**/*.md`

If any plan file was the basis for the work just completed, rename it to prefix the filename with `implemented - `. For example: `add-caching.md` → `implemented - add-caching.md`. Only rename plans that were actually implemented in this session. If no plan files exist or none were implemented, skip this step.

## General rules

- Read before writing. Do not guess at file contents.
- Preserve existing tone, structure, and style in each file.
- Only document what actually changed — do not add speculative or aspirational content.
- If the user provided notes via $ARGUMENTS, factor them into the updates.
