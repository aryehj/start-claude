"""Static analysis: apt package lists in Dockerfile and start-claude.sh must match."""
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
DOCKERFILE = REPO / "dockerfiles" / "claude-agent.Dockerfile"
START_CLAUDE = REPO / "start-claude.sh"

_PKG_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-\.~]*$")


def _parse_apt_packages(text: str) -> set[str]:
    """Extract packages from the apt-get install --no-install-recommends block."""
    lines = text.splitlines()
    in_block = False
    packages: set[str] = set()

    for line in lines:
        stripped = line.strip()

        if not in_block:
            if "apt-get install -y --no-install-recommends" in stripped:
                in_block = True
                payload = re.sub(r".*--no-install-recommends", "", stripped).rstrip("\\").strip()
                packages.update(t for t in payload.split() if _PKG_NAME.match(t))
            continue

        tokens = stripped.rstrip("\\").split()
        if stripped.startswith("&&") or not all(_PKG_NAME.match(t) for t in tokens):
            break
        packages.update(tokens)
        if not stripped.endswith("\\"):
            break

    return packages


# claude-agent.Dockerfile intentionally omits sandbox deps (ADR-033): bubblewrap
# inside an unprivileged Docker container needs CAP_SYS_ADMIN, which weakens the
# Colima VM boundary. The VM-level firewall is the real isolation layer.
# start-claude.sh (Apple Containers microVM) does use these.
_SANDBOX_ONLY_PACKAGES = {"bubblewrap", "libseccomp2", "socat"}


def test_dockerfile_and_start_claude_package_lists_match():
    dockerfile_packages = _parse_apt_packages(DOCKERFILE.read_text())
    start_claude_packages = _parse_apt_packages(START_CLAUDE.read_text())

    assert dockerfile_packages, "No packages found in Dockerfile apt-get install block"
    assert start_claude_packages, "No packages found in start-claude.sh apt-get install block"

    only_in_dockerfile = dockerfile_packages - start_claude_packages
    # Exclude known intentional divergence: sandbox deps only in start-claude.sh (ADR-033)
    only_in_start_claude = (start_claude_packages - dockerfile_packages) - _SANDBOX_ONLY_PACKAGES

    assert not only_in_dockerfile and not only_in_start_claude, (
        f"Package lists diverged (excluding known sandbox-only packages {sorted(_SANDBOX_ONLY_PACKAGES)}).\n"
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
