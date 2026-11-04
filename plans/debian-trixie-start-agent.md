# Move the start-agent.sh image to Debian trixie

## Status

- [x] Switch `FROM` to `debian:trixie-slim` and refresh the bookworm comments in the Dockerfile (Haiku ok)
- [x] Make `tests/verify-tool-refresh.sh` read the base image from the Dockerfile instead of hardcoding it (Haiku ok) — already satisfied: since 7334782 (the base/tools split, written after this plan) check 4 compares `latest` against the `claude-agent:base` tag and no longer names a Debian tag; an older-base `:previous` is INFO, not FAIL
- [ ] Build on the host via `start-agent.sh --refresh-tools`, which keeps bookworm as `claude-agent:previous`
- [ ] Live checks: `tests/verify-tool-refresh.sh`, plus a doc-toolchain and searxng-mcp smoke test
- [ ] Docs: ADR-055, ADR-054 amendment, README note on rollback (Opus recommended)

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
