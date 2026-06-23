# Add document / spreadsheet / presentation editing tooling to claude-agent

## Status

- [x] Add LibreOffice headless, Office-metric fonts, pandoc, and poppler-utils to `dockerfiles/claude-agent.Dockerfile`
- [x] Bake an `/opt/doc-tools` venv (python-docx, openpyxl, python-pptx) and expose a stable interpreter entrypoint
- [x] Document the tooling in `templates/global-claude.md` (claude-agent-scoped) and note its absence in the claude-dev section *(Haiku ok)*
- [x] Add static install-line assertions to `tests/test_dockerfile.py` *(Haiku ok)*
- [ ] `start-agent.sh --rebuild`, then smoke-test conversion + page-count inside the container

## Context

The image an agent runs in (the `claude-agent` Colima/docker container) ships
`python3` plus `uv` but **no office-document toolchain**: no LibreOffice, no
metric-compatible fonts, no pandoc, no PDF utilities, and no pre-installed
Python document libraries. In practice this means an agent asked to edit a
`.docx`/`.xlsx`/`.pptx` can reach for `python-docx`/`openpyxl` via `uv run`, but
has **no way to render the result, convert between formats, or even verify a
page count** — which is exactly how a recent résumé-editing task went wrong
(styling drifted and "≤2 pages" could not be checked in-environment).

Per repo convention (`CLAUDE.md:140-143`), image-level tooling belongs in
`dockerfiles/claude-agent.Dockerfile` (built by `start-agent.sh`), **not** in
`start-agent.sh` itself. The user named `start-agent.sh`, but the correct edit
site for installed tools is the Dockerfile it builds. Scope is **claude-agent
only** — `start-claude.sh`'s inline image is intentionally left unchanged.

Relevant existing structure:
- System apt layer: `claude-agent.Dockerfile:19-26`.
- Baked-venv precedent (the pattern to mirror): the searxng-mcp venv at
  `claude-agent.Dockerfile:62-65` — `uv venv /opt/.../venv` then
  `uv pip install --python .../venv/bin/python ...`. This exists because
  Debian bookworm's system Python is PEP 668 externally-managed, so
  `uv pip install --system` hard-fails (also documented in
  `templates/global-claude.md` "Python & uv").
- Build-time network: `docker build` runs with `--network=host` (ADR-011),
  bypassing the tinyproxy allowlist — so `apt-get` against `deb.debian.org`
  works **at build time** even though it is blocked at runtime. PyPI
  (`pypi.org`, `pythonhosted.org`) is on the runtime allowlist
  (`templates/*allowlist*.txt:41-42`), but baking the venv at build time means
  the libraries are present regardless of runtime egress.
- `templates/global-claude.md` is seeded into every session as environment
  context (and to `AGENTS.md` for OpenCode). It already scopes env-specific
  sections to claude-agent (e.g. "Network Egress (claude-agent)") and has a
  "## Differences in claude-dev (start-claude.sh)" section at line 64.
- Test convention: `tests/test_dockerfile.py` does plain substring assertions
  that required install lines exist in the Dockerfile.

## Goals

- An agent in `claude-agent` can **render and convert** `.docx`/`.xlsx`/`.pptx`
  to PDF (and between office formats) headlessly via LibreOffice.
- An agent can **verify page count / layout** of a generated document
  (LibreOffice → PDF, then `pdfinfo`).
- Office files render with **Office-metric-compatible fonts** so layout and
  pagination approximate what the user sees in Word/Excel/PowerPoint.
- `python-docx`, `openpyxl`, and `python-pptx` are **present without a runtime
  install step**, reachable through one stable interpreter path.
- `pandoc` is available for Markdown↔docx conversions.
- The global container CLAUDE.md tells agents these tools exist and how to
  invoke them, so they are actually used.

## Approach

Two install mechanisms, matching how each kind of dependency already enters the
image: **apt at build time** for the native toolchain (LibreOffice, fonts,
pandoc, poppler-utils) — safe because the build bypasses the proxy — and a
**baked `/opt/doc-tools` venv** for the Python libraries, mirroring the
searxng-mcp venv so the PEP 668 constraint is handled the same proven way and
the libraries don't depend on runtime PyPI egress. Expose the venv through one
stable entrypoint (a `/usr/local/bin` symlink, e.g. `docpython`) so docs and
agents have a single path to call rather than memorizing `/opt/...`.

Fonts are not optional polish: without Calibri/Cambria metric equivalents
(Carlito/Caladea) and Arial/Times equivalents (Liberation), LibreOffice
substitutes fonts and pagination diverges from Word — defeating the page-count
goal. Roboto is included because user documents in this environment have used
it directly.

The whole change ships together and is verified by one build + smoke test, so
this is a flat checklist, not phased work. The only real risks are
package-name and headless-dependency details, captured under Unknowns.

## Unknowns / To Verify

- **Exact font package names in bookworm.** Decision: install Liberation
  (Arial/Times/Courier metrics), Carlito (Calibri), Caladea (Cambria), DejaVu,
  and Roboto. Verify the precise apt names before building — at least
  `fonts-liberation` / `fonts-liberation2`, `fonts-crosextra-carlito`,
  `fonts-crosextra-caladea`, `fonts-dejavu`; confirm Roboto's package name
  (`fonts-roboto` vs `fonts-roboto-unhinted`) exists in bookworm
  (`apt-cache search` in a throwaway build step, or check
  packages.debian.org/bookworm). Drop any name that doesn't resolve rather
  than failing the build.
- **Headless LibreOffice dependency surface.** `--no-install-recommends` keeps
  the image smaller but may omit something `soffice --headless` needs. Install
  the component packages (`libreoffice-writer`, `libreoffice-calc`,
  `libreoffice-impress`; `libreoffice-core` arrives as a dependency) and let the
  smoke test decide. If a `--convert-to` filter fails (some spreadsheet/ODF
  filters historically need Java), add `default-jre-headless` +
  `libreoffice-java-common` and re-test — do **not** add Java preemptively.
- **Image size / build time.** LibreOffice is large; the user accepted the
  "full stack" tradeoff. Note the resulting image-size delta in the rebuild
  step so it's a known quantity, not a surprise.
- **`soffice` first-run profile.** Headless `soffice` wants a writable
  `$HOME`/profile dir; in this image `$HOME=/root` is writable, so a plain
  `--convert-to` should work, but the smoke test must run as the container's
  normal user to confirm (use `-env:UserInstallation=file:///tmp/...` only if a
  profile-lock error appears).

## Steps

1. **Dockerfile — native toolchain (apt).** In `dockerfiles/claude-agent.Dockerfile`,
   add a dedicated `RUN apt-get` layer (keep it separate from the
   `19-26` system-packages layer for cache clarity, and end with
   `rm -rf /var/lib/apt/lists/*` as the other layers do) installing: LibreOffice
   headless components (Writer/Calc/Impress), the font set from Unknowns,
   `pandoc`, and `poppler-utils` (for `pdfinfo`/`pdftotext`). Use
   `--no-install-recommends` consistent with the existing layer.

2. **Dockerfile — baked Python venv.** Mirror the searxng-mcp pattern
   (`64-65`): `uv venv /opt/doc-tools/venv` then
   `uv pip install --python /opt/doc-tools/venv/bin/python python-docx openpyxl
   python-pptx`. Then expose one stable entrypoint by symlinking
   `/opt/doc-tools/venv/bin/python` to `/usr/local/bin/docpython` (name is the
   implementer's call; `docpython` is the documented suggestion). Do not touch
   system `python3` (PEP 668).

3. **Document it in `templates/global-claude.md`.** Add a concise
   claude-agent-scoped section (alongside the other "(claude-agent)" sections)
   covering: headless conversion/rendering
   (`soffice --headless --convert-to pdf --outdir <dir> <file>`), page-count
   verification (`pdfinfo`), the `docpython` interpreter and which libraries it
   carries, and `pandoc` for Markdown↔docx. Include the one hard-won tip:
   to preserve styling, edit by templating off the existing file rather than
   rebuilding formatting from scratch. Add a bullet to the existing
   "## Differences in claude-dev (start-claude.sh)" section (line 64) noting
   this office toolchain is **not** present in claude-dev.

4. **Tests.** Add substring assertions to `tests/test_dockerfile.py` in the
   existing style: LibreOffice install present, `pandoc` and `poppler-utils`
   present, the three Python libs installed into the doc-tools venv, and the
   `docpython` symlink created. Match the existing helpful-failure-message
   format.

5. **Rebuild + smoke test.** Run `start-agent.sh --rebuild`. Inside the
   resulting container, verify end to end: create a trivial `.docx` with
   `docpython` (python-docx), `soffice --headless --convert-to pdf` it, and
   confirm `pdfinfo` reports the expected page count; spot-check `pandoc
   --version`, `openpyxl`, and `python-pptx` import. Record the image-size delta.

## Notes

- **Existing sandboxes won't auto-update the doc.** `templates/global-claude.md`
  is seeded on first run; already-initialized sandboxes keep their copy. The
  installed *tools* arrive for everyone on the next `--rebuild`, but the CLAUDE.md
  guidance reaches existing sandboxes only via the repo's reseed path (or a
  manual copy). Not blocking; mention to the user if they have long-lived
  sandboxes.
- **Commit style:** this repo omits `Co-Authored-By` lines (`CLAUDE.md`
  "Commit style").
- **Out of scope:** `start-claude.sh` / claude-dev (per the user's "claude-agent
  only" decision) and any allowlist change (build-time apt needs none; runtime
  PyPI is already allowed).
