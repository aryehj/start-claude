# Fix: opencode-ai postinstall skipped by npm allow-scripts

## Status

- [x] Add `--allow-scripts=opencode-ai,@google/genai,protobufjs` to the `npm install -g` at `dockerfiles/claude-agent.Dockerfile:52` (Haiku ok)
- [ ] Rebuild (`start-agent.sh --rebuild`) and verify `opencode --version` and `pi --version` both run inside the container, and the allow-scripts warning is gone from the build log

## Context

`claude-agent.Dockerfile:33` installs `npm@latest` (npm 11.6+), whose new default does **not** run dependency lifecycle scripts unless they are explicitly allowlisted. Step 7 (`:52`):

```
npm install -g opencode-ai@latest @earendil-works/pi-coding-agent@latest
```

then emits `npm warn allow-scripts 3 packages have install scripts not yet covered`, naming `opencode-ai` (`postinstall: node ./postinstall.mjs`), `protobufjs` (`postinstall: node scripts/postinstall`), and `@google/genai` (preinstall no-op).

opencode-ai ships its native binary via `optionalDependencies` (`opencode-linux-arm64@1.17.9` on this arm64 image) with `bin.opencode` → `bin/opencode.exe`, which `postinstall.mjs` is responsible for materializing. With that postinstall suppressed, the `opencode` command is broken at runtime even though the image builds successfully and `tests/test_dockerfile.py::test_opencode_installed_via_npm` — which only greps for the install line — stays green. `protobufjs`'s skipped postinstall (code generation) degrades pi's google-genai provider path.

This is Finding #1 from `plans/diagnose-the-output-of-elegant-swing.md`. Per the user's scope decision, only #1 is addressed and the fix is kept minimal — no build-time smoke check, no test change.

## Goals

- `opencode` and `pi` both run inside a freshly rebuilt claude-agent container.
- The allow-scripts warning for all three packages is absent from the build output.
- No change to the image's "always `@latest`" posture; findings #2–#5 untouched.

## Approach

Use npm's own grounded remediation — the per-install `--allow-scripts=<csv>` flag, with the package list taken verbatim from the build warning — rather than pinning npm to a pre-11.6 release. The flag is surgical (named packages only, not a blanket "run all scripts"), preserves `npm@latest`, and is exactly what the warning instructs. Pinning npm would fight the repo's unpinned-tooling design and forfeit other npm updates.

The edit is the single RUN at `:52`: pass `--allow-scripts=opencode-ai,@google/genai,protobufjs` to `npm install -g` alongside the existing package specs.

## Unknowns / To Verify

- **Does the postinstall succeed at build time, and does opencode then work?** Build-time egress is unrestricted inside the Colima VM (`--network=host`, ADR-011), so the optional-dep platform binary and any postinstall step should resolve. Confirm post-rebuild via the Status step above (`opencode --version` / `pi --version`). If `opencode` still fails, check that the `opencode-linux-arm64` optional dep actually installed — `postinstall.mjs` depends on it being present.
- **Inert if npm is ever pinned older:** npm accepts unknown CLI config keys rather than erroring, so `--allow-scripts=...` is harmless on a pre-allow-scripts npm. Note only; not blocking.
