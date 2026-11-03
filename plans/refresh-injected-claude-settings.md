# Refresh injected Claude Code settings

## Status

- [x] Write failing tests for the shared settings helper (Opus recommended)
- [x] Extract the global-settings merge into one shared helper and call it from both scripts
- [x] Replace `coauthorTag` with `attribution`, and remove the stale key from existing files
- [x] Force `prefersReducedMotion: true`
- [x] Widen the push/publish deny list and merge it into existing files on every run (Opus recommended)
  - Verified against the real CLI (2.1.100, 2.1.182, 2.1.317) with `tests/probe-deny-rules.py`, which needs no account: a mock Messages API on 127.0.0.1 plus PATH shims. Phase 6 should list it, and `.verify/`, in CLAUDE.md's layout.
- [x] Update the template, its tests, README, CLAUDE.md, and add an ADR (Haiku ok)

## Context

Both scripts inject the same global `~/.claude/settings.json` preferences using two near-identical inline Python heredocs: `start-claude.sh:391-439` and `start-agent.sh:1262-1304`. Both start from `templates/global-claude-settings.json`. `start-agent.sh:212-216` also copies the template at `--init-sandbox`. Project-level `settings.local.json` injection is separate (`start-claude.sh:99-209`, `start-agent.sh:1728-1765`), and this plan leaves it alone.

**Freshness audit.** Checked against the current settings reference (https://code.claude.com/docs/en/settings-reference.md, fetched 2026-11-03; the container runs Claude Code 2.1.317):

- `coauthorTag` **does not exist.** It doesn't appear anywhere in the reference. Commit and PR attribution are controlled by `attribution` (sub-keys `commit`, `pr`, `sessionUrl`; or `attribution: false`), which replaced the deprecated `includeCoAuthoredBy` in v2.0.62. So today nothing actually suppresses attribution. This session still gets told to add `Co-Authored-By` trailers, even though `coauthorTag: "none"` is set.
- `showThinkingSummaries`, `theme` (including `"dark-ansi"`), `prefersReducedMotion`, `spinnerTipsEnabled`, `effortLevel`, and every `sandbox.*` key the project blocks write are all current. The scripts already strip `sandbox.seccomp`, which is also absent from the reference.
- `prefersReducedMotion` is not injected anywhere. The project blocks only *delete* it from `settings.local.json` (`start-claude.sh:177`, `start-agent.sh:1746`), on the grounds that it belongs in global settings. That deletion is worth keeping: a project-local value would override the global one.

**Push exposure.** The template's `permissions` block, including `deny: Bash(git push*)`, is only seeded when `settings.json` has no `permissions` key at all. An existing file never picks up deny-list changes. The permissions doc (https://code.claude.com/docs/en/permissions.md, "what a Bash rule doesn't match") confirms a few things. `Bash(git push *)` does not catch `git -C . push`, `git -c k=v push`, or `git 'push'`. Deny rules apply to any subcommand of a compound command. And these rules are explicitly "not a security boundary". The template's own allow list also opens publish paths that the deny list misses: `Bash(cargo*)` covers `cargo publish`, `Bash(uv*)` covers `uv publish`, and `Bash(git config*)` lets Claude define a git alias that runs push under another name. Neither image installs `gh`. At the network level, push is already mostly blocked: `github.com` is absent from both allowlists (CLAUDE.md, ADR-044/049). So these deny rules are defense in depth.

## Goals

- One shared helper owns the global-settings merge. Both scripts call it, and both reach the same result for a missing file and for an existing one.
- Every run forces `prefersReducedMotion: true`.
- Every run forces attribution off using the `attribution` key, and removes `coauthorTag` from existing files.
- Every run union-merges the template's push/publish deny rules into `permissions.deny`. The user's other allow, deny, and ask entries stay untouched.
- The existing behaviors stay: `showThinkingSummaries` forced true, `effortLevel` removed, `theme` promoted from unset/`auto` to `dark-ansi`, and the full template `permissions` seeded when the key is absent.

## Approach

Move the duplicated Python into one stdlib-only helper script in the repo, run on the host by both scripts with `python3`, next to `templates/`. Both scripts already resolve the repo directory for the template path, so they can resolve the helper the same way. The "file missing" branch should become "start from the template, then apply the same rules", which removes the hard-coded `echo '{...coauthorTag...}'` fallbacks. A helper in its own file can be unit-tested from pytest directly, instead of only being checked indirectly through the template.

Each key gets one of three treatments. Keep the distinction visible in the helper's code:

- **forced**: overwritten every run.
- **merged**: entries added if missing, never removed.
- **seeded**: written only if absent.

Attribution and reduced motion are forced, the same as `showThinkingSummaries` is today. The push deny entries are merged. The rest of `permissions` stays seeded (ADR-039).

For attribution, write `{"commit": "", "pr": "", "sessionUrl": false}`, not `attribution: false`. The docs say `false` needs v2.1.281 or later, and older versions reject the whole settings file that contains it. `~/.claude` is shared across containers whose cached images may run older CLIs, so a rejected file would lose every setting at once. The docs recommend the empty-string form for files that older versions also read.

## Push/publish deny set

Add these to the template's `deny` list. The helper merges them into existing files.

- **git push in the forms Claude actually writes:** the current prefix rule, plus forms with a leading `-C <dir>` or `-c <k=v>` before the subcommand.
  - Trade-off: a rule shaped like `git -C * push*` also matches, for example, `git -C dir commit -m "push fix"`. Accept that, because a deny produces a visible refusal rather than a silent failure.
  - Don't use a bare `git * push*`. It would block ordinary commits whose message mentions push.
- **Defining git aliases**, so push can't run under another name. Deny `git config` invocations that set `alias.*`, at both global and local scope.
- **Publish commands the allow list would otherwise approve:** `cargo publish`, `uv publish`, and `docker push`.
  - Keep the existing npm/yarn/pnpm publish rules.
- **`gh` write paths**, in case `gh` is ever installed: `gh pr create`/`merge`, `gh release`, `gh repo create`, and `gh api`, since `gh api` can POST.

Don't remove or narrow entries in the template's `allow` list. Deny takes precedence over allow, and the user chose to leave the rest of their permissions alone.

## Unknowns / To Verify

- **Which CLI versions read the shared `~/.claude`.** The empty-string `attribution` form is chosen to be safe for older versions. Before relying on `sessionUrl`, verify that an older CLI doesn't reject it: run `claude --version` in a container started from a cached image and look for a settings-file error at startup. If it does reject it, drop `sessionUrl`, which only affects cloud and Remote Control sessions. Depends on: the attribution step.
  - **Resolved (Phase 3):** older CLIs do reject it. With `CLAUDE_CONFIG_DIR` set to a scratch dir, and an `apiKeyHelper` marker showing whether the file loaded, 2.0.62 and 2.1.100 skip a settings file containing `attribution.sessionUrl`, but load `{"commit": "", "pr": ""}`. 2.1.182 and 2.1.317 accept both. The changelog dates `attribution` to 2.0.62 and `sessionUrl` to 2.1.183. `sessionUrl` was dropped. Separately, 2.0.62 also skips the template's `permissions` block, at HEAD as well: that version rejects the `Bash(x*)` rule form (`Bash(x:*)` loads), and 2.1.100 accepts both forms. This predates this plan and is left alone.
- **Whether attribution actually changes.** After implementing, start a session with the new settings and check whether the system-reminder still tells Claude to add `Co-Authored-By`. Depends on: the attribution step.
  - **Resolved (after Phase 6):** checked by capturing the requests that CLI 2.1.317 sends to a mock Messages API (`claude -p "commit my changes"` in a scratch git repo, scratch `CLAUDE_CONFIG_DIR`). With `{"coauthorTag": "none"}`, or with no attribution key, the requests contain `Co-Authored-By` and `Generated with`. With the new template, they contain neither. Live sessions pick this up once a launch script has run the helper against `~/.claude/settings.json`.

## Notes

**Helper interface.** `tests/test_global_settings_helper.py` pins it in its docstring: `merge_global_settings.py` at the repo root, stdlib-only and Python 3.9-compatible, CLI `python3 merge_global_settings.py SETTINGS_PATH TEMPLATE_PATH`, plus `apply_rules`, `merge_file`, and `main`. Tests for later phases carry `PENDING_PHASE_<N>` skip markers. The worker for phase N deletes that marker's definition and its uses.

**Inventory of what the scripts inject today** (after this plan, `coauthorTag` → `attribution` and reduced motion is added):

Global `~/.claude/settings.json`, both scripts:

| Key | Value | Treatment | Why |
|---|---|---|---|
| `showThinkingSummaries` | `true` | forced | Makes thinking visible in the transcript |
| `coauthorTag` | `"none"` | forced (**stale key, ignored by Claude Code**) | Meant to suppress Co-Authored-By; replace with `attribution` |
| `effortLevel` | — | removed | Kept unpinned on purpose (ADR-017) |
| `theme` | `"dark-ansi"` | promoted from unset/`auto` | `auto` misreads Ghostty's background (ADR-040) |
| `permissions.allow` / `deny` | template lists | seeded if `permissions` absent | Curated allowlist plus push/publish deny (ADR-039) |

Project `.claude/settings.local.json`, `start-claude.sh`:

| Key | Value | Treatment | Why |
|---|---|---|---|
| `sandbox.enabled`, `sandbox.autoAllowBashIfSandboxed` | `true` | seeded on new file (bool form migrated) | Sandbox on by default |
| `sandbox.filesystem.allowWrite` | uv-cache and `.venv` paths under `/tmp` and `$TMPDIR` | merged | uv works inside the read-only sandbox mounts (ADR-001/004/007) |
| `sandbox.failIfUnavailable` | `true` | forced | Strict mode |
| `sandbox.allowUnsandboxedCommands` | `false` | forced | Strict mode |
| `sandbox.enableWeakerNestedSandbox` | `true` | forced | bwrap under Apple Containers' masked `/proc` (ADR-047) |
| `sandbox.network.allowedDomains` / `deniedDomains` | template expanded to `d` and `*.d` / `[]` | seeded; `--reseed-sandbox-allowlist` overwrites | Default-deny sandbox egress (ADR-044) |
| `sandbox.seccomp` | — | removed | Obsolete override |
| `theme`, `spinnerTipsEnabled`, `prefersReducedMotion` | — | removed | Belong in global settings |

Project `.claude/settings.local.json`, `start-agent.sh`:

| Key | Value | Treatment | Why |
|---|---|---|---|
| `sandbox.enabled` | `false` | forced | The VM firewall is the isolation layer (ADR-033) |
| `sandbox.failIfUnavailable`, `sandbox.allowUnsandboxedCommands` | — | removed | Not applicable with the sandbox off |
| `theme`, `spinnerTipsEnabled`, `prefersReducedMotion` | — | removed | Belong in global settings |

Keys in your live `settings.json` that the scripts don't manage, such as `remoteControlAtStartup`, `agentPushNotifEnabled`, and `autoCompactEnabled`, are all current keys. They're out of scope. `agentPushNotifEnabled` controls phone notifications, not git push.

For the ADR: record the `attribution` replacement, the forced/merged/seeded split, and the shared helper. ADR-039's references to `coauthorTag` become partially superseded. Docs that name `coauthorTag` and need updating: `README.md:113`, `README.md:398`, `ADR.md` (ADR-039), and CLAUDE.md's `templates/` layout line and permissions bullet.
