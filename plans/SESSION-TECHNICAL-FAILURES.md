# Session Technical Failures

Notes on tooling/environment problems hit while restyling
`Aryeh Jacobsohn - Resume - Elevance IT Strategy & Innovation Director.docx`
to match the CMS resume. Recorded for later fixing. Date: 2026-07-01.

---

## 1. `docpython` is broken — no `python-docx`

**Severity:** high (the documented tool for editing Office docs is unusable)

The global CLAUDE.md instructs using `docpython` to edit `.docx`/`.xlsx`/`.pptx`
files. It fails:

```
$ docpython -c "from docx import Document; ..."
ModuleNotFoundError: No module named 'docx'
```

`docpython` (`/usr/local/bin/docpython`) is a **standalone compiled binary**
(its own bundled interpreter), not a shim into the doc-tools venv. It does not
have `python-docx` (or `openpyxl`/`python-pptx`) importable.

**The actual venv works fine:**

```
$ /opt/doc-tools/venv/bin/python -c "import docx; print(docx.__version__)"
1.2.0
```

**Fix options:**
- Repoint `docpython` at `/opt/doc-tools/venv/bin/python`, or
- Install `python-docx`/`openpyxl`/`python-pptx` into whatever interpreter
  `docpython` actually is.

**Workaround used this session:** did not rely on `docpython` at all — used
`/opt/doc-tools/venv/bin/python` where needed and manipulated the OOXML
(`document.xml`) directly with the stdlib `xml` module.

---

## 2. Repetitive Java warning on every `soffice` invocation

**Severity:** low (noise; conversions still succeed)

Every headless LibreOffice call emits:

```
Warning: failed to launch javaldx - java may not function correctly
```

No JRE is on PATH / configured for LibreOffice. It is only a warning —
`--convert-to pdf` still produces correct output — but it prints on every
single call and clutters logs. Anything that genuinely needs Java in
LibreOffice (some filters, Base, certain macros) would break.

**Fix:** install a JRE and register it (`soffice ... -env:...`, or configure
the LibreOffice Java settings), or accept/suppress the warning if Java-backed
features are never needed.

---

## 3. `soffice` cannot load the original (pandoc-generated) Elevance `.docx`

**Severity:** high for this task (blocked PDF rendering / visual verification
of the source file)

The **original** Elevance file would not open in LibreOffice:

```
Error: source file could not be loaded
```

Details:
- The zip/OOXML was **well-formed** — every part (`document.xml`, `styles.xml`,
  `numbering.xml`, `[Content_Types].xml`, all `.rels`) passed XML parsing, and
  all relationship targets/content-type overrides were present and consistent.
- Failure was **not** caused by the `&` in the filename, a stale lock, or a
  running `soffice` process — it reproduced with a simplified filename, a fresh
  user profile (`-env:UserInstallation=...`), and after repackaging the
  extracted parts into a new zip.
- The file is a **pandoc-style** package (styles `FirstParagraph`/`BodyText`,
  `numId` 1001–1004, empty boilerplate `comments.xml` + separator-only
  `footnotes.xml`). LibreOffice rejects something in this package that Word
  presumably tolerates. Root cause not isolated (would require bisecting parts).

**Impact / workaround:** couldn't diff-render the original against the target.
The task was solved by rebuilding the content inside a copy of the CMS package
(which loads fine), so the produced file renders correctly. The *original*
remaining un-loadable in LibreOffice is unresolved but now moot — it was
overwritten by the corrected, loadable version.

---

## 4. `file` command not found

**Severity:** trivial

```
$ file <something>
/bin/bash: line 1: file: command not found   (exit 127)
```

The `file` utility is not installed. Not a blocker — used `unzip -l` and XML
parsing to inspect the document instead. Worth installing (`apt-get install
file` / `libmagic`) for convenience.

---

## Summary

| # | Problem | Severity | Status |
|---|---------|----------|--------|
| 1 | `docpython` binary has no `python-docx` | high | worked around (used venv python + raw XML) |
| 2 | `javaldx`/Java warning on every `soffice` call | low | benign; conversions succeeded |
| 3 | LibreOffice can't load the original pandoc `.docx` | high (task) | worked around (rebuilt in CMS package); root cause not isolated |
| 4 | `file` command missing | trivial | worked around |

The end deliverable (restyled resume) succeeded despite #1 and #3 by avoiding
`docpython` and rebuilding the document inside the known-good CMS package.
