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
