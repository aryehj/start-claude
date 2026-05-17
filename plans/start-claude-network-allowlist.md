# Wire start-claude.sh's project sandbox to a reusable network allowlist

## Status

- [ ] **Phase 0: Verify enforcement (decision gate).** Confirm with a runtime test that `sandbox.network.allowedDomains` actually blocks egress inside the Apple Containers microVM, and confirm wildcard-vs-apex matching semantics from sandbox-runtime source. If enforcement fails, this plan is abandoned in favor of `start-claude-egress-firewall.md` — every bullet below is moot. See Phase 0 section.
- [ ] Build `templates/allowlist.txt` as three labeled sections, in this order, deduped: (1) Claude Code's egress NO_PROXY hosts (the `ay4` array — `anthropic.com`/`*.anthropic.com`, `registry.npmjs.org`, `jsr.io`, `npm.jsr.io`, `pypi.org`, `files.pythonhosted.org`, `index.crates.io`, `proxy.golang.org`, plus the localhost/RFC1918 literals); (2) Claude Code's WebFetch auto-allow set (the `KcO` set — ~100 docs/SDK hosts); (3) `start-agent.sh`'s curated allowlist (`start-agent.sh:337-428`+). Per-entry curation: see Open question §1.
- [ ] In `start-claude.sh`, seed `$PROJECT_DIR/.claude/allowlist.txt` from the template on first run; honor a `--reseed-allowlist` flag for forced re-seed
- [ ] Extend the `settings.local.json` migration block to materialize the allowlist into `sandbox.network.allowedDomains` on every invocation — and apply the same materialization to the fresh-file branch (`start-claude.sh:122-135`) so first-run projects aren't skipped. Clobber policy: see Open question §2.
- [ ] Document the new file, flag, and reuse rationale in `README.md` and `CLAUDE.md`; add a short ADR entry recording the decisions (including Phase 0 findings and the finding that neither the NO_PROXY nor the WebFetch list is actually a sandbox default — both must be explicitly added)
- [ ] Add a unit test covering the parse + wildcard-expansion helper

## Context

`start-claude.sh` currently configures the Claude Code sandbox (`@anthropic-ai/sandbox-runtime`, see `start-claude.sh:85-137`) for filesystem isolation only — `sandbox.network` is unset, which under the runtime's allow-only model means "no network access" in principle, but in practice the project doesn't run Claude in network-isolated mode today because `network` is absent from the schema-required surface. The sibling `start-agent.sh` ships a curated default-deny allowlist (`start-agent.sh:337-428` and below) used by the in-VM tinyproxy; that list is the institutional knowledge we want to reuse. `research.py`'s denylist uses the inverse paradigm (default-allow + blocked domains via Squid) and does not compose with Claude sandbox's allow-only network model — out of scope per user direction.

Claude sandbox's domain matching is **not** suffix-based — `matchesDomainPattern` in `/usr/lib/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/sandbox-manager.js:45-60` supports only exact host match or `*.example.com` wildcard form. `start-agent.sh`'s allowlist relies on tinyproxy's natural suffix semantics (`github.com` matches `api.github.com`). The materialization step must bridge that gap by emitting each bare entry as both the exact apex and a `*.<apex>` wildcard.

## Goals

- A single curated allowlist lives in the repo (`templates/allowlist.txt`) and is the source of truth for both `start-claude.sh` and, eventually, `start-agent.sh` (this plan does not migrate `start-agent.sh`, but does not foreclose it either).
- `start-claude.sh` produces a working `sandbox.network.allowedDomains` block in `$PROJECT_DIR/.claude/settings.local.json` for every project, derived from a per-project, user-editable `.claude/allowlist.txt`.
- The materialized `allowedDomains` correctly approximates tinyproxy's suffix semantics inside Claude sandbox's stricter matcher.
- Re-running `start-claude.sh` reflects host-side edits to the allowlist without requiring `--rebuild`.
- No bind mounts, no in-container daemons — only the Claude sandbox affordances declared in `sandbox.network.*`.

## Approach

The cleanest mapping is: keep one human-edited file per project, mechanically translate it into JSON each launch. We pick **per-project + materialize-into-settings** (per user direction) because (a) different projects have different network needs, (b) Claude sandbox already reads `settings.local.json` on every session, so writing into it is the smallest possible integration surface, and (c) avoiding a bind mount keeps this orthogonal to the Apple Containers mount topology.

The non-obvious risk is the suffix-vs-exact-vs-wildcard mismatch. The allowlist file uses tinyproxy semantics (`github.com` ⇒ everything ending in `.github.com` and the apex itself). The materializer must expand each non-wildcard entry to `[entry, "*." + entry]`. Lines that are already wildcards (`*.foo.com`) pass through; lines that are IP literals pass through unchanged. Comments and blanks are dropped. Sort + dedupe before write so diffs are stable.

The allowlist is regenerated on every invocation — the migration block in `start-claude.sh:88-120` already runs unconditionally on the existing-settings path, so we extend it there. This makes the file mutable and self-healing without a separate `--reload` flag; `--reseed-allowlist` is only needed to overwrite a user-modified `.claude/allowlist.txt` with the current template.

## Phase 0: Verify enforcement (decision gate)

Two questions must be answered with a real test before any other work begins. If Question A resolves "no," abandon this plan (move to `_paused`) and pick up `start-claude-egress-firewall.md` as the active path — the rest of this design assumes the runtime sandbox filters network in the microVM.

**Question A — does `sandbox.network.allowedDomains` enforce inside the Apple Containers microVM?** ADR-033 documents that `start-agent.sh` force-disables `sandbox.enabled` because bubblewrap can't run in unprivileged Docker. `start-claude.sh`'s microVM path is different (sandbox deps shipped, strict mode per `start-claude.sh:108-115`), but the runtime's network-namespace + Unix-socket-proxy mechanism is new ground for this repo.

Test:
1. In a throwaway project, write `.claude/settings.local.json` with strict sandbox and `sandbox.network.allowedDomains: ["api.anthropic.com", "localhost", "127.0.0.1"]` only.
2. `start-claude.sh` into it.
3. From a sandboxed bash:
   - `curl -sS --max-time 5 https://example.com >/dev/null && echo LEAK || echo blocked`
   - `curl -sS --max-time 5 https://api.anthropic.com >/dev/null && echo reached || echo unreachable`
4. Capture any `@anthropic-ai/sandbox-runtime` stderr / log output.

Pass: `example.com → blocked`, `api.anthropic.com → reached`. Fail: `example.com → LEAK`. On fail, record runtime version + observed behavior in this plan's Notes section, mark the plan abandoned, and escalate.

**Question B — wildcard vs. apex matching.** The materializer's current design expands every bare entry to `[entry, "*." + entry]`. Confirm that expansion is necessary and sufficient.

1. With `allowedDomains: ["github.com"]` only: does `curl https://api.github.com` reach?
2. With `allowedDomains: ["*.github.com"]` only: does `curl https://github.com` reach?
3. Read `/usr/lib/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/sandbox-manager.js` (the `matchesDomainPattern` function the plan cites). Paste the actual 5–10 lines into this plan's Notes appendix so the expansion rule is grounded in source, not inference.

Output: a one-paragraph "expansion rule" decision in Notes. The materializer in subsequent phases implements exactly what these tests show is required — no more, no less.

### Acceptance

Phase 0 produces a short writeup committed to this plan (or a sibling note file): runtime version tested, curl results for both questions, quoted source for the matcher, and an explicit go/no-go decision.

## Open questions (not Phase 0)

1. **Curation policy when merging the three input lists.** `claude-default-domains-not-in-allowlist.md` documents that Claude Code's `KcO` (WebFetch auto-allow) set contains hosts `start-agent.sh` *intentionally omits* — `huggingface.co` (uploads), `kaggle.com` (uploads), `github.com/anthropics` (write surface; also path-prefix, not host-only). Merging all three lists undoes that curation by default. Decide before building `templates/allowlist.txt` whether to (a) honor `start-agent.sh`'s omissions and drop the conflicting `KcO` entries, (b) include everything and accept the looser default, or (c) split into two templates and pick at seed time.

2. **`settings.local.json` clobber semantics.** `settings.local.json` is user-editable. "Materialize on every invocation" needs a rule for what happens when a user has hand-edited `sandbox.network.allowedDomains` between runs. Pick one: (a) full replace (predictable, destroys ad-hoc edits silently); (b) union (preserves user adds but never lets removals propagate); (c) sentinel-managed block (a comment-marked region the script owns, everything outside it survives — JSON makes this awkward). Document the choice in the ADR.

## Notes

- `start-agent.sh`'s in-script heredoc allowlist (`start-agent.sh:337-428` and the rest of the block) should become a copy in `templates/allowlist.txt`; converting `start-agent.sh` itself to read from the template is a follow-up, not in this plan.
- Keep the templated comment block intact when seeding — it explains why `github.com`, `huggingface.co`, container registries, etc. are intentionally omitted; that rationale is the most valuable part of reusing this list.
- Sandbox `network` schema lives in `/usr/lib/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/sandbox-config.d.ts:43-115`; if a newer runtime adds a `suffixMatch: true` mode, the wildcard expansion can be retired.
