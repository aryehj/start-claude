---
name: scope
description: Explore the codebase and write an implementation plan to plans/ as a markdown file. Use when the user asks you to plan, design, or think through a feature, fix, or refactor before implementing it.
disable-model-invocation: true
allowed-tools: Read Glob Grep Bash Write WebFetch WebSearch
---

The user wants you to create an implementation plan. Explore just enough to write a clear, actionable plan that a capable model could follow to implement the work. The user's request (in the message after this skill content) describes what to plan.

## Process

1. **Clarify intent.** Read the request carefully. If scope, goals, or key assumptions are unclear, ask in plain text and wait for the user's reply before continuing. Push back on bad assumptions. Do not proceed until you understand what the user actually wants.

2. **Surface external unknowns (conditional).** Before exploring, identify external facts the plan depends on that you cannot verify from the working directory — package names, API shapes, library versions. If the list is trivial or empty, skip this step. Otherwise, ask the user whether they know the answers, rank unknowns by how much they constrain the plan, then resolve the rest by reading docs or probing the repo.

3. **Light exploration.** Read relevant files, grep for key patterns, understand current state. Keep it focused — you are planning, not implementing. Do not modify any source files.

4. **Draft the plan.** Write to a single markdown file in `plans/` at the project root (create the directory if it doesn't exist). Use a short kebab-case slug as the filename. Use the template below.

5. **Commit the plan.** One atomic commit, just the plan file, on the current branch.

## Plan template

Fill in and write this to `plans/<slug>.md`:

```
# <Title>

## Status

- [ ] <step>
- [ ] <step>

## Context

What exists today and why this change is needed. Cite specific files and line numbers.

## Goals

Bulleted list of what "done" looks like.

## Unknowns / To Verify

Unresolved external facts: the unknown, why it matters, how to verify it.
Omit this section if there are no unresolved unknowns.
```

### When to use phases

Use a flat Status checklist (step-level checkboxes) by default. Add phases only when a later step genuinely depends on confirming an earlier result — because a key assumption might be wrong, or a human must judge output before continuing. Each phase label must name the uncertainty it resolves (e.g., "Validate omlx port compatibility"), not the activity ("Add constants"). If two phases always ship together, collapse them into one.

## Rules

- **Output only questions or a plan file.** The only visible result at the end of your turn should be clarifying questions OR a new .md file in `plans/`. Not both in the same turn.
- **Write for a capable implementer.** They have the plan, the working directory (CLAUDE.md, README, source), and standard tool knowledge. Do not restate what they can read for themselves.
- **Match length to task size.** A small task produces a small plan. One-line config changes do not need phases or a multi-paragraph Goals section.
- **Don't confabulate.** If you don't know whether a package, file, or API exists, verify it or mark it as an unknown. Do not write confident-sounding specifics you haven't checked.
- **Don't implement.** Do not edit any source files outside of `plans/`.
- **One file, always.** All concerns go in a single plan file.
