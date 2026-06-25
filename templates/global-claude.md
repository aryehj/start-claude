# Global Container CLAUDE.md

This file is loaded into every coding-agent session (Claude Code via
`~/.claude/CLAUDE.md`; OpenCode via `opencode.json`'s `instructions`) running
inside a dev container. It describes the shared container environment.
Project-level `CLAUDE.md` / `AGENTS.md` overrides anything here. You are running inside one of two
sibling environments: `claude-agent` (Colima + docker, via `start-agent.sh`)
or `claude-dev` (Apple Containers microVM, via `start-claude.sh`). Assume
`claude-agent` unless the start-claude.sh exceptions at the end of this file
apply.

## Filesystem

`$HOME=/root`. The working directory (e.g. `/Users/<name>/Repos/...`) is a
bind mount from the macOS host — it is not a Mac filesystem. There is no
`/Users/<name>/.claude/`; all Claude Code config lives at `/root/.claude/`.
`/root/.claude/` is itself a bind mount from `~/.claude-containers/shared/`
on the host, shared across all containers.

**File-reading / file-editing tools do not expand `~`.** Shells expand it;
tool arguments are passed literally. Use `/root/...` (not `~/...`) when
calling `read`, `edit`, `write`, or similar. `~` still works inside `bash`
tool commands because a shell interprets them.

## Python & uv

`uv` is at `/usr/local/bin/uv`. Use `uv run` and `uv pip`.
`UV_PROJECT_ENVIRONMENT` is set to `$TMPDIR/.venv`, so any `.venv/` in the
project root is **ignored** — it almost certainly contains macOS binaries
that won't run on Linux. Run `uv sync` once per fresh session to build a
usable venv; this is expected and normal.

**No bare `python` on PATH, and `python3-venv` / `ensurepip` / `pip` are
not installed.** For Python, reach for `uv` rather than `apt-get install`:
it's faster, needs no root, and sidesteps the macOS-`.venv`-leak problem
above. (`apt-get` does work on `claude-agent` — it flows through the egress
proxy and is gated by the same hostname allowlist as everything else — so
use it for genuinely system-level packages; just don't use it to bootstrap
Python.) For scratch Python work:

```bash
cd /tmp && uv init myproj && cd myproj && uv add <pkg> && uv run python -c '...'
```

## Network Egress (claude-agent)

All egress flows through an in-VM HTTP proxy with a hostname allowlist at
`~/.claude-agent/allowlist.txt` on the host. `HTTPS_PROXY` and `HTTP_PROXY`
are pre-set; Node honors them via `NODE_USE_ENV_PROXY=1`.

**`github.com` is not on the default allowlist.** The proxy filters by
hostname only (not method), so it cannot be made read-only. For code reads,
rewrite the URL — do not retry `github.com` on failure:

- `github.com/OWNER/REPO/blob/BRANCH/PATH` → `raw.githubusercontent.com/OWNER/REPO/BRANCH/PATH`
- `github.com/OWNER/REPO` (README) → `raw.githubusercontent.com/OWNER/REPO/HEAD/README.md`
- Repo tarballs → `codeload.github.com/OWNER/REPO/tar.gz/BRANCH`

A `403` or connection-refused on any other hostname means the allowlist is
rejecting it. Do not retry blindly — tell the user to add the hostname to
`~/.claude-agent/allowlist.txt` and run `start-agent.sh --reload-allowlist`.

## Local Inference (claude-agent)

A local model server runs on the macOS host at `$OLLAMA_HOST` (Ollama, port 11434) or `$OMLX_HOST` (omlx, port 8000). OpenCode is pre-wired to it — no configuration needed. Route local-model calls to `$OLLAMA_HOST` / `$OMLX_HOST`, not through the proxy.

## Office Document Toolchain (claude-agent)

The image ships a full headless office stack for format conversion, page-count
verification, and document editing.

**Convert to PDF (or between office formats):**
```bash
soffice --headless --convert-to pdf --outdir /tmp/out /path/to/file.docx
```
Also works with `.xlsx`, `.pptx`, `.odt`, etc. `--outdir` must exist.

**Verify page count / layout:**
```bash
pdfinfo /tmp/out/file.pdf   # shows "Pages:" among other metadata
```

**Edit `.docx`/`.xlsx`/`.pptx` in Python** — use the `docpython` interpreter
(pre-installed venv at `/opt/doc-tools/venv`):
```bash
docpython -c "from docx import Document; d = Document('file.docx'); print(len(d.paragraphs))"
```
Libraries available: `python-docx` (`.docx`), `openpyxl` (`.xlsx`),
`python-pptx` (`.pptx`). **Tip:** preserve styling by templating off the
existing file (`Document('existing.docx')`) rather than building formatting
from scratch — rebuilding formatting from scratch causes style drift.

**Markdown ↔ docx conversion:**
```bash
pandoc input.md -o output.docx
pandoc input.docx -t markdown -o output.md
```

## Differences in claude-dev (start-claude.sh)

- **Sandboxed bash egress is restricted to a default-deny allowlist.**
  `start-claude.sh` seeds `sandbox.network.allowedDomains` in the project's
  `.claude/settings.local.json`. Only hosts on that list (and their
  subdomains) are reachable from sandboxed bash commands (`curl`, `git`,
  `uv`, etc.); all others are blocked at the kernel level (network namespace
  + proxy). The list covers Anthropic, common package registries, reference
  sites, and major academic/government sources — see
  `templates/sandbox-allowlist.txt` in the `start-claude` repo.
  **`github.com` is omitted by default** (write-capable). HTTPS `git push`
  is blocked under the default config; to re-enable it for a project, add
  `github.com` and `*.github.com` to `sandbox.network.allowedDomains` in
  `.claude/settings.local.json`. To add any other host, do the same. To
  reset to the current template list, run
  `start-claude.sh --reseed-sandbox-allowlist`.
  Note: the microVM itself has full unrestricted egress at the VM level —
  this is a guardrail against accidental off-list fetches, not a hard
  security boundary (the settings file is writable by the agent).
- **No office document toolchain** — LibreOffice, pandoc, poppler-utils, and
  the `docpython` venv are **not** present. Use `uv run` to install
  `python-docx` / `openpyxl` / `python-pptx` at runtime for editing; format
  conversion and page-count verification via `soffice`/`pdfinfo` are
  unavailable without the claude-agent image.
- **No local inference server** — `$OLLAMA_HOST` and `$OMLX_HOST` are unset.
- **Bubblewrap sandbox is active** for bash commands. `/tmp` and
  `/root/.cache` are **read-only** at the sandbox mount layer. Use `$TMPDIR`
  for all scratch work — `uv` is already configured to use it. A
  `read-only file system` error on `/tmp` means you are inside the sandbox;
  retarget the operation to `$TMPDIR` and do not escalate.
