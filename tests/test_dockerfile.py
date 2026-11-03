"""Static analysis: claude-agent.Dockerfile must install required CLIs correctly."""
from pathlib import Path

DOCKERFILE = Path(__file__).parent.parent / "dockerfiles" / "claude-agent.Dockerfile"
_TEXT = DOCKERFILE.read_text()


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
# start-agent.sh --refresh-tools passes a fresh TOOLS_CACHE_BUST so the npm
# layer (and every later one) reruns while apt/Node/Claude Code/uv stay cached.

def _npm_install_line_index(lines):
    idx = [i for i, l in enumerate(lines)
           if "npm install -g" in l and "opencode-ai@latest" in l]
    assert len(idx) == 1, f"expected one opencode/pi npm install line, found {len(idx)}"
    return idx[0]


def test_tools_cache_bust_arg_declared_before_npm_install():
    lines = _TEXT.splitlines()
    npm = _npm_install_line_index(lines)
    args = [i for i, l in enumerate(lines) if l.strip().startswith("ARG TOOLS_CACHE_BUST")]
    assert len(args) == 1, "Dockerfile must declare exactly one ARG TOOLS_CACHE_BUST"
    assert args[0] < npm, "ARG TOOLS_CACHE_BUST must come before the npm install line"
    run = npm
    while not lines[run].lstrip().startswith("RUN"):
        run -= 1
    between = [l for l in lines[args[0] + 1:run] if l.strip().startswith(("RUN", "COPY", "ADD"))]
    assert not between, (
        "no layer may sit between ARG TOOLS_CACHE_BUST and the npm install, "
        f"or a refresh would rerun it too: {between}"
    )


def test_tools_cache_bust_referenced_by_npm_run():
    lines = _TEXT.splitlines()
    npm = _npm_install_line_index(lines)
    run = npm
    while not lines[run].lstrip().startswith("RUN"):
        run -= 1
    block = "\n".join(lines[run:npm + 1])
    assert "TOOLS_CACHE_BUST" in block.split("ARG")[-1], (
        "the npm install RUN must reference $TOOLS_CACHE_BUST so a new value reruns it"
    )


def test_tools_still_installed_at_latest():
    # The user chose to keep @latest (plans/safe-tool-version-updates.md).
    assert "opencode-ai@latest" in _TEXT and "@earendil-works/pi-coding-agent@latest" in _TEXT
