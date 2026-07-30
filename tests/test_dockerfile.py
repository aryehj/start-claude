"""Static analysis: claude-agent.Dockerfile must install required CLIs correctly."""
import re
from pathlib import Path

DOCKERFILE = Path(__file__).parent.parent / "dockerfiles" / "claude-agent.Dockerfile"
_TEXT = DOCKERFILE.read_text()

SHIM = Path(__file__).parent.parent / "dockerfiles" / "searxng-mcp" / "server.py"
_SHIM_TEXT = SHIM.read_text()


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


# ── searxng-mcp shim assertions ───────────────────────────────────────────────
# An unpinned `mcp` resolved to SDK 2.0.0 on a rebuild, which moved FastMCP to
# mcp.server.mcpserver as MCPServer. The shim died at import and opencode showed
# the server as "offline" with no build failure and no error message. These two
# tests catch both halves of that: the missing pin, and pin/import drift.

def _searxng_mcp_requirement() -> str:
    """The `mcp` requirement token from the searxng-mcp `uv pip install` line."""
    match = re.search(
        r"uv pip install --python /opt/searxng-mcp/venv/bin/python\s+(.+)", _TEXT
    )
    assert match, "searxng-mcp `uv pip install` line not found in Dockerfile"
    tokens = [t.strip("'\"") for t in match.group(1).split()]
    mcp_token = next((t for t in tokens if re.match(r"^mcp\b|^mcp\[", t)), None)
    assert mcp_token, (
        f"no `mcp` requirement in the searxng-mcp install line: {match.group(1)}"
    )
    return mcp_token


def test_searxng_mcp_sdk_is_pinned():
    requirement = _searxng_mcp_requirement()
    assert re.search(r"[<>=~!]", requirement), (
        f"searxng-mcp installs `{requirement}` with no version constraint. An "
        "unpinned MCP SDK lets a major bump break the shim's import at rebuild "
        "time, which opencode surfaces only as an offline MCP server. Pin the "
        "major, e.g. 'mcp>=2,<3'."
    )


def test_searxng_shim_import_matches_pin():
    requirement = _searxng_mcp_requirement()
    pins_v2 = ">=2" in requirement and "<3" in requirement
    assert pins_v2, (
        f"searxng-mcp pins `{requirement}`, but this test only knows the 2.x "
        "import layout. Update the shim's import and this assertion together."
    )
    assert "mcp.server.fastmcp" not in _SHIM_TEXT, (
        "searxng-mcp/server.py imports mcp.server.fastmcp, which does not exist "
        "in MCP SDK 2.x (renamed to mcp.server.mcpserver.MCPServer). The shim "
        "will die at import and opencode will show searxng as offline."
    )
    assert "from mcp.server.mcpserver import MCPServer" in _SHIM_TEXT, (
        "searxng-mcp/server.py must import MCPServer from mcp.server.mcpserver "
        f"to match the pinned SDK (`{requirement}`)"
    )
