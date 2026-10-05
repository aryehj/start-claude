"""Static analysis: docker run commands in start-agent.sh must not publish host ports.
Also verifies --reset-container and sandbox trust-boundary invariants."""
import re
import subprocess
import sys
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
    import re
    mkdir_lines = [
        line for line in _SCRIPT_TEXT.splitlines()
        if re.match(r"\s*mkdir -p ", line) and "PI_CONFIG_DIR/agent" in line
    ]
    assert mkdir_lines, (
        "$PI_CONFIG_DIR/agent not in any every-invocation `mkdir -p` line; "
        "existing sandboxes won't get the pi dir automatically"
    )


# The pi-config injection lives in a single python3 heredoc. Scoping the
# write-side assertions to that heredoc prevents them from passing on
# unrelated mentions of "models.json" / "settings.json" elsewhere in the script.
def _pi_inject_block():
    marker = "inject pi config"
    start = _SCRIPT_TEXT.find(marker)
    assert start != -1, "pi config injection block not found"
    # Skip the opening "<< 'PYEOF'" tag and locate the closing PYEOF on its own line.
    open_tag = _SCRIPT_TEXT.find("<< 'PYEOF'", start)
    assert open_tag != -1, "pi config injection heredoc opener not found"
    end = _SCRIPT_TEXT.find("\nPYEOF", open_tag)
    assert end != -1, "pi config injection block not terminated"
    return _SCRIPT_TEXT[start:end]


def test_pi_config_injection_writes_models_json():
    block = _pi_inject_block()
    assert "models_path" in block and "open(models_path, 'w')" in block, (
        "pi config injection does not open models.json for write"
    )


def test_pi_config_injection_writes_settings_json():
    block = _pi_inject_block()
    assert "settings_path" in block and "open(settings_path, 'w')" in block, (
        "pi config injection does not open settings.json for write"
    )


def test_pi_config_injection_uses_correct_settings_keys():
    block = _pi_inject_block()
    assert '"defaultProvider"' in block, (
        "pi config injection missing 'defaultProvider' key write"
    )
    assert '"defaultModel"' in block, (
        "pi config injection missing 'defaultModel' key write"
    )


def test_pi_mounted_in_docker_run():
    agent_block = next((b for b in _BLOCKS if "IMAGE_TAG" in b), None)
    assert agent_block is not None, "claude-agent docker run block not found"
    assert "/root/.pi" in agent_block, (
        "PI_CONFIG_DIR not mounted at /root/.pi in docker run block"
    )


# ── agents/skills seeding invariants ─────────────────────────────────────────

def _function_body(name: str) -> str:
    """Extract the shell function body for a function named `name`."""
    start = _SCRIPT_TEXT.find(f"{name}()")
    assert start != -1, f"{name}() function not found"
    brace_open = _SCRIPT_TEXT.find("{", start)
    assert brace_open != -1, f"{name}() opening brace not found"
    depth = 0
    for i, ch in enumerate(_SCRIPT_TEXT[brace_open:], start=brace_open):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _SCRIPT_TEXT[brace_open : i + 1]
    raise AssertionError(f"{name}() closing brace not found")


def test_init_sandbox_creates_agents_skills_dir():
    body = _function_body("init_sandbox")
    assert "sandbox_config/agents/skills" in body, (
        ".sandbox_config/agents/skills not created in init_sandbox(); "
        "fresh sandboxes won't have the agents skills dir"
    )


def test_init_sandbox_does_not_seed_agent_skills():
    body = _function_body("init_sandbox")
    assert "skills-agents" not in body, (
        "init_sandbox() still contains the skills-agents copy loop; "
        "seeding should live in seed_agent_skills() called on fresh-container path"
    )


def test_seed_agent_skills_function_exists():
    _function_body("seed_agent_skills")


def test_seed_agent_skills_copies_from_skills_agents():
    body = _function_body("seed_agent_skills")
    assert "skills-agents" in body, (
        "seed_agent_skills() does not reference skills-agents/; "
        "Pi/OpenCode won't find the small-model skills"
    )


def _fresh_container_section() -> str:
    """Text from the fresh-container sync comment through the exec docker run."""
    marker = "# Fresh container:"
    start = _SCRIPT_TEXT.find(marker)
    assert start != -1, "'# Fresh container:' comment not found"
    end = _SCRIPT_TEXT.find("exec docker run", start)
    assert end != -1, "exec docker run not found after fresh-container marker"
    return _SCRIPT_TEXT[start:end]


def test_seed_agent_skills_called_on_fresh_container():
    section = _fresh_container_section()
    assert "seed_agent_skills" in section, (
        "seed_agent_skills not called in the fresh-container section; "
        "--rebuild and --reset-container won't repopulate agent skills"
    )


def test_agents_skills_dir_created_every_invocation():
    mkdir_lines = [
        line for line in _SCRIPT_TEXT.splitlines()
        if re.match(r"\s*mkdir -p ", line) and "AGENTS_SKILLS_DIR" in line
    ]
    assert mkdir_lines, (
        "AGENTS_SKILLS_DIR not in any every-invocation `mkdir -p` line; "
        "existing sandboxes won't have the dir and the bind-mount will fail"
    )


def test_agents_skills_mounted_in_docker_run():
    agent_block = next((b for b in _BLOCKS if "IMAGE_TAG" in b), None)
    assert agent_block is not None, "claude-agent docker run block not found"
    assert "/root/.agents/skills" in agent_block, (
        "AGENTS_SKILLS_DIR not mounted at /root/.agents/skills in docker run block"
    )


# ── warm-reattach optimizations ───────────────────────────────────────────────

def test_batch_vm_probe_single_ssh_call():
    # The read-only network discovery must be a single colima ssh invocation
    # (vm-probe.sh piped in) rather than separate vm_ssh calls for bridge/host/agentnet.
    # We verify this by checking that the vm-probe.sh pattern exists and that the
    # old separate per-property vm_ssh patterns do not.
    assert "vm-probe.sh" in _SCRIPT_TEXT, (
        "vm-probe.sh batch probe not found; separate vm_ssh calls for network "
        "discovery were not collapsed into a single round-trip"
    )
    # The old duplicate docker network inspect bridge calls must be gone.
    bridge_inspect_calls = re.findall(
        r'vm_ssh\s+docker\s+network\s+inspect\s+bridge', _SCRIPT_TEXT
    )
    assert len(bridge_inspect_calls) == 0, (
        f"Found {len(bridge_inspect_calls)} separate vm_ssh docker network inspect bridge call(s); "
        "should be zero after batching into vm-probe.sh"
    )


def test_batch_probe_covers_dpkg_query():
    # dpkg-query (tinyproxy install check) must be inside the batch probe,
    # not a separate vm_ssh call on the main code path.
    # The probe script written to disk contains the dpkg-query logic.
    assert "dpkg-query" in _SCRIPT_TEXT, "dpkg-query not found in script at all"
    # There must be no top-level `vm_ssh dpkg-query` outside a heredoc/probe.
    standalone_dpkg = re.findall(r'(?m)^\s*(?:if\s+)?(?:!\s+)?vm_ssh\s+dpkg-query', _SCRIPT_TEXT)
    assert len(standalone_dpkg) == 0, (
        f"Found {len(standalone_dpkg)} standalone vm_ssh dpkg-query call(s); "
        "tinyproxy install check must be part of the batch probe"
    )


def _tinyproxy_push_block() -> str:
    start = _SCRIPT_TEXT.find('if $RELOAD_ALLOWLIST || [[ "$NEW_PROXY_HASH"')
    assert start != -1, "hash-gated tinyproxy push block not found"
    end = _SCRIPT_TEXT.find("\nfi\n", start)
    return _SCRIPT_TEXT[start:end]


def test_tinyproxy_push_hash_gated():
    # The tinyproxy config push must be conditional on a hash comparison so
    # unchanged configs are not pushed on warm reattach.
    assert "TINYPROXY_STORED_HASH" in _SCRIPT_TEXT, (
        "TINYPROXY_STORED_HASH variable not found; hash-gated tinyproxy push not implemented"
    )
    assert "filter.hash" in _SCRIPT_TEXT, (
        "filter.hash marker file not found; stored hash not being persisted in VM"
    )
    # The one push handshake runs inside the hash gate, and nothing else
    # writes the tinyproxy files.
    push_runs = [l for l in _SCRIPT_TEXT.splitlines()
                 if "colima ssh" in l and "tinyproxy-push.sh" in l]
    assert len(push_runs) == 1, f"expected one tinyproxy push handshake, found {push_runs}"
    assert push_runs[0] in _tinyproxy_push_block(), (
        "tinyproxy push runs outside the hash-gated block"
    )


def test_tinyproxy_reload_not_unconditional_restart():
    # On the warm path (config unchanged), tinyproxy must NOT be restarted.
    # We verify by checking there's no unconditional `systemctl restart tinyproxy`
    # outside a conditional block.
    lines = _SCRIPT_TEXT.splitlines()
    restart_lines = [
        i for i, l in enumerate(lines)
        if "systemctl restart tinyproxy" in l and not l.strip().startswith("#")
    ]
    for lineno in restart_lines:
        context = "\n".join(lines[max(0, lineno - 20):lineno + 1])
        assert re.search(r'\bif\b', context), (
            f"systemctl restart tinyproxy at line {lineno + 1} is not inside a "
            "conditional block; it will run unconditionally on every warm reattach"
        )


def test_inference_probe_backgrounded():
    # The inference probe must be launched as a background job so its up-to-3s
    # timeout overlaps the iptables apply and Python config injection.
    assert "PROBE_PID" in _SCRIPT_TEXT, (
        "PROBE_PID not found; inference probe is not backgrounded"
    )
    # The background launch: the probe block's closing brace is followed by `&`
    # (job control). PROBE_PID=$! captures the job id immediately after.
    assert re.search(r'PROBE_PID=\$!', _SCRIPT_TEXT), (
        "PROBE_PID=$! not found; background job is not being captured for wait"
    )
    assert re.search(r'}\s*>.*probe-warning.*&', _SCRIPT_TEXT, re.DOTALL), (
        "No '} > probe-warning ... &' block found; inference probe must be backgrounded"
    )


def test_wait_inference_probe_before_exec():
    # wait_inference_probe (or equivalent) must be called before both exec paths
    # so there are no orphaned background processes and the warning is emitted.
    assert "wait_inference_probe" in _SCRIPT_TEXT, (
        "wait_inference_probe not found; background probe is never awaited"
    )
    # Must appear before attach_existing's exec docker exec and before exec docker run.
    attach_fn = _function_body("attach_existing")
    assert "wait_inference_probe" in attach_fn, (
        "wait_inference_probe not called inside attach_existing(); "
        "warm-reattach path would leave an orphaned background probe"
    )
    # Must appear before exec docker run in the fresh-container section.
    fresh_section = _fresh_container_section()
    assert "wait_inference_probe" in fresh_section, (
        "wait_inference_probe not called before exec docker run in fresh-container section"
    )


def test_unknown_flag_rejected():
    # Without a `-*)` arm, a typo like --rebuilt falls into POSITIONAL, becomes
    # PROJECT_DIR, and fails later with a cryptic `cd: --: invalid option`.
    assert re.search(r"^\s*-\*\)\s.*unknown option", _SCRIPT_TEXT, re.MULTILINE), (
        "arg parser has no `-*)` arm rejecting unknown options"
    )


def test_inference_probe_retries():
    # A single 3s curl false-fails when the inference server is momentarily slow.
    assert "probe_retry vm_ssh curl" in _SCRIPT_TEXT, (
        "inference probe must go through probe_retry, not a single curl attempt"
    )
    assert "NOT reachable at startup" in _SCRIPT_TEXT, (
        "summary line must flag a failed probe instead of printing inference as healthy"
    )


# ── batch 1: security and correctness ─────────────────────────────────────────

def _firewall_script() -> str:
    start = _SCRIPT_TEXT.find("<<FWEOF")
    assert start != -1, "firewall-apply.sh heredoc not found"
    end = _SCRIPT_TEXT.find("\nFWEOF", start)
    assert end != -1, "firewall-apply.sh heredoc not terminated"
    return _SCRIPT_TEXT[start:end]


def test_firewall_chain_applied_atomically():
    # Flush + separate -A calls leave CLAUDE_AGENT empty (egress allowed) until
    # the final REJECT lands. iptables-restore --noflush swaps the chain in one commit.
    fw = _firewall_script()
    assert "iptables-restore --noflush" in fw, (
        "CLAUDE_AGENT chain must be applied via iptables-restore --noflush"
    )
    assert ":CLAUDE_AGENT - [0:0]" in fw, "restore stanza must declare :CLAUDE_AGENT"
    assert "iptables -F CLAUDE_AGENT" not in fw, "non-atomic flush still present"
    assert not re.search(r"iptables\s+-A\s+CLAUDE_AGENT", fw), (
        "individual `iptables -A CLAUDE_AGENT` calls still present"
    )
    # Catch-all REJECT and COMMIT must both be in the stanza, REJECT last.
    reject = fw.find("-A CLAUDE_AGENT -j REJECT")
    commit = fw.find("COMMIT")
    assert reject != -1 and commit != -1 and reject < commit, (
        "restore stanza must end with the catch-all REJECT followed by COMMIT"
    )
    # The jump rules need the chain to exist, so they must follow the restore.
    assert fw.find("iptables-restore") < fw.find("-j CLAUDE_AGENT 2>/dev/null"), (
        "DOCKER-USER jump rules must be installed after the chain is populated"
    )


def test_docker_context_not_persisted():
    # `docker context use` writes currentContext into ~/.docker/config.json and
    # redirects every later docker command on the Mac. DOCKER_CONTEXT is per-process.
    assert "docker context use" not in _SCRIPT_TEXT, (
        "`docker context use` rewrites the user's global docker config"
    )
    assert re.search(r'export DOCKER_CONTEXT="colima-\$COLIMA_PROFILE"', _SCRIPT_TEXT), (
        "DOCKER_CONTEXT must be exported for the script's docker calls"
    )


def test_secrets_not_in_docker_argv():
    # -e NAME=value puts the secret in the docker argv (visible in host `ps`);
    # value-less -e NAME makes docker read it from its own environment.
    assert not re.search(r'-e\s+"?\w*(API_KEY|TOKEN)\w*=', _SCRIPT_TEXT), (
        "secret passed as -e NAME=value; use value-less -e NAME passthrough"
    )
    assert re.search(r"DOCKER_ENV_ARGS\+=\(-e OMLX_API_KEY\)", _SCRIPT_TEXT), (
        "OMLX_API_KEY must be forwarded with value-less -e"
    )


def test_cloud_api_keys_forwarded():
    # CLAUDE.md, README and ADR-036 promise Pi can use cloud providers via
    # host-forwarded keys; both docker run and docker exec consume DOCKER_ENV_ARGS.
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert key in _SCRIPT_TEXT, f"{key} is not forwarded into the container"
    assert re.search(r'DOCKER_ENV_ARGS\+=\(-e "\$key"\)', _SCRIPT_TEXT), (
        "cloud API keys must be forwarded with value-less -e"
    )


def _vm_sizing_parser() -> str:
    m = re.search(r"running_sizing=\$\(.*?python3 -c '(.*?)'", _SCRIPT_TEXT, re.DOTALL)
    assert m, "running_sizing python parser not found"
    return m.group(1)


def test_vm_memory_compared_as_integer_gib():
    # colima list --json reports memory in bytes; a substring match against the
    # requested GiB ("8" in "12884901888") never warns.
    assert '*"${CLAUDE_AGENT_MEMORY_GB}"*' not in _SCRIPT_TEXT, (
        "memory mismatch still uses a substring glob against a byte count"
    )
    import subprocess, sys
    code = _vm_sizing_parser()
    cases = {
        '{"cpus": 6, "memory": 12884901888}': "6|12",
        '{"cpus": 2, "memory": 8589934592}': "2|8",
        '{"cpus": 4}': "4|",
        '{"memory": 6442450944}': "|6",
    }
    for payload, expected in cases.items():
        out = subprocess.run([sys.executable, "-c", code], input=payload,
                             capture_output=True, text=True, check=True).stdout.strip()
        assert out == expected, f"{payload} -> {out!r}, expected {expected!r}"
    assert re.search(r'"\$running_mem_gib"\s+-ne\s+"\$CLAUDE_AGENT_MEMORY_GB"', _SCRIPT_TEXT), (
        "memory mismatch must be an integer comparison against CLAUDE_AGENT_MEMORY_GB"
    )


def test_vm_probe_failure_is_reported():
    # Under set -eo pipefail, a failed probe with stderr discarded killed the
    # script with no message at all.
    assert not re.search(r'PROBE_OUTPUT=\$\(colima ssh[^\n]*2>/dev/null', _SCRIPT_TEXT), (
        "batch VM probe still discards colima ssh stderr"
    )
    start = _SCRIPT_TEXT.find('vm-probe.sh" 2>')
    assert start != -1, "batch VM probe must capture colima ssh stderr"
    window = _SCRIPT_TEXT[start:start + 400]
    assert "error: could not run" in window and "exit 1" in window, (
        "batch VM probe failure must print an error (with ssh stderr) and exit"
    )


def test_exit_trap_kills_inference_probe():
    # A set -e failure after the probe launches must not leave `colima ssh curl`
    # running detached; the EXIT trap has to reap it along with TMP_WORK.
    assert re.search(r"^trap on_exit EXIT$", _SCRIPT_TEXT, re.MULTILINE), (
        "TMP_WORK cleanup must go through the on_exit trap handler"
    )
    body = _function_body("on_exit")
    assert "PROBE_PID" in body and "kill" in body, "on_exit does not kill the inference probe"
    # The probe is a subshell; its colima ssh child must be killed too.
    assert 'pkill -P "$PROBE_PID"' in body, "on_exit must kill the probe's children"
    assert 'rm -rf "$TMP_WORK"' in body, "on_exit must still remove TMP_WORK"


def test_exit_trap_reaps_probe_silently(tmp_path):
    # Killing the probe from the EXIT trap made macOS bash print a job notice
    # ("line N: PID Terminated: 15 { if ! probe_retry ...") on every early exit,
    # e.g. --reload-allowlist. Reaping it with a stderr-silenced `wait` stops
    # that. Linux bash 5 never prints the notice, so pin the shape statically...
    body = _function_body("on_exit")
    kill = body.find('kill "$PROBE_PID"')
    reap = body.find('wait "$PROBE_PID" 2>/dev/null')
    assert kill != -1 and reap > kill, "on_exit must reap the killed probe with a silenced wait"
    # ...and run the real on_exit against a probe that outlives its children
    # (probe_retry sleeps and retries), to prove the wait cannot hang.
    script = (
        f'TMP_WORK="{tmp_path}/work"; mkdir -p "$TMP_WORK"\n'
        '{ while :; do sleep 1; done; } >/dev/null 2>&1 &\n'
        'PROBE_PID=$!\n'
        f'on_exit() {body}\n'
        'trap on_exit EXIT\n'
        'sleep 0.2\n'
        'exit 0\n'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0
    assert "Terminated" not in proc.stderr, proc.stderr
    assert not (tmp_path / "work").exists()


def test_reload_allowlist_rejects_rebuild_and_reset():
    # --rebuild/--reset-container combined with --reload-allowlist used to drop
    # the first flag silently; the combination must be rejected up front.
    guard = re.search(
        r"if \$RELOAD_ALLOWLIST && \(\$REBUILD \|\| \$RESET_CONTAINER\); then(.*?)\nfi",
        _SCRIPT_TEXT, re.DOTALL,
    )
    assert guard, "no guard rejecting --reload-allowlist with --rebuild/--reset-container"
    assert "exit" in guard.group(1) and "reload-allowlist" in guard.group(1)
    # The old silent-drop conditions are dead once the guard exists.
    assert "&& ! $RELOAD_ALLOWLIST" not in _SCRIPT_TEXT, (
        "leftover `&& ! $RELOAD_ALLOWLIST` conditions silently ignore --rebuild/--reset-container"
    )


_SEED_LINE = 'cat "$ALLOWLIST_TEMPLATE" > "$ALLOWLIST_FILE"'


def test_reseed_allowlist_backs_up_existing_file():
    seed = _SCRIPT_TEXT.find("# ── seed allowlist")
    write = _SCRIPT_TEXT.find(_SEED_LINE, seed)
    assert seed != -1 and write != -1, "allowlist seed block not found"
    before = _SCRIPT_TEXT[seed:write]
    assert re.search(r'cp "\$ALLOWLIST_FILE" "\$ALLOWLIST_FILE\.bak"', before), (
        "--reseed-allowlist must back up the curated allowlist before overwriting it"
    )


def test_python3_in_preflight():
    pre = _SCRIPT_TEXT.find("# ── preflight")
    end = _SCRIPT_TEXT.find("# ── seed allowlist", pre)
    assert "command -v python3" in _SCRIPT_TEXT[pre:end], (
        "python3 is a hard host dependency but is not checked in preflight"
    )


def test_usage_documents_env_vars():
    body = _function_body("usage")
    for var in ("CLAUDE_SKILLS_ARCHIVE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert var in body, f"usage() does not document {var}"


# ── batch 2: firewall gaps ────────────────────────────────────────────────────

def _probe_script() -> str:
    start = _SCRIPT_TEXT.find("<<PROBE_EOF")
    assert start != -1, "vm-probe.sh heredoc not found"
    end = _SCRIPT_TEXT.find("\nPROBE_EOF", start)
    assert end != -1, "vm-probe.sh heredoc not terminated"
    return _SCRIPT_TEXT[start:end]


def _restore_stanza(fw: str) -> str:
    start = fw.find("echo '*filter'")
    end = fw.find("iptables-restore --noflush", start)
    assert start != -1 and end != -1, "iptables-restore stanza not found"
    return fw[start:end]


def test_agent_net_cidr_probed_regardless_of_search():
    # A container left on claude-agent-net by an earlier search-enabled run must
    # still get the DOCKER-USER jump when this run passes --disable-search.
    probe = _probe_script()
    assert 'echo "AGENT_NET_CIDR=' in probe, "probe no longer reports AGENT_NET_CIDR"
    assert "LOCAL_SEARCH_ENABLED" not in probe, (
        "agent-net CIDR probe is still gated on search; the jump is skipped "
        "for a reattached claude-agent-net container under --disable-search"
    )
    fw = _firewall_script()
    jump = fw.find('-s "\\$AGENT_NET_CIDR" -j CLAUDE_AGENT 2>/dev/null')
    assert jump != -1, "DOCKER-USER jump for AGENT_NET_CIDR not found"
    guard = fw.rfind("if ", 0, jump)
    assert "LOCAL_SEARCH_ENABLED" not in fw[guard:jump], (
        "DOCKER-USER jump for AGENT_NET_CIDR must not depend on the search flag"
    )


def test_input_chain_applied_atomically():
    # Container → VM-local address (bridge gateways, 192.168.5.1) traverses INPUT,
    # not FORWARD. CLAUDE_AGENT_IN governs it and is swapped in the same commit.
    fw = _firewall_script()
    stanza = _restore_stanza(fw)
    assert ":CLAUDE_AGENT_IN - [0:0]" in stanza, (
        "CLAUDE_AGENT_IN must be declared in the same restore stanza as CLAUDE_AGENT"
    )
    assert "-A CLAUDE_AGENT_IN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN" in stanza
    assert "-A CLAUDE_AGENT_IN -d \\$BRIDGE_IP -p tcp --dport \\$TINYPROXY_PORT -j RETURN" in stanza
    reject = stanza.find("-A CLAUDE_AGENT_IN -j REJECT --reject-with icmp-admin-prohibited")
    commit = stanza.find("COMMIT")
    assert reject != -1 and reject < commit, "CLAUDE_AGENT_IN must end in a catch-all REJECT"
    in_rules = re.findall(r"-A CLAUDE_AGENT_IN [^\"]*", stanza)
    assert len(in_rules) == 3, f"CLAUDE_AGENT_IN should hold exactly 3 rules, got {in_rules}"
    assert not re.search(r"iptables\s+-A\s+CLAUDE_AGENT_IN", fw), (
        "individual `iptables -A CLAUDE_AGENT_IN` calls still present"
    )


def test_input_chain_jumps_by_source_only():
    fw = _firewall_script()
    restore = fw.find("iptables-restore")
    for cidr in ("BRIDGE_CIDR", "AGENT_NET_CIDR"):
        check = f'iptables -C INPUT -s "\\${cidr}" -j CLAUDE_AGENT_IN 2>/dev/null'
        insert = f'iptables -I INPUT 1 -s "\\${cidr}" -j CLAUDE_AGENT_IN'
        assert check in fw and insert in fw, f"idempotent INPUT jump for {cidr} missing"
        assert fw.find(check) > restore, "INPUT jumps must follow the restore that creates the chain"
    jump_lines = [l for l in fw.splitlines() if "-j CLAUDE_AGENT_IN" in l]
    assert all(" -d " not in l for l in jump_lines), (
        "INPUT jumps must match on source CIDR only so every VM-local address is covered"
    )


def test_dead_bridge_rules_removed_from_forward_chain():
    # $BRIDGE_IP is VM-local, so these FORWARD-chain rules could never match.
    stanza = _restore_stanza(_firewall_script())
    assert not re.search(r"-A CLAUDE_AGENT -d \\\$BRIDGE_IP", stanza), (
        "dead `-A CLAUDE_AGENT -d $BRIDGE_IP` rules still present"
    )


def test_agent_container_dns_blackholed():
    assert re.search(r'^DNS_BLACKHOLE="192\.0\.2\.1"', _SCRIPT_TEXT, re.MULTILINE), (
        "DNS_BLACKHOLE must be the RFC 5737 TEST-NET-1 address"
    )
    agent_block = next((b for b in _BLOCKS if "IMAGE_TAG" in b), None)
    assert agent_block is not None, "claude-agent docker run block not found"
    assert '--dns "$DNS_BLACKHOLE"' in agent_block, (
        "agent container must use the blackhole resolver so external names do not resolve"
    )
    searxng_block = next((b for b in _BLOCKS if "SEARXNG_CONTAINER" in b), None)
    assert searxng_block is not None and "--dns" not in searxng_block, (
        "SearXNG reaches engines via outgoing.proxies; leave its resolver alone"
    )


def test_reattach_warns_when_dns_blackhole_missing():
    start = _SCRIPT_TEXT.find('if docker container inspect "$CONTAINER_NAME"')
    end = _SCRIPT_TEXT.find("# Fresh container:", start)
    assert start != -1 and end != -1, "reattach block not found"
    block = _SCRIPT_TEXT[start:end]
    assert "{{json .HostConfig.Dns}}" in block, "reattach does not inspect the container's resolver"
    assert "DNS_BLACKHOLE" in block and "--reset-container" in block, (
        "reattach must warn and name --reset-container when the blackhole is absent"
    )
    attach = block.find("attach_existing")
    assert block.find("HostConfig.Dns") < attach, "DNS check must run before attaching"


def test_firewall_smoke_covers_dns_and_input():
    smoke = (SCRIPT.parent / "tests" / "test-agent-firewall.sh").read_text()
    for fn in ("test_external_dns_blackholed", "test_container_dns", "test_vm_local_services_closed"):
        assert f"{fn}() {{" in smoke, f"{fn} not defined in test-agent-firewall.sh"
        assert re.search(rf"^{fn}$", smoke, re.MULTILINE), f"{fn} defined but never called"


# ── batch 3: container lifecycle ──────────────────────────────────────────────

def _reattach_block() -> str:
    start = _SCRIPT_TEXT.find('if docker container inspect "$CONTAINER_NAME"')
    end = _SCRIPT_TEXT.find("# Fresh container:", start)
    assert start != -1 and end != -1, "reattach block not found"
    return _SCRIPT_TEXT[start:end]


def _searxng_lifecycle() -> str:
    start = _SCRIPT_TEXT.find("# ── SearXNG container lifecycle")
    end = _SCRIPT_TEXT.find("# ── host-side persistent state dirs", start)
    assert start != -1 and end != -1, "SearXNG lifecycle section not found"
    return _SCRIPT_TEXT[start:end]


def test_agent_container_always_on_agent_net():
    # One topology whatever the search flag says: toggling --disable-search
    # against an existing container must not leave it on the wrong network.
    agent_block = next((b for b in _BLOCKS if "IMAGE_TAG" in b), None)
    assert agent_block is not None, "claude-agent docker run block not found"
    assert '--network "$AGENT_NET_NAME"' in agent_block, (
        "agent container must always join claude-agent-net"
    )
    assert "NETWORK_ARGS" not in _SCRIPT_TEXT, "search-conditional NETWORK_ARGS still present"


def test_agent_net_created_regardless_of_search():
    start = _SCRIPT_TEXT.find("# ── user-defined network")
    end = _SCRIPT_TEXT.find("# ── inference backend preflight", start)
    assert start != -1 and end != -1, "agent-net section not found"
    section = _SCRIPT_TEXT[start:end]
    assert 'docker network create "$AGENT_NET_NAME"' in section
    assert "LOCAL_SEARCH_ENABLED" not in section, (
        "claude-agent-net creation is still gated on the search flag"
    )


def test_reattach_connects_missing_agent_net():
    block = _reattach_block()
    assert ".NetworkSettings.Networks" in block, "reattach does not inspect the container's networks"
    connect = block.find('docker network connect "$AGENT_NET_NAME" "$CONTAINER_NAME"')
    assert connect != -1, "reattach must connect an old container to claude-agent-net"
    assert connect < block.find("attach_existing"), "network connect must run before docker start"
    assert "docker network disconnect" not in _SCRIPT_TEXT, (
        "never disconnect: it can strand the container without a network and cuts live shells"
    )


def test_agent_start_failure_reported_not_swallowed():
    body = _function_body("attach_existing")
    assert '>/dev/null 2>&1 || true' not in body, "attach_existing still swallows docker start failures"
    assert "{{.State.Running}}" in body, "attach_existing must check the container is running"
    assert "--reset-container" in body and "exit 1" in body, (
        "a failed start must exit non-zero and name the --reset-container remedy"
    )
    assert "docker rm" not in body, (
        "the agent container holds user-installed state; never auto-remove it"
    )
    assert body.find("{{.State.Running}}") < body.find("exec docker exec"), (
        "running check must come before exec"
    )


def test_searxng_start_failure_self_heals():
    section = _searxng_lifecycle()
    assert 'docker start "$SEARXNG_CONTAINER" >/dev/null || true' not in section, (
        "SearXNG start failure is still swallowed"
    )
    assert "{{.State.Running}}" in section, "SearXNG start must verify the container is running"
    rm = section.find('docker rm -f "$SEARXNG_CONTAINER"')
    run = section.find("docker run -d")
    assert rm != -1 and run != -1 and rm < run, (
        "a failed SearXNG start must remove the container and fall through to docker run"
    )
    runs = [b for b in _BLOCKS if "SEARXNG_CONTAINER" in b]
    assert len(runs) == 1, f"expected one SearXNG docker run block, got {len(runs)}"


def test_no_restart_policy_on_runtime_containers():
    # dockerd would start them at VM boot, before the firewall is installed.
    for block in _BLOCKS:
        assert "--restart" not in block, "restart policy found on a docker run:\n" + block


def test_searxng_image_pinned_by_digest():
    m = re.search(
        r'^SEARXNG_IMAGE="docker\.io/searxng/searxng@sha256:[0-9a-f]{64}"$',
        _SCRIPT_TEXT, re.MULTILINE,
    )
    assert m, "SEARXNG_IMAGE must be pinned by sha256 digest"
    runs = [b for b in _BLOCKS if "SEARXNG_CONTAINER" in b]
    assert runs and '"$SEARXNG_IMAGE"' in runs[0], "SearXNG docker run must use $SEARXNG_IMAGE"
    refs = [l for l in _SCRIPT_TEXT.splitlines()
            if "searxng/searxng" in l and not l.lstrip().startswith("#")]
    assert len(refs) == 1, f"unpinned SearXNG image reference remains: {refs}"


# ── allowlist filter generator (W1) ──────────────────────────────────────────
# The generator is a python3 heredoc inside generate_filter_file(); run it for
# real on a sample allowlist rather than grepping its source.

def _filter_generator_source() -> str:
    start = _SCRIPT_TEXT.find("generate_filter_file() {")
    assert start != -1, "generate_filter_file() not found"
    open_tag = _SCRIPT_TEXT.find("<<'PYEOF'\n", start)
    assert open_tag != -1, "generate_filter_file python heredoc not found"
    body_start = open_tag + len("<<'PYEOF'\n")
    end = _SCRIPT_TEXT.find("\nPYEOF", body_start)
    assert end != -1, "generate_filter_file python heredoc not terminated"
    return _SCRIPT_TEXT[body_start:end]


def _run_filter_generator(tmp_path, allowlist: str):
    src = tmp_path / "allowlist.txt"
    src.write_text(allowlist)
    proc = subprocess.run(
        [sys.executable, "-", str(src)],
        input=_filter_generator_source(), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines(), proc.stderr


def test_filter_generator_skips_non_domains_with_warning(tmp_path):
    lines, err = _run_filter_generator(
        tmp_path,
        "# header\n"
        "https://foo.com\n"
        "*.bar.com\n"
        "com\n"
        "example.org/path\n"
        "example.net:443\n",
    )
    assert lines == [r"(^|\.)bar\.com$"]
    warnings = err.strip().splitlines()
    assert len(warnings) == 4, err
    assert "warning: allowlist.txt:2: 'https://foo.com' is not a bare domain, skipped" in warnings
    assert any("allowlist.txt:4: 'com'" in w for w in warnings)


def test_filter_generator_normalises_entries(tmp_path):
    lines, err = _run_filter_generator(
        tmp_path,
        "Rust-Lang.org.\n"
        "  docs.python.org   # inline comment\n"
        "\n"
        "10.0.0.1\n",
    )
    assert err == ""
    assert lines == [
        r"(^|\.)rust-lang\.org$",
        r"(^|\.)docs\.python\.org$",
        r"(^|\.)10\.0\.0\.1$",
    ]
    # Only '.' is escaped: '\-' is undefined in POSIX ERE.
    assert "re.escape(" not in _filter_generator_source()


def test_reload_summary_counts_generated_filter():
    block = re.search(
        r"if \$RELOAD_ALLOWLIST; then\n(.*?)\nfi", _SCRIPT_TEXT, re.DOTALL,
    )
    assert block, "--reload-allowlist fast-path block not found"
    body = block.group(1)
    assert '"$TMP_WORK/filter"' in body, "reload summary must count the generated filter"
    assert '"$ALLOWLIST_FILE"' not in body, "reload summary must not grep the source allowlist"


# ── shared allowlist template (W2) ───────────────────────────────────────────

_TEMPLATE = SCRIPT.parent / "templates" / "sandbox-allowlist.txt"


def test_allowlist_seeded_from_shared_template():
    assert "<<'ALLOWLIST'" not in _SCRIPT_TEXT, "inline allowlist heredoc must be gone"
    assert 'ALLOWLIST_TEMPLATE="$SCRIPT_DIR/templates/sandbox-allowlist.txt"' in _SCRIPT_TEXT
    seed = _SCRIPT_TEXT.find("# ── seed allowlist")
    end = _SCRIPT_TEXT.find("\nfi\n", seed)
    block = "\n".join(l for l in _SCRIPT_TEXT[seed:end].splitlines()
                      if not l.lstrip().startswith("#"))
    # Written into the existing file, never replaced: allowlist.txt is a
    # single-file bind mount, and a new inode would strand the container.
    assert _SEED_LINE in block
    assert not re.search(r"(^|\s)mv\s", block), "seed must not mv over the bind-mounted file"


def test_allowlist_template_in_preflight():
    pre = _SCRIPT_TEXT.find("# ── preflight")
    end = _SCRIPT_TEXT.find("# ── seed allowlist", pre)
    assert '-f "$ALLOWLIST_TEMPLATE"' in _SCRIPT_TEXT[pre:end], (
        "the shared allowlist template is a hard dependency but is not checked in preflight"
    )


def test_template_entries_pass_filter_generator(tmp_path):
    text = _TEMPLATE.read_text()
    lines, err = _run_filter_generator(tmp_path, text)
    assert err == "", f"template entries rejected by the filter generator:\n{err}"
    entries = [l for l in (r.split("#", 1)[0].strip() for r in text.splitlines()) if l]
    assert len(lines) == len(entries)


# ── batch 5: perf and cleanup ────────────────────────────────────────────────

def _code_lines() -> list[str]:
    return [l for l in _SCRIPT_TEXT.splitlines() if not l.lstrip().startswith("#")]


_SAMPLE_CONF = 'Port 8888\nFilterDefaultDeny Yes\nFilter "/etc/tinyproxy/filter"\n'


def _render_push_script(tmp_path, active: str, conf_text: str = _SAMPLE_CONF,
                        path: str = "/usr/bin:/bin"):
    """Run write_tinyproxy_push on sample files.

    Returns (exit code, stderr, generated VM script or None).
    """
    import subprocess
    conf = tmp_path / "tinyproxy.conf"
    filt = tmp_path / "filter"
    conf.write_text(conf_text)
    filt.write_text("".join(f"(^|\\.)host{i}-x\\.example$\n" for i in range(300)))
    fn = "write_tinyproxy_push() " + _function_body("write_tinyproxy_push")
    r = subprocess.run(
        ["bash", "-c", f"set -eo pipefail\n{fn}\nwrite_tinyproxy_push"],
        env={"PATH": path, "TMP_WORK": str(tmp_path),
             "NEW_PROXY_HASH": "abc123", "TINYPROXY_ACTIVE": active},
        capture_output=True, text=True,
    )
    out = tmp_path / "tinyproxy-push.sh"
    return r.returncode, r.stderr, (out.read_text() if out.exists() else None)


def _run_push_script(tmp_path, script: str, expect_ok: bool = True) -> list[str]:
    """Execute the VM script against a fake /etc/tinyproxy; return systemctl calls."""
    import subprocess
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    log = tmp_path / "systemctl.log"
    stub = f'systemctl() {{ echo "$*" >> "{log}"; }}\n'
    script = stub + script.replace("/etc/tinyproxy/", f"{etc}/")
    r = subprocess.run(["sh"], input=script, text=True, capture_output=True)
    assert (r.returncode == 0) == expect_ok, r.stderr
    return log.read_text().splitlines() if log.exists() else []


def test_tinyproxy_push_round_trips_files(tmp_path):
    # P1: both files and the hash ride in one script as base64 payloads.
    rc, err, script = _render_push_script(tmp_path, "true")
    assert rc == 0, err
    calls = _run_push_script(tmp_path, script)
    etc = tmp_path / "etc"
    assert (etc / "tinyproxy.conf").read_bytes() == (tmp_path / "tinyproxy.conf").read_bytes()
    assert (etc / "filter").read_bytes() == (tmp_path / "filter").read_bytes()
    assert (etc / "filter.hash").read_text() == "abc123\n"
    assert (etc / "filter").stat().st_mode & 0o777 == 0o644
    assert calls == ["reload tinyproxy"], calls


def test_tinyproxy_push_restarts_when_inactive(tmp_path):
    rc, err, script = _render_push_script(tmp_path, "false")
    assert rc == 0, err
    calls = _run_push_script(tmp_path, script)
    assert calls == ["enable tinyproxy", "restart tinyproxy"], calls


def test_tinyproxy_push_aborts_when_encoding_fails(tmp_path):
    # A failed $(base64 ...) inside the heredoc would silently ship an empty
    # tinyproxy.conf, i.e. one without FilterDefaultDeny.
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "base64").write_text("#!/bin/sh\nexit 1\n")
    (stubs / "base64").chmod(0o755)
    rc, err, _ = _render_push_script(tmp_path, "true", path=f"{stubs}:/usr/bin:/bin")
    assert rc != 0
    assert "tinyproxy" in err


def test_tinyproxy_push_aborts_on_empty_conf(tmp_path):
    rc, err, _ = _render_push_script(tmp_path, "true", conf_text="")
    assert rc != 0
    assert "tinyproxy" in err


def test_vm_refuses_conf_without_default_deny(tmp_path):
    # Last line of defence in the VM: never install or load a config that
    # would let tinyproxy proxy anywhere.
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "tinyproxy.conf").write_text("previous\n")
    (etc / "filter.hash").write_text("old\n")
    rc, err, script = _render_push_script(tmp_path, "true", conf_text="Port 8888\n")
    assert rc == 0, err
    calls = _run_push_script(tmp_path, script, expect_ok=False)
    assert calls == [], "tinyproxy must not be reloaded with a non-default-deny config"
    assert (etc / "tinyproxy.conf").read_text() == "previous\n"
    assert (etc / "filter.hash").read_text() == "old\n", "hash must not advance, so the next run re-pushes"


def test_generated_conf_passes_vm_default_deny_check():
    # The VM-side guard greps for this exact line; the host must emit it.
    conf = re.search(r'tinyproxy\.conf" <<CONF\n(.*?)\nCONF\n', _SCRIPT_TEXT, re.DOTALL)
    assert conf, "tinyproxy.conf heredoc not found"
    assert "FilterDefaultDeny Yes" in conf.group(1).splitlines()


def test_push_failure_stops_the_script():
    block = _tinyproxy_push_block()
    assert re.search(r"write_tinyproxy_push \|\| exit 1", block), (
        "a failed write_tinyproxy_push must stop the script before the firewall and containers"
    )


def test_vm_put_file_gone():
    # Two handshakes per file; the single push script replaces it.
    assert "vm_put_file" not in _SCRIPT_TEXT


def test_inference_probe_skipped_on_reload():
    # --reload-allowlist exits before anything reads the probe result.
    start = _SCRIPT_TEXT.find("# ── inference backend preflight")
    end = _SCRIPT_TEXT.find("# ── SearXNG settings.yml seed", start)
    section = _SCRIPT_TEXT[start:end]
    guard = section.find("if ! $RELOAD_ALLOWLIST; then")
    assert guard != -1, "inference probe launch is not skipped under --reload-allowlist"
    assert guard < section.find('case "$BACKEND" in') < section.rfind("\nfi\n")


def test_single_colima_list_call():
    calls = [l for l in _code_lines() if re.search(r"\bcolima list\b", l)]
    assert len(calls) == 1, f"expected one colima list call, found {calls}"


def test_agent_run_has_no_resource_limits():
    # Both equalled the whole VM (and the requested, not the running, size).
    agent_block = next(b for b in _BLOCKS if "IMAGE_TAG" in b)
    assert "--memory" not in agent_block
    assert "--cpus" not in agent_block


def test_script_dir_computed_once():
    assert 'dirname "$0"' not in _SCRIPT_TEXT
    assert _SCRIPT_TEXT.count('dirname "${BASH_SOURCE[0]}"') == 1
    assert 'DOCKERFILE_PATH="$SCRIPT_DIR/dockerfiles/claude-agent.Dockerfile"' in _SCRIPT_TEXT
    assert 'GLOBAL_CLAUDEMD_TEMPLATE="$SCRIPT_DIR/templates/global-claude.md"' in _SCRIPT_TEXT
