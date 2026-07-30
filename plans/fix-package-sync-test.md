# Fix the failing package-sync test

## Status

- [ ] Add `unzip zip` to `start-claude.sh`'s apt block (Haiku ok)
- [ ] Make `_parse_apt_packages` collect every `--no-install-recommends` block, not just the first
- [ ] Add a doc-tools exclusion set + staleness guard, mirroring `_SANDBOX_ONLY_PACKAGES`
- [ ] Run the suite; confirm both `test_package_sync.py` tests pass

## Context

`tests/test_package_sync.py::test_dockerfile_and_start_claude_package_lists_match`
has been red since `623f4f8` (2026-06-23), which added `unzip zip` to
`dockerfiles/claude-agent.Dockerfile:25` without the matching change to
`start-claude.sh:246-250`. The commit recorded no rationale and no ADR — it
reads as an oversight, not a deliberate divergence.

While reading the test, a second defect surfaced. `_parse_apt_packages`
(`tests/test_package_sync.py:12-35`) `break`s out of its scan loop at the end of
the first apt block rather than resuming the search. The Dockerfile's second
block (`claude-agent.Dockerfile:64-70`, the LibreOffice/pandoc/poppler doc-tools
stack) is therefore never parsed and never compared. The test is quietly
narrower than its name and docstring claim, and the doc-tools divergence passes
today only by accident of that bug.

Current state of the three apt blocks:

| Block | Contents |
|---|---|
| `claude-agent.Dockerfile:21` | base toolchain + `unzip zip` |
| `claude-agent.Dockerfile:64` | doc-tools stack (10 packages) — currently invisible to the test |
| `start-claude.sh:246` | base toolchain + `bubblewrap socat libseccomp2` |

Neither file's `apt-get install -y nodejs` line is in scope — the parser keys on
`--no-install-recommends`, which those lines lack.

## Goals

- `unzip`/`zip` present in both base images.
- The parser compares every `--no-install-recommends` block in both files.
- The doc-tools divergence is excluded *explicitly*, with a cited rationale,
  rather than by parser accident.
- Both tests in `test_package_sync.py` pass; no other test regresses.

## Approach

Converge the images rather than widen the test's exclusion list. The existing
`_SANDBOX_ONLY_PACKAGES` exclusion earns its place because ADR-033 gives
`bubblewrap`/`socat`/`libseccomp2` an architectural reason to differ — the
bubblewrap sandbox would need `CAP_SYS_ADMIN` in an unprivileged Docker
container. `unzip`/`zip` have no such reason, so the honest fix is to make
reality match the test's premise, not to teach the test to ignore the drift.

The doc-tools stack is the opposite case and *does* warrant an exclusion: its
absence from `claude-dev` is a documented, intentional property of that
environment (see the "Differences in claude-dev" section of
`templates/global-claude.md`, which tells agents the office toolchain is not
present there). Once the parser sees the second block, that divergence must be
declared deliberately — with the same shape and staleness guard as the sandbox
set, so a future reader can tell "intended" from "drifted" at a glance.

## Steps

1. **Add `unzip zip`** to the package list at `start-claude.sh:246-250`. Match
   the surrounding grouping style; note that this block's last line has no
   trailing backslash, so placement matters for the continuation.

2. **Fix the parser** at `tests/test_package_sync.py:12-35` so that reaching the
   end of one apt block resets block state and resumes scanning for the next
   `--no-install-recommends` line, instead of terminating the whole scan. Both
   existing block terminators must keep working: a line beginning `&&` (the
   Dockerfile's `&& apt-get upgrade -y`) and a line without a trailing
   backslash (`start-claude.sh`'s final package line). A line that ends one
   block must still be eligible to start the next.

3. **Declare the doc-tools divergence.** Add a module-level exclusion set beside
   `_SANDBOX_ONLY_PACKAGES` (`test_package_sync.py:38-42`) covering the ten
   packages in `claude-agent.Dockerfile:64-70`, with a comment citing why
   `claude-dev` omits the office stack. Subtract it from `only_in_dockerfile`,
   the mirror of how the sandbox set is subtracted from `only_in_start_claude`.
   Update the failure message so both exclusion sets are named.

4. **Add the symmetric staleness guard.** `test_sandbox_only_packages_present_in_start_claude`
   (`test_package_sync.py:63-70`) exists so the sandbox exclusion can't silently
   outlive its packages. Add the doc-tools counterpart asserting those packages
   are still in the Dockerfile, so a future slim-down of the image forces the
   exclusion set to be revisited rather than leaving it as dead cover.

5. **Verify.** `uv run --with pytest pytest tests/ -q`. Expect
   `test_package_sync.py` green and the rest of the suite unchanged. Confirm the
   parser fix is load-bearing by checking that removing the doc-tools exclusion
   makes the test fail with exactly those ten packages — if it still passes, the
   second block is not actually being parsed.

## Notes

- **The image change needs a rebuild to land.** `start-claude.sh --rebuild`
  removes the `claude-dev:latest` image and the project container; existing
  containers keep the old package set until then. The test is static analysis,
  so it goes green without a rebuild — don't read that as the image being
  updated.
- **Sequencing against in-flight work.** The working tree currently carries the
  SearXNG MCP fix (`dockerfiles/searxng-mcp/server.py`, the Dockerfile's shim
  install line, `start-agent.sh`, and two test files). This plan touches
  `start-claude.sh` and `tests/test_package_sync.py` only — no overlap, but both
  changes want `--rebuild` on their respective scripts.
- **Scope boundary.** `_PKG_NAME` and the `apt-get install -y nodejs` lines are
  deliberately untouched. Widening the parser to unpinned or non-`--no-install-recommends`
  installs is a separate question from the one this plan closes.
