"""--refresh-tools / --restore-previous-image, the per-image tool-version record,
and the SearXNG bump script (plans/safe-tool-version-updates.md, Phase 1)."""
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "start-agent.sh"
_TEXT = SCRIPT.read_text()


def _function_body(name: str) -> str:
    start = _TEXT.find(f"\n{name}() {{")
    assert start != -1, f"{name}() not found in start-agent.sh"
    brace_open = _TEXT.find("{", start)
    depth = 0
    for i, ch in enumerate(_TEXT[brace_open:], start=brace_open):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _TEXT[start + 1 : i + 1]
    raise AssertionError(f"{name}() body not terminated")


def _usage_text() -> str:
    start = _TEXT.find("usage() {")
    end = _TEXT.find("\nUSAGE\n", start)
    assert start != -1 and end != -1
    return _TEXT[start:end]


# ── flag parsing and help ─────────────────────────────────────────────────────

@pytest.mark.parametrize("flag", ["--refresh-tools", "--restore-previous-image"])
def test_flag_parsed_and_documented(flag):
    assert f"    {flag})" in _TEXT, f"{flag}) case missing from the arg parser"
    assert flag in _usage_text(), f"{flag} missing from usage()"


@pytest.mark.parametrize("flag", ["--refresh-tools", "--restore-previous-image"])
def test_flag_alone_is_accepted(flag, tmp_path):
    # Outside a sandbox the script stops at sandbox detection, which runs
    # after argument validation: a parsed flag gets that far.
    proc = subprocess.run(["bash", str(SCRIPT), flag], cwd=tmp_path,
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 1
    assert "unknown option" not in proc.stderr
    assert "no .sandbox_config/" in proc.stderr


@pytest.mark.parametrize("a,b", [
    ("--refresh-tools", "--rebuild"),
    ("--refresh-tools", "--reset-container"),
    ("--restore-previous-image", "--rebuild"),
    ("--restore-previous-image", "--reset-container"),
    ("--refresh-tools", "--restore-previous-image"),
    ("--refresh-tools", "--reload-allowlist"),
    ("--restore-previous-image", "--reseed-allowlist"),
])
def test_mutually_exclusive(a, b, tmp_path):
    proc = subprocess.run(["bash", str(SCRIPT), a, b], cwd=tmp_path,
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 1, proc.stderr
    first = proc.stderr.splitlines()[0]
    assert first.startswith("error:"), proc.stderr
    assert "no .sandbox_config/" not in proc.stderr, "exclusion must be checked before sandbox detection"
    assert a in first and (b in first or b.replace("reseed", "reload") in first), first


def test_exclusions_sit_with_the_existing_ones():
    # Same block as the --reset-container/--rebuild guard, before sandbox helpers.
    guard = _TEXT.find("if $RESET_CONTAINER && $REBUILD; then")
    helpers = _TEXT.find("# ── sandbox helpers")
    for var in ("REFRESH_TOOLS", "RESTORE_PREVIOUS"):
        m = re.search(rf"^if .*\${var}", _TEXT, re.MULTILINE)
        assert m and guard < m.start() < helpers, f"${var} exclusion not in the arg-validation block"


# ── image lifecycle ───────────────────────────────────────────────────────────

def _image_section() -> str:
    start = _TEXT.find("# ── image lifecycle")
    end = _TEXT.find("# ── SearXNG container lifecycle")
    assert start != -1 and end != -1 and start < end, "image lifecycle section not found"
    return _TEXT[start:end]


def test_refresh_tags_previous_before_building_with_fresh_cache_bust():
    sec = _image_section()
    tag = sec.find('docker tag "$IMAGE_TAG" "$PREVIOUS_IMAGE_TAG"')
    bust = sec.find("--build-arg TOOLS_CACHE_BUST=")
    assert tag != -1, "refresh must tag the current image as previous"
    assert bust != -1, "refresh must build with --build-arg TOOLS_CACHE_BUST=<fresh value>"
    assert tag < bust, "previous must be tagged before the rebuild moves latest"
    assert re.search(r"TOOLS_CACHE_BUST=\"?\$\(date", sec), "cache-bust value must be fresh per run"
    assert 'PREVIOUS_IMAGE_TAG="claude-agent:previous"' in _TEXT


def test_refresh_and_restore_recreate_containers_after_the_image_moves():
    sec = _image_section()
    for label in ("--refresh-tools", "--restore-previous-image"):
        assert f'remove_containers "{label}"' in sec, f"{label} must recreate containers like --reset-container"
    assert sec.find("--build-arg TOOLS_CACHE_BUST=") < sec.find('remove_containers "--refresh-tools"'), (
        "remove containers only after a successful build, so a failed refresh leaves them intact"
    )


def test_restore_fails_clearly_without_previous():
    sec = _image_section()
    m = re.search(r'if ! docker image inspect "\$PREVIOUS_IMAGE_TAG".*?\n(.*?)\n\s*exit 1', sec, re.DOTALL)
    assert m and "error:" in m.group(1), "restore must print an error and exit 1 when previous is missing"
    assert 'docker tag "$PREVIOUS_IMAGE_TAG" "$IMAGE_TAG"' in sec


def test_plain_build_uses_no_cache_bust():
    # Only --refresh-tools pulls fresh npm packages; a missing-image build or
    # --rebuild reuses whatever the builder has cached.
    sec = _image_section()
    assert sec.count("--build-arg TOOLS_CACHE_BUST=") == 1


def test_versions_printed_old_to_new():
    sec = _image_section()
    assert "→" in sec and "print_tool_versions_change" in sec


# ── tool-version record ───────────────────────────────────────────────────────

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
case "$1" in
  image) [[ -n "${FAKE_IMAGE_ID:-}" ]] && echo "$FAKE_IMAGE_ID" || exit 1 ;;
  run)   [[ "${FAKE_RUN_FAIL:-0}" == 1 ]] && exit 1
         printf 'opencode=%s\npi=%s\n' "${FAKE_OPENCODE-1.19.5}" "${FAKE_PI-1.5.0}" ;;
  *)     exit 1 ;;
esac
"""


def _record(tmp_path, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    vfile = tmp_path / "tool-versions"
    script = (
        f'IMAGE_TAG=claude-agent:latest\nTOOL_VERSIONS_FILE="{vfile}"\n'
        + _function_body("record_tool_versions")
        + "\nrecord_tool_versions\necho rc=$?\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls), **env},
    )
    return proc, vfile, calls.read_text()


def test_record_writes_versions_keyed_by_image_id(tmp_path):
    proc, vfile, calls = _record(tmp_path, FAKE_IMAGE_ID="sha256:aaa")
    assert "rc=0" in proc.stdout
    text = vfile.read_text()
    assert "image_id=sha256:aaa\n" in text
    assert "opencode=1.19.5\n" in text and "pi=1.5.0\n" in text
    assert re.search(r"^recorded=\d{4}-\d\d-\d\dT", text, re.MULTILINE)
    run = [l for l in calls.splitlines() if l.startswith("run ")]
    assert len(run) == 1 and "--rm" in run[0] and "--network none" in run[0]


def test_record_skips_container_when_image_unchanged(tmp_path):
    _record(tmp_path, FAKE_IMAGE_ID="sha256:aaa")
    proc, vfile, calls = _record(tmp_path, FAKE_IMAGE_ID="sha256:aaa")
    assert "rc=0" in proc.stdout
    assert not [l for l in calls.splitlines() if l.startswith("run ")], "warm path must not start a container"


def test_record_rerecords_when_image_changes(tmp_path):
    _record(tmp_path, FAKE_IMAGE_ID="sha256:aaa")
    proc, vfile, calls = _record(tmp_path, FAKE_IMAGE_ID="sha256:bbb", FAKE_OPENCODE="1.20.0")
    assert "image_id=sha256:bbb\n" in vfile.read_text()
    assert "opencode=1.20.0\n" in vfile.read_text()


@pytest.mark.parametrize("env", [
    {"FAKE_IMAGE_ID": "sha256:aaa", "FAKE_RUN_FAIL": "1"},
    {"FAKE_IMAGE_ID": "sha256:aaa", "FAKE_PI": ""},
    {"FAKE_IMAGE_ID": ""},
])
def test_record_failure_never_aborts_and_keeps_old_file(tmp_path, env):
    _record(tmp_path, FAKE_IMAGE_ID="sha256:old")
    before = (tmp_path / "tool-versions").read_text()
    proc, vfile, _ = _record(tmp_path, **env)
    assert "rc=0" in proc.stdout
    assert vfile.read_text() == before


def test_record_not_called_on_warm_reattach():
    assert "record_tool_versions" not in _function_body("attach_existing")
    reattach = _TEXT.find('if docker container inspect "$CONTAINER_NAME" &>/dev/null; then\n  # All projects')
    fresh = _TEXT.find("# Fresh container:")
    assert reattach != -1 and fresh != -1
    assert "record_tool_versions" not in _TEXT[reattach:fresh]
    assert "record_tool_versions" in _TEXT[fresh:_TEXT.find("exec docker run", fresh)], (
        "a fresh container should record versions for images built before the record existed"
    )


def test_tool_versions_file_in_sandbox_config():
    assert 'TOOL_VERSIONS_FILE="$SANDBOX_ROOT/.sandbox_config/tool-versions"' in _TEXT


# ── SearXNG follows the pin ───────────────────────────────────────────────────

def test_searxng_recreated_when_pin_changes():
    start = _TEXT.find("# ── SearXNG container lifecycle")
    sec = _TEXT[start:_TEXT.find("# ── host-side persistent state dirs", start)]
    assert "{{.Config.Image}}" in sec and '"$SEARXNG_IMAGE"' in sec, (
        "an existing searxng container on another image must be recreated from the pin"
    )


# ── image lifecycle, run for real against a stateful fake docker ──────────────

STATEFUL_DOCKER = r"""#!/usr/bin/env bash
S="$STATE"; echo "$*" >> "$S/calls"
tagf() { printf '%s/tags/%s' "$S" "${1//[:\/]/_}"; }
case "$1" in
  image)
    [[ "$2" == inspect ]] || exit 1
    if [[ "$3" == -f ]]; then cat "$(tagf "$5")" 2>/dev/null || exit 1
    else [[ -f "$(tagf "$3")" ]]; fi ;;
  tag)   cp "$(tagf "$2")" "$(tagf "$3")" ;;
  build) [[ "${FAKE_BUILD_FAIL:-0}" == 1 ]] && exit 1
         n=$(( $(cat "$S/builds" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$S/builds"
         out=""; prev=""
         for a in "$@"; do [[ "$prev" == -t ]] && out="$a"; prev="$a"; done
         echo "sha256:img$n" > "$(tagf "$out")" ;;
  run)   id=$(cat "$(tagf claude-agent:latest)"); n="${id#sha256:img}"
         printf 'opencode=1.0.%s\npi=2.0.%s\n' "$n" "$n" ;;
  *)     exit 1 ;;
esac
"""


def _lifecycle(tmp_path, flags, latest=None, previous=None, **env):
    state = tmp_path / "state"
    (state / "tags").mkdir(parents=True)
    if latest:
        (state / "tags" / "claude-agent_latest").write_text(latest + "\n")
    if previous:
        (state / "tags" / "claude-agent_previous").write_text(previous + "\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(STATEFUL_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    start = _TEXT.find("# ── image lifecycle")
    section = _TEXT[start:_TEXT.find("# ── SearXNG container lifecycle")]
    script = (
        "set -euo pipefail\n"
        f"REFRESH_TOOLS={'true' if '--refresh-tools' in flags else 'false'}\n"
        f"RESTORE_PREVIOUS={'true' if '--restore-previous-image' in flags else 'false'}\n"
        'IMAGE_TAG="claude-agent:latest"; PREVIOUS_IMAGE_TAG="claude-agent:previous"\n'
        f'TOOL_VERSIONS_FILE="{tmp_path}/tool-versions"\n'
        'DOCKERFILE_PATH=/x/Dockerfile; DOCKERFILE_DIR=/x\n'
        'remove_containers() { echo "REMOVED $1"; }\n'
        + section + "\necho END\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=20,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "STATE": str(state), **env},
    )

    def tag(name):
        f = state / "tags" / name
        return f.read_text().strip() if f.exists() else None

    calls = (state / "calls").read_text() if (state / "calls").exists() else ""
    return proc, tag("claude-agent_latest"), tag("claude-agent_previous"), calls


def test_refresh_flow(tmp_path):
    proc, latest, previous, calls = _lifecycle(tmp_path, ["--refresh-tools"], latest="sha256:img0")
    assert "END" in proc.stdout, proc.stderr
    assert previous == "sha256:img0" and latest == "sha256:img1"
    assert "REMOVED --refresh-tools" in proc.stdout
    assert "opencode 1.0.0 → 1.0.1, pi 2.0.0 → 2.0.1" in proc.stdout
    build = [l for l in calls.splitlines() if l.startswith("build ")]
    assert len(build) == 1 and re.search(r"--build-arg TOOLS_CACHE_BUST=\d+", build[0])
    assert "image_id=sha256:img1" in (tmp_path / "tool-versions").read_text()


def test_refresh_build_failure_keeps_containers(tmp_path):
    proc, latest, previous, _ = _lifecycle(tmp_path, ["--refresh-tools"], latest="sha256:img0",
                                           FAKE_BUILD_FAIL="1")
    assert proc.returncode != 0 and "END" not in proc.stdout
    assert "REMOVED" not in proc.stdout
    assert latest == "sha256:img0" == previous


def test_restore_flow(tmp_path):
    proc, latest, previous, _ = _lifecycle(tmp_path, ["--restore-previous-image"],
                                           latest="sha256:img5", previous="sha256:img4")
    assert "END" in proc.stdout, proc.stderr
    assert latest == "sha256:img4"
    assert "REMOVED --restore-previous-image" in proc.stdout
    assert "opencode 1.0.5 → 1.0.4" in proc.stdout


def test_restore_without_previous_errors(tmp_path):
    proc, latest, _, _ = _lifecycle(tmp_path, ["--restore-previous-image"], latest="sha256:img5")
    assert proc.returncode == 1
    assert "error:" in proc.stderr and "--refresh-tools" in proc.stderr
    assert "REMOVED" not in proc.stdout and latest == "sha256:img5"


def test_missing_image_plain_build(tmp_path):
    proc, latest, previous, calls = _lifecycle(tmp_path, [])
    assert "END" in proc.stdout, proc.stderr
    assert latest == "sha256:img1" and previous is None
    assert "TOOLS_CACHE_BUST" not in calls and "REMOVED" not in proc.stdout


def test_existing_image_no_flags_is_untouched(tmp_path):
    proc, latest, _, calls = _lifecycle(tmp_path, [], latest="sha256:img3")
    assert "END" in proc.stdout and latest == "sha256:img3"
    assert not [l for l in calls.splitlines() if l.startswith(("build", "run", "tag"))]


# ── scripts/bump-searxng.sh (offline: --digest skips the VM pull) ─────────────

BUMP = REPO / "scripts" / "bump-searxng.sh"
NEW = "sha256:" + "ab" * 32


def _bump(tmp_path, *args):
    target = tmp_path / "start-agent.sh"
    if not target.exists():
        target.write_text(_TEXT)
    proc = subprocess.run(["bash", str(BUMP), *args], capture_output=True, text=True, timeout=20,
                          env={**os.environ, "START_AGENT": str(target)})
    return proc, target.read_text()


def test_bump_rewrites_pin_and_date(tmp_path):
    proc, text = _bump(tmp_path, "--digest", NEW, "--version", "2026.11.2+deadbeef")
    assert proc.returncode == 0, proc.stderr
    assert re.search(r'^SEARXNG_IMAGE="docker\.io/searxng/searxng@' + NEW + '"$', text, re.MULTILINE)
    assert re.search(r"known good on \d{4}-\d\d-\d\d", text)
    assert "2026.11.2+deadbeef" in text
    # Only the comment block and the pin line changed.
    old_lines, new_lines = _TEXT.splitlines(), text.splitlines()
    i = next(n for n, l in enumerate(old_lines) if l.startswith("SEARXNG_IMAGE="))
    j = next(n for n, l in enumerate(new_lines) if l.startswith("SEARXNG_IMAGE="))
    assert old_lines[i + 1:] == new_lines[j + 1:]
    start = i
    while old_lines[start - 1].startswith("#"):
        start -= 1
    assert old_lines[:start] == new_lines[:start]


def test_bump_output_keeps_pin_test_format(tmp_path):
    _, text = _bump(tmp_path, "--digest", NEW)
    refs = [l for l in text.splitlines() if "searxng/searxng" in l and not l.lstrip().startswith("#")]
    assert len(refs) == 1


def test_bump_is_idempotent(tmp_path):
    _bump(tmp_path, "--digest", NEW)
    proc, text = _bump(tmp_path, "--digest", NEW)
    assert proc.returncode == 0 and "already" in proc.stdout


@pytest.mark.parametrize("bad", ["sha256:xyz", "deadbeef", "sha256:" + "a" * 63])
def test_bump_rejects_bad_digest(tmp_path, bad):
    proc, text = _bump(tmp_path, "--digest", bad)
    assert proc.returncode != 0 and text == _TEXT


def test_bump_does_not_commit():
    assert "git commit" not in BUMP.read_text()


def test_bump_pulls_in_the_claude_agent_vm():
    src = BUMP.read_text()
    assert "colima ssh -p" in src and "searxng/searxng:latest" in src and "RepoDigests" in src
