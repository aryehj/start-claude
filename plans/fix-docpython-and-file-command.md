# Fix `docpython` and install `file` in claude-agent image

## Status

- [ ] Replace the bare `docpython` symlink with a wrapper that execs the venv Python (`dockerfiles/claude-agent.Dockerfile:94`)
- [ ] Add `file` to the office-toolchain apt layer (`dockerfiles/claude-agent.Dockerfile:63-70`)
- [ ] Strengthen `test_docpython_symlink_created` to assert the wrapper form, not a bare symlink (`tests/test_dockerfile.py:68`)
- [ ] Add a `file` install assertion to `tests/test_dockerfile.py`
- [ ] `start-agent.sh --rebuild`, then verify `docpython -c "import docx, openpyxl, pptx"` and `file --version` inside the container

## Context

Two of the four failures logged in `plans/SESSION-TECHNICAL-FAILURES.md` are
real, fixable bugs in the `claude-agent` image (built from
`dockerfiles/claude-agent.Dockerfile`; `start-agent.sh` path only —
`start-claude.sh` ships no doc toolchain). Failures #2 (benign Java warning)
and #3 (LibreOffice can't load a pandoc `.docx`) are dropped per the scoping
decision.

**#1 — `docpython` is broken (high).** Line 94 creates the entrypoint as a bare
symlink: `ln -sf /opt/doc-tools/venv/bin/python /usr/local/bin/docpython`. The
venv's own `bin/python` is itself a symlink into uv's standalone interpreter, so
invoking Python through the *second* symlink at `/usr/local/bin/docpython`
resolves `sys.executable` past the venv and loses the `pyvenv.cfg` landmark —
the venv's `site-packages` (where `docx`/`openpyxl`/`pptx` live) drops off
`sys.path`. The session confirmed this empirically: `docpython -c "import docx"`
raises `ModuleNotFoundError`, while `/opt/doc-tools/venv/bin/python -c "import
docx"` succeeds. The sibling searxng-mcp venv (line 83-84) avoids the bug only
because `start-agent.sh` invokes its `bin/python` directly, never through a
relocated symlink.

The documented usage in `templates/global-claude.md:83-91` is correct as
written; only the interpreter entrypoint needs to actually resolve the venv.

**#4 — `file` command missing (trivial).** `file`/libmagic isn't installed;
`file <x>` exits 127. Useful for document inspection. It belongs in the existing
office-toolchain apt layer.

## Goals

- `docpython` resolves the `/opt/doc-tools/venv` site-packages so `import docx`,
  `import openpyxl`, and `import pptx` all succeed.
- `file` is available on `PATH` in the `claude-agent` container.
- `tests/test_dockerfile.py` guards both the working-entrypoint form and the
  `file` install, so a regression to the bare symlink fails CI.

## Approach

The fix for #1 is to stop routing through a second-order symlink. Make
`docpython` a tiny wrapper (`exec /opt/doc-tools/venv/bin/python "$@"`) installed
at `/usr/local/bin/docpython` and marked executable. Invoking the venv's
`bin/python` by its real in-venv path is exactly what the session verified works,
and what preserves `pyvenv.cfg` discovery. A wrapper is preferred over any
symlink so `sys.executable` stays inside the venv directory.

## Notes

- `test_docpython_symlink_created` currently only asserts the substring
  `"docpython"` is present, which the wrapper still satisfies — but the test name
  and message describe the exact broken construct. Rename/retarget it to assert
  the wrapper shape (e.g. the line is not a bare `ln -sf ... docpython` and does
  `exec` the venv python) so the guard has teeth. Keep it a static text check on
  the Dockerfile, consistent with the rest of the file.
- Rebuild is required to exercise the change (`start-agent.sh --rebuild`); the
  static tests run without a rebuild. The container-side verification in the last
  Status item is the real proof for #1 and #4.
