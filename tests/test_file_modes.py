"""Entry-point scripts keep their executable bit.

Editing files in place on the macOS bind mount (e.g. `sed -i`) can drop the
executable bit, and git then records a 100755 -> 100644 mode change. This
checks the working tree, so the loss is caught before it is committed.
"""
import os
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent

EXECUTABLES = [
    "start-claude.sh",
    "start-agent.sh",
    "research.py",
    "scripts/bump-searxng.sh",
    "tests/verify-tool-refresh.sh",
    "tests/probe-deny-rules.py",
    "tests/probe-tinyproxy-owner-rule.sh",
    "tests/probe-vane-egress.sh",
    "tests/probe-vm-ipv6.sh",
    "tests/test-agent-firewall.sh",
    "tests/test-cross-vm-isolation.sh",
]


@pytest.mark.parametrize("name", EXECUTABLES)
def test_script_is_executable(name):
    path = REPO / name
    assert path.is_file(), "{} is missing".format(name)
    assert os.stat(path).st_mode & 0o111, (
        "{} lost its executable bit; restore it with chmod 755".format(name)
    )
