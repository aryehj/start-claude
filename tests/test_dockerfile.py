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


def test_file_command_installed():
    assert "libreoffice-writer" in _TEXT, "expected office-toolchain apt layer to exist"
    office_layer_start = _TEXT.index("libreoffice-writer")
    office_layer_end = _TEXT.index("rm -rf /var/lib/apt/lists/*", office_layer_start)
    assert "file" in {
        pkg.strip() for pkg in _TEXT[office_layer_start:office_layer_end].split()
    }, (
        "file not installed in Dockerfile; expected `file` in the office-toolchain "
        "apt layer (provides libmagic-backed file-type detection)"
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


def test_docpython_is_wrapper_not_bare_symlink():
    assert "ln -sf /opt/doc-tools/venv/bin/python /usr/local/bin/docpython" not in _TEXT, (
        "docpython must not be a bare symlink to the venv's bin/python; that "
        "second-order symlink loses the venv's pyvenv.cfg discovery and drops "
        "site-packages off sys.path (import docx/openpyxl/pptx fails)"
    )
    assert "/usr/local/bin/docpython" in _TEXT and "exec env PYTHONHASHSEED=0 /opt/doc-tools/venv/bin/python" in _TEXT, (
        "docpython must be a wrapper at /usr/local/bin/docpython that execs "
        "/opt/doc-tools/venv/bin/python with PYTHONHASHSEED pinned (skips the "
        "blocking getrandom() startup read on a low-entropy VM)"
    )


def test_pythonhashseed_pinned_in_env():
    assert "PYTHONHASHSEED=0" in _TEXT, (
        "PYTHONHASHSEED=0 must be set (ENV + docpython wrapper) so CPython skips "
        "the unconditional getrandom(2) read at startup, which blocks and hangs "
        "every python invocation when the VM kernel CRNG is unseeded"
    )


def test_uv_uses_system_python_no_managed_download():
    assert "UV_PYTHON_DOWNLOADS=never" in _TEXT and "UV_PYTHON_PREFERENCE=system" in _TEXT, (
        "UV_PYTHON_DOWNLOADS=never and UV_PYTHON_PREFERENCE=system must be set so "
        "ad-hoc `uv init/run` uses the baked system python3 instead of fetching a "
        "managed CPython via github.com, which is excluded from the allowlist and stalls"
    )
