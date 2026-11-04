# Move the start-agent.sh image to Debian trixie

## Status

- [x] Switch `FROM` to `debian:trixie-slim` and refresh the bookworm comments in the Dockerfile (Haiku ok)
- [x] Make `tests/verify-tool-refresh.sh` read the base image from the Dockerfile instead of hardcoding it (Haiku ok) — already satisfied: since 7334782 (the base/tools split, written after this plan) check 4 compares `latest` against the `claude-agent:base` tag and no longer names a Debian tag; an older-base `:previous` is INFO, not FAIL
- [ ] Build on the host via `start-agent.sh --refresh-tools`, which keeps bookworm as `claude-agent:previous` (human gate: run `.temp/trixie-rollout.sh` on the macOS host; it covers this phase and the next)
- [ ] Live checks: `tests/verify-tool-refresh.sh`, plus a doc-toolchain and searxng-mcp smoke test (covered by `.temp/trixie-rollout.sh`; read `.verify/trixie-rollout.log`)
- [ ] Check and fix `docpython` after the trixie bump (human gate: host rebuild and live check, only if a fix is needed)
  - Read the `docpython (the /usr/local/bin symlink) finds the venv` result in `.verify/trixie-rollout.log`. If it passes on Python 3.13, record that in the plan and tick this phase with no code change.
  - If it fails: replace the `ln -sf /opt/doc-tools/venv/bin/python /usr/local/bin/docpython` symlink in `dockerfiles/claude-agent.Dockerfile` with something that runs the venv's interpreter by its own path, e.g. a two-line wrapper script (`exec /opt/doc-tools/venv/bin/python "$@"`). Add a static check to `tests/test_dockerfile.py` that `docpython` is not a bare symlink to the venv interpreter. Write it so it fails against the current Dockerfile.
  - The fix is in the base Dockerfile, so `--refresh-tools` won't pick it up. Write a one-off host script in `.temp/` that rebuilds `claude-agent:base` (the same `docker build` as `ensure_base_image`, logged), runs `start-agent.sh --backend=omlx --refresh-tools`, then checks in the container that `docpython -c "import sys, docx; print(sys.prefix)"` prints `/opt/doc-tools/venv` and that the documented command in `templates/global-claude.md` works. This refresh moves the trixie image without the fix to `:previous` and drops the bookworm `:previous`, so a bookworm rollback is then only possible through `claude-agent:base-bookworm`. Say so in the script's header.
- [ ] Docs: ADR-057 (ADR-055 and ADR-056 were taken after this plan was written), ADR-054 amendment, README note on rollback, and the `docpython` result or fix (Opus recommended)

## Decisions during /build (2026-11-04)

- **Rollout method changed (user's choice).** The Approach below predates 7334782, which split the image into `claude-agent:base` (this Dockerfile) and `claude-agent:latest` (the OpenCode/Pi npm layer `FROM claude-agent:base`). `--refresh-tools` now rebuilds only `latest` on the existing base, so it would not pick up the new `FROM`; `--rebuild` deletes `:previous`. Chosen: a one-off host script, `.temp/trixie-rollout.sh`, that tags the bookworm base `claude-agent:base-bookworm`, builds a trixie `claude-agent:base` with the same `docker build` `ensure_base_image` runs (logged), then runs `start-agent.sh --refresh-tools`, which builds `latest` on it and saves the bookworm `latest` as `:previous`. No change to start-agent.sh. Rollback: `--restore-previous-image` (latest back to bookworm); a full rollback also retags `claude-agent:base-bookworm` as `claude-agent:base` so a later refresh builds on bookworm. `--rebuild` does not remove `claude-agent:base-bookworm`; the docs phase should say to `docker image rm` it once trixie has settled. The "rebuilding from the npm layer onward" message concern in the Approach no longer applies, and check 4's `:previous` mismatch is already INFO, not FAIL.
- **`docpython` is broken on bookworm already (found by the dry run in the current container).** `/usr/local/bin/docpython` is a symlink to `/opt/doc-tools/venv/bin/python`; started through it, Python 3.11 does not find the venv (`sys.prefix` is `/usr`), so `docpython -c "from docx import Document"` (the command `templates/global-claude.md` documents) fails with `ModuleNotFoundError`. The venv's own interpreter works. The rollout script checks it separately. The `docpython` phase in Status (added at the user's request) checks the trixie result and fixes it if it is still broken.

## Context

The agent image (`dockerfiles/claude-agent.Dockerfile:14`) is `FROM debian:bookworm-slim`, a floating tag. Bookworm is now oldstable. Only the agent image is in scope: `start-claude.sh:44` (`CLAUDE_CONTAINER_IMAGE`) stays on bookworm, because its bwrap/sandbox-runtime surface (ADR-047) needs its own re-verification. The README and CLAUDE.md bookworm mentions are about start-claude.sh and stay as they are. The Colima VM's own `apt-get` calls (`start-agent.sh:934`, `:1027`) run on the VM's OS, not on this image, so they are unaffected.

Checked during planning:
- Every apt package in the Dockerfile has a trixie package page on packages.debian.org (`fd-find`, `ripgrep`, the LibreOffice components, all five font packages, `pandoc`, `poppler-utils`, `jq`, `zip`/`unzip`).
- Trixie's `python3` is 3.13.
- NodeSource's `setup_lts.x` installs from the distro-agnostic `nodistro` repo, so it doesn't depend on the Debian codename.

Other places that mention bookworm:
- Dockerfile comments at lines 3 and 86 (the PEP 668 note, which applies to trixie too).
- `tests/verify-tool-refresh.sh:117`, which hardcodes `debian:bookworm-slim` to count base layers for its layer-reuse check.

## Goals

- `claude-agent:latest` builds from `debian:trixie-slim` (a floating tag, not a digest pin; the user chose this, and the existing `apt-get upgrade` keeps security fixes coming in).
- Claude Code, OpenCode, Pi, SearXNG MCP (`opencode run` calling the searxng tool on omlx), uv, the doc toolchain (`soffice`, `pandoc`, `pdfinfo`, `docpython`) and `rg`/`fdfind` all work in the new container.
- Rolling back to the bookworm image is a single command.
- Nothing hardcodes the base image tag except the Dockerfile's `FROM` line.

## Approach

Roll out through `--refresh-tools`, not `--rebuild`. `--refresh-tools` (`start-agent.sh:1355-1369`) tags the current image `claude-agent:previous` and then rebuilds. A changed `FROM` invalidates every layer whatever the cache-bust arg says, so this gives a full trixie build that `--restore-previous-image` can undo. `--rebuild` deletes the old image first and leaves no rollback. One side effect: the refresh prints "rebuilding from the OpenCode/Pi npm layer onward", which is wrong for this run. Mention it in the ADR rather than changing the message.

On that transition run, expect check 4 of `verify-tool-refresh.sh` ("refresh kept the layers above npm") to fail, because `:previous` is bookworm-based and shares no layers with `:latest`. That failure is correct. The fix to line 117 is about later runs: once the tag is read from the Dockerfile's `FROM`, the layer count stays right after the switch.

Per the project's host-commands-as-scripts convention, build and verify steps that run on the macOS host go in a script (or reuse `tests/verify-tool-refresh.sh`). Don't paste command blocks. Live inference uses `gemma-4-e4b-it-6bit` only, which the verify script already does.

## Unknowns / To Verify

- **Python 3.13 compatibility of the baked venvs.** `mcp[cli]<2`, `httpx`, `python-docx`, `openpyxl` and `python-pptx` are installed into venvs that `uv venv` creates from the system `python3`. This is expected to work but wasn't checked. Verify by building, importing each package with the venv's interpreter, and checking that OpenCode reports the `searxng` MCP as connected (`verify-tool-refresh.sh` covers the MCP part). If `uv venv` turns out to pick a uv-managed Python rather than the system one, note that, but no change is needed.
- **Doc-toolchain behaviour after the major-version jumps.** LibreOffice, pandoc and poppler all move to newer upstream releases in trixie. Smoke-test a docx→pdf conversion with `soffice --headless`, `pdfinfo` on the output, and a Markdown→docx conversion with `pandoc`. The commands are in `templates/global-claude.md:78-95`. If a conversion breaks, decide whether it's a trixie regression worth a workaround or a reason to hold the upgrade.
- **Image size change.** It's informational only, but record the before and after sizes (`docker image ls claude-agent`) in the ADR, since the `:previous` image keeps both on disk until the next refresh.
