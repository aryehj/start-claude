"""Static analysis: apt package lists in Dockerfile and start-claude.sh must match."""
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
DOCKERFILE = REPO / "dockerfiles" / "claude-agent.Dockerfile"
START_CLAUDE = REPO / "start-claude.sh"

_PKG_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-\.~]*$")


def _parse_apt_packages(text: str) -> set[str]:
    """Extract packages from every apt-get install --no-install-recommends block.

    Both files carry more than one such block, so the end of a block only clears
    block state — the scan continues looking for the next one.
    """
    in_block = False
    packages: set[str] = set()

    for line in text.splitlines():
        stripped = line.strip()

        if in_block:
            tokens = stripped.rstrip("\\").split()
            if stripped.startswith("&&") or not all(_PKG_NAME.match(t) for t in tokens):
                # Not a package line: the block ended here. Fall through so this
                # same line can open the next block if it happens to be one.
                in_block = False
            else:
                packages.update(tokens)
                # No line continuation means this was the block's last line.
                in_block = stripped.endswith("\\")
                continue

        if "apt-get install -y --no-install-recommends" in stripped:
            in_block = True
            payload = re.sub(r".*--no-install-recommends", "", stripped).rstrip("\\").strip()
            packages.update(t for t in payload.split() if _PKG_NAME.match(t))

    return packages


# claude-agent.Dockerfile intentionally omits sandbox deps (ADR-033): bubblewrap
# inside an unprivileged Docker container needs CAP_SYS_ADMIN, which weakens the
# Colima VM boundary. The VM-level firewall is the real isolation layer.
# start-claude.sh (Apple Containers microVM) does use these.
_SANDBOX_ONLY_PACKAGES = {"bubblewrap", "libseccomp2", "socat"}

# start-claude.sh intentionally omits the office document toolchain. Its absence
# from claude-dev is a documented property of that environment — see the
# "Differences in claude-dev" section of templates/global-claude.md, which tells
# agents LibreOffice, pandoc, poppler-utils and the docpython venv are not there.
# claude-agent.Dockerfile carries the stack in its own apt block.
_DOC_TOOLS_ONLY_PACKAGES = {
    "libreoffice-writer",
    "libreoffice-calc",
    "libreoffice-impress",
    "fonts-liberation",
    "fonts-crosextra-carlito",
    "fonts-crosextra-caladea",
    "fonts-dejavu",
    "fonts-roboto",
    "pandoc",
    "poppler-utils",
}


def test_dockerfile_and_start_claude_package_lists_match():
    dockerfile_packages = _parse_apt_packages(DOCKERFILE.read_text())
    start_claude_packages = _parse_apt_packages(START_CLAUDE.read_text())

    assert dockerfile_packages, "No packages found in Dockerfile apt-get install block"
    assert start_claude_packages, "No packages found in start-claude.sh apt-get install block"

    # Exclude the two known intentional divergences: the office toolchain lives
    # only in the Dockerfile, the sandbox deps only in start-claude.sh (ADR-033).
    only_in_dockerfile = (dockerfile_packages - start_claude_packages) - _DOC_TOOLS_ONLY_PACKAGES
    only_in_start_claude = (start_claude_packages - dockerfile_packages) - _SANDBOX_ONLY_PACKAGES

    assert not only_in_dockerfile and not only_in_start_claude, (
        "Package lists diverged (excluding known doc-tools-only packages "
        f"{sorted(_DOC_TOOLS_ONLY_PACKAGES)} and sandbox-only packages "
        f"{sorted(_SANDBOX_ONLY_PACKAGES)}).\n"
        f"  Only in Dockerfile:     {sorted(only_in_dockerfile)}\n"
        f"  Only in start-claude.sh: {sorted(only_in_start_claude)}"
    )


def test_sandbox_only_packages_present_in_start_claude():
    """Guard: if sandbox deps are ever removed from start-claude.sh, update _SANDBOX_ONLY_PACKAGES."""
    start_claude_packages = _parse_apt_packages(START_CLAUDE.read_text())
    missing = _SANDBOX_ONLY_PACKAGES - start_claude_packages
    assert not missing, (
        f"Sandbox-only package(s) {sorted(missing)} no longer in start-claude.sh. "
        "Update _SANDBOX_ONLY_PACKAGES in this file."
    )


def test_doc_tools_only_packages_present_in_dockerfile():
    """Guard: if the office stack is ever slimmed down, update _DOC_TOOLS_ONLY_PACKAGES."""
    dockerfile_packages = _parse_apt_packages(DOCKERFILE.read_text())
    missing = _DOC_TOOLS_ONLY_PACKAGES - dockerfile_packages
    assert not missing, (
        f"Doc-tools-only package(s) {sorted(missing)} no longer in claude-agent.Dockerfile. "
        "Update _DOC_TOOLS_ONLY_PACKAGES in this file."
    )


# ── parser unit tests ────────────────────────────────────────────────────────
# Both real files carry more than one apt block, so the parser has to resume
# scanning after a block ends rather than stopping at the first one.


def test_parser_collects_every_dockerfile_style_block():
    """Two RUN blocks, each terminated by a line beginning `&&`."""
    text = """\
RUN apt-get update -qq \\
 && apt-get install -y --no-install-recommends \\
      bash curl \\
 && apt-get upgrade -y \\
 && rm -rf /var/lib/apt/lists/*

RUN apt-get update -qq \\
 && apt-get install -y --no-install-recommends \\
      pandoc \\
      poppler-utils \\
 && rm -rf /var/lib/apt/lists/*
"""
    assert _parse_apt_packages(text) == {"bash", "curl", "pandoc", "poppler-utils"}


def test_parser_collects_every_shell_style_block():
    """Two blocks, each terminated by a package line with no trailing backslash."""
    text = """\
    apt-get install -y --no-install-recommends \\
      bash curl \\
      jq
    apt-get upgrade -y

    apt-get install -y --no-install-recommends \\
      unzip zip
"""
    assert _parse_apt_packages(text) == {"bash", "curl", "jq", "unzip", "zip"}


def test_parser_treats_terminator_line_as_eligible_block_start():
    """A line that closes one block can open the next one."""
    text = """\
RUN apt-get install -y --no-install-recommends \\
      bash \\
 && apt-get install -y --no-install-recommends \\
      pandoc
"""
    assert _parse_apt_packages(text) == {"bash", "pandoc"}


def test_parser_ignores_installs_without_no_install_recommends():
    """`apt-get install -y nodejs` is deliberately out of scope."""
    text = """\
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \\
 && apt-get install -y nodejs
"""
    assert _parse_apt_packages(text) == set()
