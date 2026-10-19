"""Static analysis: claude-agent.Dockerfile must install required CLIs correctly."""
import re
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


# ── offline-agent.Dockerfile ──────────────────────────────────────────────────

OFFLINE_DOCKERFILE = Path(__file__).parent.parent / "dockerfiles" / "offline-agent.Dockerfile"
USER_PACKAGES_MARKER = "# ── user packages ──"


def _offline_text() -> str:
    return OFFLINE_DOCKERFILE.read_text()


def _offline_instructions() -> list:
    """Top-level Dockerfile instruction keywords, in order (comments skipped)."""
    out = []
    continued = False
    for line in _offline_text().splitlines():
        stripped = line.strip()
        if not continued and stripped and not stripped.startswith("#"):
            out.append(stripped.split()[0].upper())
        if not stripped.startswith("#"):
            continued = stripped.endswith("\\")
    return out


def test_offline_opencode_and_pi_installed():
    text = _offline_text()
    assert "npm install -g" in text
    assert "opencode-ai@latest" in text
    assert "@earendil-works/pi-coding-agent@latest" in text


def test_offline_no_claude_code_installer():
    # Claude Code needs the Anthropic API; the offline image has no egress.
    assert "claude.ai/install.sh" not in _offline_text()


def test_offline_drops_claude_agent_extras():
    # Comments may name what was dropped; only instructions count.
    text = "\n".join(
        l for l in _offline_text().lower().splitlines() if not l.lstrip().startswith("#")
    )
    for unwanted in ("libreoffice", "searxng-mcp", "doc-tools", "pandoc"):
        assert unwanted not in text, f"{unwanted} belongs to claude-agent, not offline-agent"


def test_offline_opencode_phone_home_disabled():
    # Phase 1: autoupdate=false alone still let opencode reach registry.npmjs.org
    # and models.opencode.ai; these four env vars removed both.
    text = _offline_text()
    for var in (
        "OPENCODE_DISABLE_AUTOUPDATE=1",
        "OPENCODE_DISABLE_MODELS_FETCH=1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD=1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS=1",
    ):
        assert var in text, f"{var} missing from offline-agent.Dockerfile ENV"


def test_offline_git_safe_directory():
    # virtiofs files show as 0:0 inside the container; git refuses them otherwise.
    assert "safe.directory" in _offline_text() and "--system" in _offline_text()


def test_offline_uv_project_environment_redirect():
    assert 'UV_PROJECT_ENVIRONMENT="${TMPDIR:-/tmp}/.venv"' in _offline_text()


def test_offline_user_packages_block_is_last_run():
    text = _offline_text()
    assert USER_PACKAGES_MARKER in text, "user-packages block marker missing"
    tail = text[text.index(USER_PACKAGES_MARKER):]
    tail_runs = [l for l in tail.splitlines() if l.strip().startswith("RUN")]
    assert len(tail_runs) == 1, "exactly one RUN must follow the user-packages marker"
    # Nothing but WORKDIR/CMD may follow the user-packages RUN.
    instructions = _offline_instructions()
    last_run = len(instructions) - 1 - instructions[::-1].index("RUN")
    assert set(instructions[last_run + 1:]) <= {"WORKDIR", "CMD"}


def test_offline_rsync_installed():
    # offline-agent.py's non-git --diff/--export/--apply run rsync in a helper
    # container built from this image.
    apt = next(l for l in _offline_text().split("RUN ") if "apt-get install" in l)
    assert re.search(r"\brsync\b", apt)
