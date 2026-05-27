# Seed Claude Code settings.json with a permissive allowlist

## Status

- [ ] Add `templates/global-claude-settings.json` with the curated defaults (showThinkingSummaries, coauthorTag, permissions.allow / permissions.deny)
- [ ] Update `start-claude.sh` settings injection (around `start-claude.sh:297-326`) to also seed `permissions` when absent
- [ ] Update `start-agent.sh` `init_sandbox()` (around `start-agent.sh:179-216`) to write the curated settings.json into `.sandbox_config/claude/settings.json` at init time
- [ ] Update `start-agent.sh` always-run injection (around `start-agent.sh:1112-1140`) to seed `permissions` only when absent, matching the showThinkingSummaries pattern
- [ ] Document the new behavior in `README.md` and add an ADR entry in `ADR.md`
- [ ] Add a test under `tests/` asserting the template is well-formed JSON and that `git push` is *not* in the allow list

## Context

Both scripts already manage `settings.json` non-destructively (`start-claude.sh:297-326`, `start-agent.sh:1112-1140`): they merge `showThinkingSummaries: true` and `coauthorTag: "none"` into an existing file, or write a fresh inline JSON when none exists. Neither script seeds a `permissions` block, so Claude Code falls back to its default permission prompt behavior — which is noisy inside a container sandbox where many normally-prompted operations are already isolated by the VM/container boundary.

The container is genuinely a sandbox: in `start-agent.sh` it sits behind a VM-level tinyproxy allowlist (`CLAUDE.md` "Network Egress" section) and only the active sandbox's filesystem is visible; in `start-claude.sh` it is a per-project microVM with no host filesystem access beyond the project mount. Pre-approving read-only and safe-local-mutation commands is therefore reasonable; destructive or remote-affecting operations (push, publish, rm -rf, curl|sh) should still prompt.

The user has chosen: ship a curated default in `templates/`, target both scripts, scope the allowlist to **read-only + safe local mutations** (deny push/publish/destructive), and apply the seed only on first creation — not on `--rebuild` or `--reset-container`. The existing "merge only if key absent" pattern naturally implements this: once the user (or a prior run) has written a `permissions` block, neither script will touch it again.

## Goals

- A new template file under `templates/` carries the curated `settings.json` (replacing the inline JSON in both scripts).
- New `start-agent.sh` sandboxes created via `--init-sandbox` get the curated settings.json written to `.sandbox_config/claude/settings.json` at init time.
- New `start-claude.sh` containers (first run, when `~/.claude-containers/shared/settings.json` does not yet exist) get the same curated settings.json.
- Existing sandboxes / shared dirs that already have a `settings.json` get `permissions` merged in **only if the key is absent**. User edits are never clobbered.
- The allowlist permits common dev-loop operations (git read, git commit, file edits, build/test, package installs, ripgrep/fd/jq, etc.) and explicitly denies remote-affecting and destructive ops (`git push`, `git push --force`, `npm publish`, `rm -rf /`, `curl … | sh`, etc.).
- `--rebuild` and `--reset-container` do **not** re-seed `permissions` — auth state and any user edits to the allowlist survive untouched. (Existing files in `.sandbox_config/claude/` are already untouched by these flags; this plan does not change that.)

## Approach

Extract the inline `{"showThinkingSummaries": true, "coauthorTag": "none"}` JSON from both scripts into a single source of truth at `templates/global-claude-settings.json`, and extend it with `permissions.allow` / `permissions.deny`. Both scripts then `cat` (or `cp`) the template when seeding fresh, and the existing Python merge blocks gain one additional non-destructive branch that adds `permissions` only when absent.

The non-destructive merge is what makes the "only on init" semantics work without a new flag. If a user customizes the allowlist later, subsequent runs leave it alone. If a user wants to fully refresh the template, they delete the `permissions` key (or the whole file) and re-run — symmetric with how the existing `--reseed-allowlist` flag works for the tinyproxy allowlist.

For the allowlist content itself: keep entries as Claude Code permission strings (e.g., `Bash(git diff:*)`, `Bash(git commit:*)`, `Read`, `Edit`). Deny entries should use the precise patterns that target the dangerous variants (`Bash(git push:*)`, `Bash(git push --force:*)`, `Bash(npm publish:*)`). Order matters in Claude Code permissions (deny wins), so denies cover any allow patterns that might otherwise overlap.

## Unknowns / To Verify

- **Exact `permissions` schema in current Claude Code settings.json.** The shape (`permissions.allow: string[]`, `permissions.deny: string[]`, plus the `Bash(<cmd>:*)` glob syntax) should be confirmed against the installed Claude Code version's docs before finalizing the template. Verify via `claude --help` / official docs or by inspecting a settings.json that has been edited through `/permissions` in a live session. Affects: every step that touches the template.
- **Whether `permissions.allow` patterns must be exact-prefix or support shell globs.** Some Claude Code versions use a custom matcher rather than glob. If only exact-prefix matching is supported, entries like `Bash(git commit -m:*)` may need to be broadened to `Bash(git commit:*)`. Verify before finalizing the allow list.
- **Whether deny patterns take precedence over allow patterns when they overlap.** The plan assumes deny wins (standard semantics). Confirm with current docs; if not, the allow patterns need to be narrowed to exclude the dangerous variants directly.

## Notes

- The plan deliberately does *not* introduce a `--reseed-settings` flag. If experience shows users want one, it can be added later with semantics symmetric to `--reseed-allowlist` (overwrite `permissions` from template, preserve other keys).
- Consider whether `start-agent.sh` should also propagate the same allowlist to OpenCode / Pi via their respective settings files. Out of scope for this plan — those tools have different permission models and warrant a separate decision.
- The deny list should include `Bash(git push --force-with-lease:*)` and `Bash(git push -f:*)` alongside `Bash(git push --force:*)` since all three shapes appear in the wild.
