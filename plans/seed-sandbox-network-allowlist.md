# Seed a default-deny sandbox network allowlist in start-claude.sh

## Status

- [ ] Phase 1: Validate `sandbox.network` enforcement and Claude Code connectivity in-container
- [ ] Phase 2: Full list port, migration semantics, flag, docs/ADR/tests

## Context

`start-claude.sh` runs a per-project Apple Containers microVM with Claude Code's
bubblewrap sandbox active (`@anthropic-ai/sandbox-runtime`, installed at
`start-claude.sh:226`; `--cap-add SYS_ADMIN` at the `container run` invocation,
see ADR-043). The script already manages the project `sandbox` config in
`.claude/settings.local.json` (`start-claude.sh:97-154`): it migrates existing
files (bool→object, `filesystem.allowWrite`, `failIfUnavailable=true`,
`allowUnsandboxedCommands=false`) and writes a fresh file when none exists.

Today there is **no network restriction** — the seeded global CLAUDE.md tells
agents exactly that (`templates/global-claude.md:64-66`: "No network proxy or
allowlist — full outbound egress is available"). The microVM is the isolation
boundary; egress at the VM level is unrestricted.

`start-agent.sh` takes the opposite stance: a curated **default-deny** egress
allowlist (`start-agent.sh:350-637`), enforced by an in-VM tinyproxy with the
allowlist bind-mounted `:ro` so the agent cannot edit it. Its philosophy:
read-only hosts only, upload-capable hosts (`github.com`, container/image
registries, `huggingface.co`, data repositories) deliberately omitted, with a
large curated research/reference/docs corpus.

This change ports that allowlist into `start-claude.sh` via Claude Code's own
`sandbox.network.allowedDomains` setting.

## Goals

- Every newly-created `.claude/settings.local.json` gets an **active**
  `sandbox.network` block: deny-by-default egress, allowing only the curated
  list. The migration block adds the same block to existing files that lack it.
- The curated list is the **full `start-agent.sh` allowlist**, ported verbatim
  in content, sourced from a new `templates/` seed file (not hard-coded in the
  script).
- Sandboxed bash egress (`curl`, `git`, `npm`, `uv`, …) is restricted to the
  list; off-list hosts are blocked.
- Docs reflect reality: the global CLAUDE.md egress note, project `CLAUDE.md`
  key-decisions, README, and a new ADR.

## Approach

Two enforcement models are in play and the difference is the central design
fact. `start-agent.sh` enforces at the VM/proxy layer with a `:ro` mount — the
agent **cannot** lift its own restriction. In `start-claude.sh`,
`settings.local.json` lives in the project tree and is writable by the agent, so
the network allowlist is a **guardrail** (reduce accidental exfil / off-list
fetches), not a hard boundary — the microVM remains the real boundary. Record
this honestly in the ADR; do not oversell it as isolation parity.

Two semantic gaps between the source list and the target mechanism must be
bridged:

1. **Matching.** The tinyproxy allowlist is suffix-matching (`github.com` also
   matches `api.github.com`). Claude Code's `allowedDomains` matches exact host
   plus `*.`-wildcards (`github.com` matches only `github.com`; `*.github.com`
   matches subdomains). To preserve start-agent.sh semantics, each apex domain
   `d` must expand to **both** `d` and `*.d`. The template stores bare domains
   (same idiom as `start-agent.sh`'s allowlist); the injection logic does the
   expansion when building the JSON array. Confirm `*.d` multi-level behavior in
   Phase 1.

2. **Source of truth.** A new `templates/sandbox-allowlist.txt` (bare domains +
   comments, mirroring `start-agent.sh:350-637`). `start-claude.sh` reads it.
   Do **not** refactor `start-agent.sh` to share the file in this change — its
   enforcement path and matching semantics differ; sharing is a possible later
   DRY step, noted as a non-goal.

Build out behind a Phase 1 live validation, because enforce-by-default is a
behavior change whose blast radius (does Claude Code itself still work? does
`git push`/SSH survive? does the documented schema actually enforce in this
container?) must be known before committing the full list and rewriting docs.

## Unknowns / To Verify

- **`sandbox.network.allowedDomains` enforcement in this container.** Verified
  against current Claude Code docs (code.claude.com/docs/en/sandboxing): network
  config lives at `sandbox.network`, deny-by-default once `allowedDomains` is
  populated, exact + `*.`-wildcard matching, companion fields `deniedDomains`,
  `allowUnixSockets`, `allowLocalBinding`. **Must be smoke-tested live** in
  Phase 1 — schema details across Claude Code versions are the load-bearing risk.
- **Does the network allowlist govern Claude Code's own API traffic?** Expected
  answer: no — Claude Code's Node process runs outside the sandbox, so
  `api.anthropic.com` connectivity is unaffected; only sandboxed *bash* egress
  is filtered. If wrong, deny-by-default would brick the session. Verify in
  Phase 1 (start a session, confirm Claude responds while egress is restricted).
- **Non-HTTP egress (SSH `git push`, `git@github.com:22`).** The proxy is
  HTTP/HTTPS SNI-based; how SSH egress behaves under the sandbox (blocked,
  bypassed, or proxied) is unknown. Verify in Phase 1; document the outcome.
- **`*.d` matching depth** — does `*.foo.com` match `a.b.foo.com`? Confirm in
  Phase 1; if single-level only, the expansion strategy may need adjustment.

## Phase 1: Validate `sandbox.network` enforcement and Claude Code connectivity in-container

### Steps

1. In a throwaway project, hand-write a minimal `sandbox.network` block into
   `.claude/settings.local.json` alongside the existing sandbox config —
   `allowedDomains` with a tiny set (e.g. `anthropic.com`, `*.anthropic.com`,
   `pypi.org`, `*.pypi.org`, `pythonhosted.org`, `*.pythonhosted.org`).
2. Launch the container via `start-claude.sh` and confirm Claude Code starts and
   responds (resolves: does the allowlist break Claude's own API traffic).
3. From a sandboxed Bash tool call, verify an **on-list** fetch succeeds
   (`curl https://pypi.org`) and an **off-list** fetch is blocked
   (`curl https://example.com`). Confirms deny-by-default enforcement.
4. Test `*.` matching: confirm a subdomain on-list via wildcard
   (`curl https://files.pythonhosted.org`) succeeds and that the apex-only entry
   does not leak subdomains. Determine multi-level wildcard depth.
5. Test non-HTTP egress: attempt an SSH `git` operation to `github.com` and a
   plain `git push` over HTTPS; record whether each is blocked or allowed.
6. Record findings (especially the github.com/SSH outcome and any field-name or
   structural corrections to the schema) directly into this plan's Notes before
   starting Phase 2.

### Acceptance criteria

- Confirmed: deny-by-default blocks off-list hosts and allows list + `*.`
  entries from sandboxed bash.
- Confirmed: Claude Code session functions normally under the restriction.
- Documented: behavior of SSH/`git push` to `github.com` (which the full list
  omits), so Phase 2 can decide whether to note it or add an exemption.

## Phase 2: Full list port, migration semantics, flag, docs/ADR/tests

### Steps

1. **Create `templates/sandbox-allowlist.txt`** — port the full domain content
   of `start-agent.sh:350-637` (bare domains + section comments, same idiom).
   Keep the read-only philosophy: `github.com`/registries/`huggingface.co`/data
   repos stay omitted unless Phase 1 shows a coding workflow truly breaks.
2. **Refactor the settings-injection block** (`start-claude.sh:97-154`) so the
   network list is built from the template in both branches. The fresh-file case
   can no longer be a fully-static heredoc; route both fresh and existing files
   through the Python block, passing the template path as an additional argv.
   Parse the template (strip comments/blanks), expand each domain `d` to `d` and
   `*.d`, and set `sandbox.network.allowedDomains` to the result.
3. **Migration semantics — seed-if-absent, do not reconcile per-entry.** If
   `sandbox.network.allowedDomains` (or the `network` key) is absent, seed the
   whole block. If present, leave it untouched. This deliberately differs from
   the existing `filesystem.allowWrite` append-each-missing behavior
   (`start-claude.sh:114-119`): the network list is large and user-pruned
   entries must stay pruned. State this contrast in the ADR.
4. **Add a `--reseed-sandbox-allowlist` flag** mirroring the existing
   `--reseed-global-claudemd` pattern (`start-claude.sh:19,26,165-167`):
   overwrite the current project's `sandbox.network.allowedDomains` from the
   template. There is no "reload" analog — Claude Code reads settings at
   startup, so re-attach picks up changes on next launch.
5. **Update `templates/global-claude.md:64-66`** — replace "No network proxy or
   allowlist — full outbound egress is available" with an accurate description:
   sandboxed bash egress is now restricted to a default-deny allowlist in
   `.claude/settings.local.json` (`sandbox.network.allowedDomains`), editable
   per-project; the microVM itself still has full egress at the VM level; note
   how to add a host and the `--reseed-sandbox-allowlist` flag. Reflect the
   Phase 1 finding about `github.com`/SSH.
6. **Update project `CLAUDE.md`** — add a key-decision bullet under the
   `start-claude.sh` "Key decisions" list summarizing the seeded default-deny
   network allowlist, template source of truth, and guardrail-not-boundary
   framing, pointing at the new ADR.
7. **Update `README.md`** where the sandbox / settings.local.json behavior is
   described, if applicable.
8. **Write ADR-044** (next number after ADR-043) capturing: the decision to seed
   an active default-deny `sandbox.network` allowlist; full start-agent.sh list
   ported via `templates/sandbox-allowlist.txt`; the `d` + `*.d` expansion and
   the suffix-vs-wildcard semantic difference vs start-agent.sh; seed-if-absent
   migration (vs allowWrite append); guardrail-not-boundary rationale (writable
   `settings.local.json`, microVM is the real boundary); and the Phase 1 finding
   on non-HTTP/github egress.
9. **Add a test** following `tests/test_settings_template.py` /
   `tests/test_research.py` patterns: assert `templates/sandbox-allowlist.txt`
   exists and is well-formed (non-empty, parseable, comments/blanks ignored),
   and a static check that `start-claude.sh` injects
   `sandbox.network.allowedDomains` and expands to both bare and `*.` forms.

### Acceptance criteria

- A fresh `start-claude.sh` run produces a `settings.local.json` whose
  `sandbox.network.allowedDomains` contains every template domain in both `d`
  and `*.d` form, with egress denied by default.
- An existing `settings.local.json` lacking a `network` block gets one; one that
  already has user-customized `allowedDomains` is left untouched.
- Docs no longer claim full unrestricted egress for the sandbox.

## Notes

- **`github.com` / `git push` consequence.** The ported list omits `github.com`
  (start-agent.sh's read-only stance). Under deny-by-default, `gh` and HTTPS
  pushes to `github.com` will be blocked from sandboxed bash. Unlike
  start-agent.sh, the user can fix this trivially by adding `github.com` /
  `*.github.com` to their own writable `settings.local.json`. Surface this in
  the global CLAUDE.md note. Phase 1 determines whether SSH push is also
  affected.
- **Non-goal:** refactoring `start-agent.sh` to read the same template. Its
  enforcement (proxy + `:ro` mount) and matching (suffix) differ; unifying is a
  separate change.
