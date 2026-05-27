# Re-seed Pi/OpenCode agent skills on fresh-container path

## Status

- [ ] Extract a `seed_agent_skills` helper from the inline block in `init_sandbox()` (`start-agent.sh:196-209`); keep the destination/source paths and the per-skill `rm -rf` + `cp -R` idempotency
- [ ] Stop seeding inside `init_sandbox()` — leave only the `mkdir -p ".../agents/skills"` (line 193) so the bind-mount target exists; the next container creation does the seeding
- [ ] Call `seed_agent_skills` from the fresh-container path next to `sync_skills` (`start-agent.sh:1533`). Both `--rebuild` and `--reset-container` remove the container, which routes execution through this path; reattach skips both (matches Claude Code skills' behavior — that's the intent)
- [ ] Update CLAUDE.md ADR-038 bullet ("`--init-sandbox` seeds these into …") and the "small-model skill ports seeded … via `--init-sandbox`" line in the `start-agent.sh key decisions` section to reflect: seeding now runs on every fresh container, identical trigger surface as Claude Code skills
- [ ] Update README.md where it describes when skills appear (if any prose currently ties seeding to init time)

## Context

`start-agent.sh` has two separate skill-seeding mechanisms and they don't align:

- **Claude Code skills** (`~/.claude/skills/`) — `sync_skills()` at `start-agent.sh:1403`, called from the fresh-container path at line 1533. Re-runs on `--rebuild` and `--reset-container`; skipped on reattach because `attach_existing` `exec`s out (line 1510) before reaching `sync_skills`.
- **Pi/OpenCode agent skills** (`/root/.agents/skills/`) — copy loop inlined in `init_sandbox()` at `start-agent.sh:196-209`, called only from the `--init-sandbox` one-shot at line 235 (which `exit 0`s immediately after). Nothing re-seeds an existing sandbox; manual `cp -R` is the only update path.

Symptom: a Gemma session in a sandbox initialized before commit `829f854` found `/root/.agents/skills/` empty even after `start-agent.sh --rebuild`, because `--rebuild` never re-enters `init_sandbox()`.

The two skill kinds share their on-host source-of-truth model differently — Claude Code skills come from a remote tarball (`$CLAUDE_SKILLS_ARCHIVE_URL`), agent skills from the local `skills-agents/` tree in this repo. Per discussion, agent skills stay local: this repo is the canonical source and a network fetch for files already on disk would be a regression.

## Goals

- `start-agent.sh --rebuild` and `start-agent.sh --reset-container` repopulate `$SANDBOX/.sandbox_config/agents/skills/` from `skills-agents/`, the same way they repopulate Claude Code skills.
- Bare `start-agent.sh` on an existing container does not re-seed (matches Claude Code skills; attach path stays fast).
- `--init-sandbox` still produces a usable sandbox: the bind-mount target exists; the first subsequent `start-agent.sh` run seeds it as a side effect of creating the container.
- Docs (ADR-038, CLAUDE.md key-decisions, README) describe the new trigger surface.

## Notes

- The current inline loop already does `rm -rf "$dest/$name"` per skill before copying, so calling it on every fresh container is naturally idempotent and picks up edits or new skill directories. No flag needed.
- Local-only skill directories under `.sandbox_config/agents/skills/` (skills not present in `skills-agents/`) are left alone by the per-skill loop. Worth preserving that property — don't switch to a wholesale `rm -rf $dest && cp -R`.
- No change needed at the bind-mount layer (`start-agent.sh:1562`) or the mkdir at `start-agent.sh:1068`; both already exist and remain correct.
- If a future change moves agent-skill source to upstream tarball (parallel to `sync_skills`), the `CLAUDE_AGENT_SKILLS_ARCHIVE_URL` override pattern is the natural extension point — out of scope here.
