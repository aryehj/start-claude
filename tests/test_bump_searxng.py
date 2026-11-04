"""scripts/bump-searxng.sh's VM path, driven through a fake `colima` on PATH.

The fake `colima ssh` runs the piped command line with bash and a fake
`docker`, so the script's quoting of Go templates is exercised too.
tests/test_tool_refresh.py covers the offline --digest path and the rewrite.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
BUMP = REPO / "scripts" / "bump-searxng.sh"

OLD = "sha256:" + "0" * 64
NEW = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32

FIXTURE = (
    "#!/usr/bin/env bash\n"
    "# Pinned by digest (old comment).\n"
    'SEARXNG_IMAGE="docker.io/searxng/searxng@' + OLD + '"\n'
    "echo rest\n"
)

RUNNING = '{"name":"claude-agent","status":"Running","arch":"aarch64"}'

FAKE_COLIMA = r"""#!/usr/bin/env bash
case "$1" in
  list)
    printf '%s\n' "$FAKE_COLIMA_LIST"
    # Keep writing long after the match, as a large or chatty colima would.
    if [[ -n "${FAKE_COLIMA_PAD:-}" ]]; then
      for _ in $(seq 1 "$FAKE_COLIMA_PAD"); do printf '%01024d\n' 0; done
    fi
    ;;
  ssh) PATH="$FAKE_BIN:$PATH" bash ;;
  *) exit 2 ;;
esac
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
case "$1 $2" in
  "pull -q") exit 0 ;;
  "image inspect")
    [[ "$3" == "-f" && $# -eq 5 ]] || { echo "bad inspect argv: $*" >&2; exit 9; }
    case "$4" in
      *RepoDigests*) printf '%s' "$FAKE_REPO_DIGESTS" ;;
      *Labels*) printf '%s\n' "${FAKE_VERSION:-<no value>}" ;;
      *) exit 9 ;;
    esac
    ;;
  *) exit 9 ;;
esac
"""


def _run(tmp_path, digests, colima_list=RUNNING, pad=0):
    bin_dir = tmp_path / "bin"
    vm_bin = tmp_path / "vm-bin"
    for d, name, body in ((bin_dir, "colima", FAKE_COLIMA), (vm_bin, "docker", FAKE_DOCKER)):
        d.mkdir(exist_ok=True)
        f = d / name
        f.write_text(body)
        f.chmod(0o755)
    target = tmp_path / "start-agent.sh"
    if not target.exists():
        target.write_text(FIXTURE)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_BIN": str(vm_bin),
        "FAKE_COLIMA_LIST": colima_list,
        "FAKE_COLIMA_PAD": str(pad) if pad else "",
        "FAKE_REPO_DIGESTS": digests,
        "FAKE_VERSION": "2026.11.2+deadbeef",
        "START_AGENT": str(target),
    }
    proc = subprocess.run(["bash", str(BUMP)], capture_output=True, text=True,
                          timeout=30, env=env)
    return proc, target.read_text()


def _pin(text):
    m = re.search(r'^SEARXNG_IMAGE="docker\.io/searxng/searxng@(sha256:[0-9a-f]{64})"$',
                  text, re.MULTILINE)
    return m and m.group(1)


@pytest.mark.parametrize("entry", [
    "searxng/searxng@" + NEW,
    "docker.io/searxng/searxng@" + NEW,
])
def test_takes_the_searxng_digest(tmp_path, entry):
    proc, text = _run(tmp_path, entry + "\n")
    assert proc.returncode == 0, proc.stderr
    assert _pin(text) == NEW
    assert "2026.11.2+deadbeef" in text


def test_skips_other_repos_listed_first(tmp_path):
    digests = ("example.com/mirror/searxng@" + OTHER + "\n"
               "someone/searxng-fork@" + OTHER + "\n"
               "searxng/searxng@" + NEW + "\n")
    proc, text = _run(tmp_path, digests)
    assert proc.returncode == 0, proc.stderr
    assert _pin(text) == NEW


@pytest.mark.parametrize("digests", [
    "",
    "example.com/mirror/searxng@" + OTHER + "\n",
    "other/searxng/searxng@" + OTHER + "\n",
])
def test_fails_without_a_searxng_entry(tmp_path, digests):
    proc, text = _run(tmp_path, digests)
    assert proc.returncode != 0
    assert "RepoDigests" in proc.stderr
    assert text == FIXTURE


def test_running_check_survives_sigpipe(tmp_path):
    # ~2 MiB after the "Running" line: a `colima list | grep -q` pipeline under
    # pipefail would see colima die of SIGPIPE and report the VM as stopped.
    proc, text = _run(tmp_path, "searxng/searxng@" + NEW + "\n", pad=2048)
    assert proc.returncode == 0, proc.stderr
    assert _pin(text) == NEW


def test_stopped_vm_is_refused(tmp_path):
    proc, text = _run(tmp_path, "searxng/searxng@" + NEW + "\n",
                      colima_list='{"name":"claude-agent","status":"Stopped"}')
    assert proc.returncode != 0 and "not running" in proc.stderr
    assert text == FIXTURE
