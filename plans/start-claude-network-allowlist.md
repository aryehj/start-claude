# Wire start-claude.sh's project sandbox to a reusable network allowlist

## Status

- [ ] Build `templates/allowlist.txt` as three labeled sections, in this order, deduped: (1) Claude Code's egress NO_PROXY hosts (the `ay4` array — `anthropic.com`/`*.anthropic.com`, `registry.npmjs.org`, `jsr.io`, `npm.jsr.io`, `pypi.org`, `files.pythonhosted.org`, `index.crates.io`, `proxy.golang.org`, plus the localhost/RFC1918 literals); (2) Claude Code's WebFetch auto-allow set (the `KcO` set — ~100 docs/SDK hosts); (3) `start-agent.sh`'s curated allowlist (`start-agent.sh:337-428`+)
- [ ] In `start-claude.sh`, seed `$PROJECT_DIR/.claude/allowlist.txt` from the template on first run; honor a `--reseed-allowlist` flag for forced re-seed
- [ ] Extend the `settings.local.json` migration block to materialize the allowlist into `sandbox.network.allowedDomains` (with `deniedDomains: []`) on every invocation
- [ ] Document the new file, flag, and reuse rationale in `README.md` and `CLAUDE.md`; add a short ADR entry recording the decisions (including the finding that neither the NO_PROXY nor the WebFetch list is actually a sandbox default — both must be explicitly added)
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

## Unknowns / To Verify

- **Does `sandbox.network.allowedDomains` actually take effect inside the Apple Containers microVM environment that `start-claude.sh` builds?** ADR-033 documents that the sibling `start-agent.sh` force-disables `sandbox.enabled` because bubblewrap can't run in unprivileged Docker; the `start-claude.sh` microVM path is different (it ships the sandbox deps and runs sandbox in strict mode per `start-claude.sh:108-115`), but the network-namespace + Unix-socket-proxy mechanism described in the sandbox-runtime README is new ground for this repo. Verify with a smoke test before relying on the design — e.g., set `allowedDomains: ["anthropic.com"]` only and confirm `curl example.com` is blocked from inside a sandboxed bash. If the network sandbox does not function in the microVM, the plan still produces a correct settings file but its effect must be reported honestly in `CLAUDE.md`/`README.md` rather than implied.
- **Wildcard semantics for apex vs. apex-only entries.** Confirm via the same smoke test that `["github.com", "*.github.com"]` lets both `github.com` and `api.github.com` resolve, and that omitting one breaks the other. The decision to emit both forms hinges on this; if `*.github.com` already covers the apex on this runtime version, the expansion can be simplified.

## Notes

- `start-agent.sh`'s in-script heredoc allowlist (`start-agent.sh:337-428` and the rest of the block) should become a copy in `templates/allowlist.txt`; converting `start-agent.sh` itself to read from the template is a follow-up, not in this plan.
- Keep the templated comment block intact when seeding — it explains why `github.com`, `huggingface.co`, container registries, etc. are intentionally omitted; that rationale is the most valuable part of reusing this list.
- Sandbox `network` schema lives in `/usr/lib/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/sandbox-config.d.ts:43-115`; if a newer runtime adds a `suffixMatch: true` mode, the wildcard expansion can be retired.
