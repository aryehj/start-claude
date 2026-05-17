"""Static analysis: docker run commands in start-agent.sh must not publish host ports.
Also verifies --reset-container and sandbox trust-boundary invariants."""
import re
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "start-agent.sh"
_SCRIPT_TEXT = SCRIPT.read_text()


def _collect_docker_run_block(lines: list[str], start: int) -> str:
    block = []
    i = start
    while i < len(lines):
        block.append(lines[i])
        if not lines[i].rstrip().endswith("\\"):
            break
        i += 1
    return "\n".join(block)


def _find_docker_run_blocks(script_text: str) -> list[str]:
    lines = script_text.splitlines()
    blocks = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if "docker run" in stripped and not stripped.startswith("#"):
            blocks.append(_collect_docker_run_block(lines, i))
    return blocks


def _has_host_port_binding(block: str) -> bool:
    # -p followed by a digit to distinguish port-publish from protocol flags like -p tcp
    return bool(re.search(r"(?:^|\s)-p\s+\d", block)) or "--publish" in block


_BLOCKS = _find_docker_run_blocks(_SCRIPT_TEXT)


def _assert_no_host_port_binding(marker: str, service_name: str) -> None:
    block = next((b for b in _BLOCKS if marker in b), None)
    assert block is not None, f"{service_name} docker run not found in start-agent.sh"
    assert not _has_host_port_binding(block), (
        f"{service_name} docker run exposes a host port (-p or --publish); "
        "reachable from the macOS host and other Colima VMs:\n" + block
    )


def test_searxng_no_host_port_binding():
    _assert_no_host_port_binding("SEARXNG_CONTAINER", "SearXNG")


def test_claude_agent_no_host_port_binding():
    _assert_no_host_port_binding("IMAGE_TAG", "claude-agent")


# ── --reset-container invariants ──────────────────────────────────────────────

def test_reset_container_arg_case_exists():
    assert "--reset-container)" in _SCRIPT_TEXT, (
        "--reset-container) case missing from arg parser"
    )


def test_reset_container_in_help_text():
    # The usage() function text must document the flag.
    assert "--reset-container" in _SCRIPT_TEXT, (
        "--reset-container missing from usage/help block"
    )


def test_reset_container_mutual_exclusion_check():
    # The script must contain a mutual-exclusion guard for --reset-container + --rebuild.
    assert "RESET_CONTAINER" in _SCRIPT_TEXT and "REBUILD" in _SCRIPT_TEXT, (
        "RESET_CONTAINER or REBUILD variable missing"
    )
    # The guard must produce an error message referencing both flags.
    assert re.search(r"reset-container.*rebuild|rebuild.*reset-container", _SCRIPT_TEXT, re.IGNORECASE), (
        "No mutual-exclusion error message referencing both --reset-container and --rebuild found"
    )


def test_reset_container_skips_image_rm():
    # The reset-container branch must NOT contain docker image rm.
    # We verify by asserting the image-rm only appears inside the REBUILD-specific block,
    # not duplicated in a reset-container-only block.
    # Simple invariant: any 'docker image rm' line must be inside a $REBUILD guard.
    lines = _SCRIPT_TEXT.splitlines()
    image_rm_lines = [i for i, l in enumerate(lines) if "docker image rm" in l and not l.strip().startswith("#")]
    for lineno in image_rm_lines:
        # Walk back to find the nearest enclosing if-condition
        context = "\n".join(lines[max(0, lineno - 20):lineno + 1])
        assert "REBUILD" in context, (
            f"docker image rm at line {lineno + 1} is not inside a REBUILD guard:\n{context}"
        )


# ── sandbox trust-boundary invariants ────────────────────────────────────────

def test_init_sandbox_arg_case_exists():
    assert "--init-sandbox)" in _SCRIPT_TEXT or "--init-sandbox=*)" in _SCRIPT_TEXT, (
        "--init-sandbox case missing from arg parser"
    )


def test_init_sandbox_in_help_text():
    assert "--init-sandbox" in _SCRIPT_TEXT, (
        "--init-sandbox missing from usage/help block"
    )


def test_no_legacy_home_paths():
    # After the redesign, all state lives under $SANDBOX_ROOT — no hardcoded
    # ~/.claude-containers/ or ~/.claude-agent/ paths should remain.
    assert ".claude-containers/" not in _SCRIPT_TEXT, (
        "start-agent.sh still references ~/.claude-containers/ — should use $SANDBOX_ROOT/state/"
    )
    assert ".claude-agent/" not in _SCRIPT_TEXT, (
        "start-agent.sh still references ~/.claude-agent/ — should use $SANDBOX_ROOT/state/"
    )


def test_sandbox_root_variable_present():
    assert "SANDBOX_ROOT" in _SCRIPT_TEXT, (
        "SANDBOX_ROOT variable missing from start-agent.sh"
    )


def test_allowlist_mounted_ro():
    # The agent's docker run block must mount the allowlist read-only so the
    # container cannot rewrite which URLs are permitted.
    agent_block = next((b for b in _BLOCKS if "IMAGE_TAG" in b), None)
    assert agent_block is not None, "claude-agent docker run block not found"
    assert ":ro" in agent_block, (
        "allowlist not mounted :ro in claude-agent docker run block"
    )
    assert "/etc/claude-agent/allowlist.txt" in agent_block, (
        "allowlist not mounted at /etc/claude-agent/allowlist.txt in docker run block"
    )


# ── pi integration invariants ─────────────────────────────────────────────────

def test_pi_config_dir_variable_present():
    assert "PI_CONFIG_DIR" in _SCRIPT_TEXT, (
        "PI_CONFIG_DIR variable missing from start-agent.sh"
    )


def test_init_sandbox_creates_pi_dir():
    # init_sandbox() must create the pi state directory so fresh sandboxes
    # get the directory without needing --init-sandbox to be re-run.
    lines = _SCRIPT_TEXT.splitlines()
    in_init_sandbox = False
    found = False
    for line in lines:
        if "init_sandbox()" in line:
            in_init_sandbox = True
        if in_init_sandbox and "sandbox_config/pi" in line:
            found = True
            break
        if in_init_sandbox and line.strip().startswith("}") and found is False:
            break
    assert found, (
        ".sandbox_config/pi not created in init_sandbox(); "
        "fresh sandboxes won't have the pi state dir"
    )


def test_pi_dir_created_every_invocation():
    # PI_CONFIG_DIR/agent must be in the mkdir -p that runs on every invocation
    # so existing sandboxes are backfilled without --init-sandbox.
    assert "PI_CONFIG_DIR/agent" in _SCRIPT_TEXT or '"$PI_CONFIG_DIR/agent"' in _SCRIPT_TEXT, (
        "$PI_CONFIG_DIR/agent not in the every-invocation mkdir -p block; "
        "existing sandboxes won't get the pi dir automatically"
    )


def test_pi_config_injection_writes_models_json():
    assert "models.json" in _SCRIPT_TEXT, (
        "pi config injection does not write models.json"
    )


def test_pi_config_injection_writes_settings_json():
    assert "settings.json" in _SCRIPT_TEXT, (
        "pi config injection does not write settings.json"
    )


def test_pi_config_injection_uses_correct_settings_keys():
    assert "defaultProvider" in _SCRIPT_TEXT, (
        "pi config injection missing 'defaultProvider' key in settings.json"
    )
    assert "defaultModel" in _SCRIPT_TEXT, (
        "pi config injection missing 'defaultModel' key in settings.json"
    )


def test_pi_mounted_in_docker_run():
    agent_block = next((b for b in _BLOCKS if "IMAGE_TAG" in b), None)
    assert agent_block is not None, "claude-agent docker run block not found"
    assert "/root/.pi" in agent_block, (
        "PI_CONFIG_DIR not mounted at /root/.pi in docker run block"
    )
