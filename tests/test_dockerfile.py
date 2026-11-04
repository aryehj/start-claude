"""Static analysis: the claude-agent Dockerfiles must install required CLIs correctly.

claude-agent.Dockerfile builds claude-agent:base (everything but OpenCode/Pi);
claude-agent-tools.Dockerfile builds claude-agent:latest FROM that tag plus the
npm layer (ADR-054)."""
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
DOCKERFILE = REPO / "dockerfiles" / "claude-agent.Dockerfile"
TOOLS_DOCKERFILE = REPO / "dockerfiles" / "claude-agent-tools.Dockerfile"
_BASE = DOCKERFILE.read_text()
_TOOLS = TOOLS_DOCKERFILE.read_text()
# Both files: most checks only care that the package is installed somewhere.
_TEXT = _BASE + "\n" + _TOOLS


def test_pi_installed_via_npm():
    assert "@earendil-works/pi-coding-agent@latest" in _TEXT, (
        "pi CLI not installed in Dockerfile; expected "
        "@earendil-works/pi-coding-agent@latest in an `npm install -g` line"
    )


def test_pi_deprecated_package_not_used():
    assert "@mariozechner/pi-coding-agent" not in _TEXT, (
        "Deprecated pi package @mariozechner/pi-coding-agent found in Dockerfile; "
        "use @earendil-works/pi-coding-agent instead"
    )


def test_opencode_installed_via_npm():
    assert "npm install -g" in _TEXT and "opencode-ai@latest" in _TEXT, (
        "opencode CLI not installed in Dockerfile"
    )


# ── doc-tools assertions ──────────────────────────────────────────────────────

def test_libreoffice_headless_installed():
    assert "libreoffice-writer" in _TEXT and "libreoffice-calc" in _TEXT and "libreoffice-impress" in _TEXT, (
        "LibreOffice headless components not installed in Dockerfile; expected "
        "libreoffice-writer, libreoffice-calc, libreoffice-impress in an apt-get layer"
    )


def test_pandoc_installed():
    assert "pandoc" in _TEXT, (
        "pandoc not installed in Dockerfile; expected pandoc in an apt-get layer"
    )


def test_poppler_utils_installed():
    assert "poppler-utils" in _TEXT, (
        "poppler-utils not installed in Dockerfile; expected poppler-utils in an apt-get layer "
        "(provides pdfinfo, pdftotext)"
    )


def test_doc_tools_venv_python_docx():
    assert "python-docx" in _TEXT, (
        "python-docx not installed in Dockerfile; expected python-docx in the /opt/doc-tools venv"
    )


def test_doc_tools_venv_openpyxl():
    assert "openpyxl" in _TEXT, (
        "openpyxl not installed in Dockerfile; expected openpyxl in the /opt/doc-tools venv"
    )


def test_doc_tools_venv_python_pptx():
    assert "python-pptx" in _TEXT, (
        "python-pptx not installed in Dockerfile; expected python-pptx in the /opt/doc-tools venv"
    )


def test_docpython_symlink_created():
    assert "docpython" in _TEXT, (
        "docpython symlink not created in Dockerfile; expected a symlink from "
        "/opt/doc-tools/venv/bin/python to /usr/local/bin/docpython"
    )


# ── searxng MCP shim assertions ───────────────────────────────────────────────

def test_searxng_mcp_pinned_below_v2():
    # server.py imports mcp.server.fastmcp, which mcp 2.x removed.
    assert "'mcp[cli]<2'" in _TEXT, (
        "searxng MCP shim must pin 'mcp[cli]<2'; mcp 2.x renamed FastMCP and "
        "the shim fails on import"
    )


# ── tools cache bust (--refresh-tools) ────────────────────────────────────────
# start-agent.sh --refresh-tools rebuilds only the tools image, with a fresh
# TOOLS_CACHE_BUST, on top of the tagged base; the npm layer must be its only
# layer, and the base must not install the tools.

def _instructions(text):
    """(index, line) of Dockerfile instruction lines, comments and continuations dropped."""
    out, cont = [], False
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        if not cont and s and not s.startswith("#"):
            out.append((i, s))
        cont = s.endswith("\\")
    return out


def _npm_install_line_index(lines):
    idx = [i for i, l in enumerate(lines)
           if "npm install -g" in l and "opencode-ai@latest" in l]
    assert len(idx) == 1, f"expected one opencode/pi npm install line, found {len(idx)}"
    return idx[0]


def test_tools_image_builds_from_the_tagged_base():
    froms = [s for _, s in _instructions(_TOOLS) if s.upper().startswith("FROM ")]
    assert froms == ["FROM claude-agent:base"], (
        "claude-agent-tools.Dockerfile must have one FROM, of the tagged base image "
        f"(not a stage, which the legacy builder would rebuild): {froms}"
    )


def test_base_tag_matches_start_agent():
    script = (REPO / "start-agent.sh").read_text()
    assert 'BASE_IMAGE_TAG="claude-agent:base"' in script
    assert 'TOOLS_DOCKERFILE_PATH="$SCRIPT_DIR/dockerfiles/claude-agent-tools.Dockerfile"' in script


def test_base_does_not_install_the_tools():
    for pkg in ("opencode-ai", "pi-coding-agent", "TOOLS_CACHE_BUST"):
        assert pkg not in _BASE, f"{pkg} belongs in claude-agent-tools.Dockerfile, not the base"


def test_base_has_one_stage():
    froms = [s for _, s in _instructions(_BASE) if s.upper().startswith("FROM ")]
    assert len(froms) == 1, f"the base Dockerfile must be a single stage: {froms}"


def test_tools_image_has_only_the_npm_layer():
    layers = [s for _, s in _instructions(_TOOLS) if s.startswith(("RUN", "COPY", "ADD"))]
    assert len(layers) == 1 and "npm install -g" in _TOOLS, (
        f"the tools image must add one layer, the npm install; found {layers}"
    )
    workdir = [s for _, s in _instructions(_TOOLS) if s.upper().startswith(("WORKDIR", "VOLUME"))]
    assert not workdir, f"WORKDIR/VOLUME belong in the base (the legacy builder commits them): {workdir}"


def test_tools_cache_bust_arg_declared_before_npm_install():
    lines = _TOOLS.splitlines()
    npm = _npm_install_line_index(lines)
    args = [i for i, l in enumerate(lines) if l.strip().startswith("ARG TOOLS_CACHE_BUST")]
    assert len(args) == 1, "the tools Dockerfile must declare exactly one ARG TOOLS_CACHE_BUST"
    assert args[0] < npm, "ARG TOOLS_CACHE_BUST must come before the npm install line"
    run = npm
    while not lines[run].lstrip().startswith("RUN"):
        run -= 1
    between = [l for l in lines[args[0] + 1:run] if l.strip().startswith(("RUN", "COPY", "ADD"))]
    assert not between, (
        "no layer may sit between ARG TOOLS_CACHE_BUST and the npm install, "
        f"or a refresh would rerun it too: {between}"
    )
    from_line = next(i for i, s in _instructions(_TOOLS) if s.upper().startswith("FROM "))
    assert from_line < args[0], "ARG TOOLS_CACHE_BUST must follow FROM to be in scope for the RUN"


def test_tools_cache_bust_referenced_by_npm_run():
    lines = _TOOLS.splitlines()
    npm = _npm_install_line_index(lines)
    run = npm
    while not lines[run].lstrip().startswith("RUN"):
        run -= 1
    block = "\n".join(lines[run:npm + 1])
    assert re.search(r"\$\{?TOOLS_CACHE_BUST", block), (
        "the npm install RUN must reference $TOOLS_CACHE_BUST so a new value reruns it"
    )


def test_tools_still_installed_at_latest():
    # The user chose to keep @latest (plans/safe-tool-version-updates.md).
    assert "opencode-ai@latest" in _TOOLS and "@earendil-works/pi-coding-agent@latest" in _TOOLS


def test_npm_install_is_the_last_layer():
    # Any layer below the npm install would rerun on each --refresh-tools.
    # Only metadata instructions may follow it.
    lines = _TOOLS.splitlines()
    npm = _npm_install_line_index(lines)
    after = [l for l in lines[npm + 1:] if l.strip().startswith(("RUN", "COPY", "ADD"))]
    assert not after, f"the opencode/pi npm install must be the last layer; found after it: {after}"
