# Port office document-editing toolchain into start-claude.sh (claude-dev)

## Status

- [ ] Phase 1: Port the toolchain — apt office layer, baked venv, `docpython` wrapper, env persistence, docs, static tests
- [ ] Phase 2: Human gate — `--rebuild` + in-sandbox smoke test; resolve soffice/python behavior under bubblewrap and document any claude-dev-specific invocation

## Context

`claude-agent` (built from `dockerfiles/claude-agent.Dockerfile`) ships a full
headless office stack — LibreOffice Writer/Calc/Impress, Office-metric fonts,
pandoc, poppler-utils, `file`/libmagic, and a baked `/opt/doc-tools` venv
(python-docx/openpyxl/python-pptx) exposed via a `docpython` wrapper. See the
Dockerfile's two blocks: the office apt layer at `dockerfiles/claude-agent.Dockerfile:66-84`
and the doc-tools venv + `docpython` wrapper at `:100-117`. This was built by
`plans/add-document-editing-tools.md` and hardened by
`plans/fix-docpython-and-file-command.md`.

`claude-dev` (built by `start-claude.sh`) ships **none** of it. Its setup runs
as an inline `bash -c '...'` heredoc inside a temporary `debian:bookworm-slim`
container (`start-claude.sh:241-309`), exported and rebuilt `FROM scratch`. The
apt layer there (`:246-250`) currently installs only base tooling plus
`unzip zip`; there is no office toolchain, no baked venv, no `docpython`. The
global CLAUDE.md seeded into every session
(`templates/global-claude.md`) scopes the "Office Document Toolchain" section to
claude-agent (`:67-96`) and explicitly tells claude-dev users the toolchain is
**not** present (`:118-122`).

This plan brings claude-dev to rough capability parity with claude-agent. The
mechanism differs (inline heredoc vs Dockerfile; no `ENV` directive available),
but the resulting capability and its documentation should match.

Relevant environment facts (all grounded):
- Base image is identical — `debian:bookworm-slim` (`start-claude.sh:42`,
  `dockerfiles/claude-agent.Dockerfile:14`) — so **apt package names carry over
  verbatim**; no font/package-name re-verification needed.
- Build-time network in the Apple Containers microVM is unrestricted (no proxy),
  so build-time `apt` (deb.debian.org) and `uv pip` (PyPI) both work. Baking the
  venv at build time means the libraries need no runtime egress.
- claude-dev runs Claude Code's bash commands inside a **bubblewrap sandbox**
  where `/tmp` and `/root/.cache` are read-only and only `$TMPDIR` is writable
  (`CLAUDE.md` claude-dev section; `templates/global-claude.md:124-125`). This
  is the one environment claude-agent does **not** have, and the source of the
  load-bearing uncertainty in Phase 2.
- The microVM has the same low-entropy CRNG risk that motivated
  `PYTHONHASHSEED=0` in claude-agent (`dockerfiles/claude-agent.Dockerfile:16-24`,
  and the "maybe fix python hang" commit) — CPython's startup `getrandom(2)` can
  block. This applies to claude-dev too.

## Goals

- An agent in claude-dev can render/convert `.docx`/`.xlsx`/`.pptx` to PDF (and
  between office formats) headlessly via `soffice --headless --convert-to`, and
  verify page count via `pdfinfo`.
- Office files render with the same Office-metric fonts as claude-agent, so
  pagination approximates Word/Excel/PowerPoint.
- `python-docx`, `openpyxl`, `python-pptx` are present with no runtime install
  step, reachable through a `docpython` entrypoint that resolves the venv's
  site-packages (i.e. not the bare-symlink bug fixed for claude-agent).
- `pandoc` and `file`/libmagic are on `PATH`.
- Python invocations do not hang on the microVM's unseeded CRNG.
- `templates/global-claude.md` presents the toolchain as available in **both**
  environments; the stale "not present in claude-dev" note is corrected.
- Static tests guard the claude-dev install the way `tests/test_dockerfile.py`
  guards claude-agent, so a regression fails CI without a rebuild.

## Approach

Mirror claude-agent's two-mechanism split inside the inline heredoc: an apt
layer for the native toolchain and a baked `/opt/doc-tools` venv for the Python
libs, with a `docpython` wrapper. Two claude-dev-specific adaptations:

1. **No `ENV` directive.** The final image is `FROM scratch + ADD rootfs.tar`
   with only `CMD`, so persistent env must be written the way the existing UV
   vars already are — into `/etc/profile.d/*.sh` and `/root/.bashrc`
   (`start-claude.sh:280-291`). `PYTHONHASHSEED=0` and the `UV_PYTHON_*` pins
   (`UV_PYTHON_DOWNLOADS=never`, `UV_PYTHON_PREFERENCE=system`; see
   `dockerfiles/claude-agent.Dockerfile:25-29`) belong there. The `docpython`
   wrapper additionally bakes `PYTHONHASHSEED=0` inline so it holds even if the
   ambient env is stripped inside the sandbox — exactly the claude-agent wrapper
   form (`:113-116`).

2. **Keep the office packages in a separate apt layer**, not appended to the
   system-packages block at `start-claude.sh:246-250`. `tests/test_package_sync.py`
   parses only the *first* `--no-install-recommends` block in each file and
   requires the two to match; claude-agent's office packages live in a second
   apt layer for the same reason. Appending office packages to the first block
   would break that sync test. A separate layer also mirrors the Dockerfile's
   structure for legibility.

The `docpython` entrypoint must be the wrapper form, never a bare
`ln -sf .../venv/bin/python /usr/local/bin/docpython` — that second-order symlink
resolves past the venv and drops its site-packages off `sys.path` (root cause
documented in `plans/fix-docpython-and-file-command.md`).

## Phase 1: Port the toolchain

### Steps

1. **Office apt layer.** In the setup heredoc (`start-claude.sh:241-309`), add a
   dedicated apt layer — separate from the system-packages block at `:246-250`
   (see Approach #2) — installing the same set as
   `dockerfiles/claude-agent.Dockerfile:76-84`: `libreoffice-writer`
   `libreoffice-calc` `libreoffice-impress`, the fonts `fonts-liberation`
   `fonts-crosextra-carlito` `fonts-crosextra-caladea` `fonts-dejavu`
   `fonts-roboto`, `pandoc`, `poppler-utils`, and `file`. Use
   `--no-install-recommends` and end with `rm -rf /var/lib/apt/lists/*`,
   consistent with the surrounding layers. Package names are known-good on
   bookworm (same base image as claude-agent) — no re-verification needed.

2. **Baked doc-tools venv + `docpython` wrapper.** After the apt layer, mirror
   `dockerfiles/claude-agent.Dockerfile:100-117`: `uv venv /opt/doc-tools/venv`,
   then `uv pip install --python /opt/doc-tools/venv/bin/python python-docx
   openpyxl python-pptx`, then write `/usr/local/bin/docpython` as an executable
   wrapper that does `exec env PYTHONHASHSEED=0 /opt/doc-tools/venv/bin/python
   "$@"` and `chmod +x` it. Use the wrapper form, not a bare symlink (see
   Approach). Note the heredoc-inside-heredoc quoting: the outer setup is already
   a single-quoted `bash -c '...'`, so the wrapper heredoc must use the same
   `'"'"'DOCPYEOF'"'"'` escaping already used for the UVEOF/GITEOF heredocs at
   `:280-308`.

3. **Persist env vars.** Add `PYTHONHASHSEED=0`, `UV_PYTHON_DOWNLOADS=never`, and
   `UV_PYTHON_PREFERENCE=system` to both the `/root/.bashrc` and
   `/etc/profile.d/uv-cache.sh` writes (`start-claude.sh:280-291`), alongside the
   existing UV vars. Rationale for each: `PYTHONHASHSEED=0` prevents the
   getrandom hang for ad-hoc `python3`/`uv run` (the wrapper already covers
   `docpython`); the `UV_PYTHON_*` pins keep `uv init`/`uv run` on the baked
   system python3 instead of fetching a managed CPython whose first hop is
   github.com — which is off the claude-dev sandbox allowlist and would stall
   (same reasoning as `dockerfiles/claude-agent.Dockerfile:21-24`).

4. **Documentation.** In `templates/global-claude.md`, rescope the "Office
   Document Toolchain" section (`:67-96`) so it applies to **both** environments
   rather than "(claude-agent)" only — the invocation commands (`soffice
   --headless --convert-to`, `pdfinfo`, `docpython`, `pandoc`) are identical.
   Then fix the claude-dev differences section: remove or rewrite the "No office
   document toolchain" bullet (`:118-122`), which is now false. If Phase 2
   surfaces a claude-dev-only invocation wrinkle (e.g. a soffice
   `-env:UserInstallation` flag for the read-only sandbox HOME), document that
   caveat here.

5. **Static tests.** Add a test module (e.g. `tests/test_start_claude_doc_tools.py`)
   that reads `start-claude.sh` and asserts, in the substring style of
   `tests/test_dockerfile.py:28-95`: the LibreOffice components, fonts, `pandoc`,
   `poppler-utils`, and `file` are installed; the three Python libs go into the
   `/opt/doc-tools` venv; `docpython` is the exec-wrapper form and **not** a bare
   symlink; and `PYTHONHASHSEED=0` is set. Reuse the helpful-failure-message
   convention from `test_dockerfile.py`. Confirm `tests/test_package_sync.py`
   still passes unchanged (it will, if the office packages are in a second apt
   layer — see Approach #2).

### Acceptance criteria

- `pytest tests/` passes, including the existing `test_package_sync.py` (proves
  the office layer stayed out of the first apt block) and the new assertions.

## Phase 2: Human gate — rebuild + in-sandbox verification

The one thing static tests cannot prove: that the toolchain actually works
**inside claude-dev's bubblewrap sandbox**, which claude-agent does not have.
`soffice --headless` wants a writable HOME/profile dir, but under the sandbox
`/tmp` and `/root/.cache` are read-only and only `$TMPDIR` is writable — so a
plain `soffice --convert-to` may fail or hang on a profile lock, and Python may
hang on the unseeded CRNG if the env pin didn't take. This phase resolves both
and may feed a documentation fix back into Phase 1 step 4.

### Steps

1. Run `start-claude.sh --rebuild` to rebuild `claude-dev:latest` with the new
   layers. Record the image-size delta (LibreOffice is large; make it a known
   quantity).

2. Inside the resulting container, exercise the toolchain **from a sandboxed
   bash command** (not just an interactive shell, so the read-only mounts are in
   effect): create a trivial `.docx` with `docpython` (`from docx import
   Document`), `soffice --headless --convert-to pdf --outdir $TMPDIR <file>`,
   and confirm `pdfinfo` reports the expected page count. Spot-check `import
   openpyxl`, `import pptx`, `pandoc --version`, and `file --version`.

3. If `soffice` fails or hangs on a profile/lock error, retry with
   `-env:UserInstallation=file://$TMPDIR/lo` pointing the profile at the
   sandbox-writable temp dir, and — if that is the working invocation — document
   it in `templates/global-claude.md` (Phase 1 step 4) so agents use it. If a
   bare `python3`/`docpython` invocation hangs, confirm `PYTHONHASHSEED=0` is
   actually present in the sandboxed env and fix the persistence path.

### Acceptance criteria

- End-to-end docx→PDF→page-count round-trip succeeds from a sandboxed bash
  command in claude-dev, matching what claude-agent can do.
- `templates/global-claude.md` reflects the actual working invocation, including
  any sandbox-specific flag discovered here.

## Notes

- **Existing containers won't auto-update.** The tools arrive on the next
  `--rebuild`; the CLAUDE.md guidance reaches existing sandboxes only via the
  reseed path (`--reseed-global-claudemd`, per `start-claude.sh:191`) or a manual
  copy. Mention to the user if they have long-lived containers.
- **Commit style:** this repo omits `Co-Authored-By` lines (`CLAUDE.md` "Commit
  style"). Commit the plan and the implementation separately per repo habit.
- **Out of scope:** any sandbox-allowlist change — build-time apt/PyPI need none,
  and the baked venv needs no runtime egress. No ADR is strictly required (this
  is parity work mirroring existing decisions), but if one is added, note it
  follows the claude-agent doc-tooling precedent.
