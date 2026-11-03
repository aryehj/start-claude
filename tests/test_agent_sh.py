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
    # The in-VM proxy holds the omlx key, so it is not forwarded at all.
    assert not re.search(r"-e\s+\"?OMLX_API_KEY", _SCRIPT_TEXT), (
        "OMLX_API_KEY must not be forwarded into the container"
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
    assert "-A CLAUDE_AGENT_IN -d \\$BRIDGE_IP -p tcp --dport \\$INFERENCE_PORT -j RETURN" in stanza
    reject = stanza.find("-A CLAUDE_AGENT_IN -j REJECT --reject-with icmp-admin-prohibited")
    commit = stanza.find("COMMIT")
    assert reject != -1 and reject < commit, "CLAUDE_AGENT_IN must end in a catch-all REJECT"
    in_rules = re.findall(r"-A CLAUDE_AGENT_IN [^\"]*", stanza)
    assert len(in_rules) == 4, f"CLAUDE_AGENT_IN should hold exactly 4 rules, got {in_rules}"
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


# ── inference firewall: rendered and run against a stubbed sudo ───────────────

_FW_ENV = {
    "BRIDGE_IP": "172.17.0.1",
    "BRIDGE_CIDR": "172.17.0.0/16",
    "AGENT_NET_CIDR": "172.20.0.0/24",
    "HOST_IP": "192.168.5.2",
    "TINYPROXY_PORT": "8888",
    "INFERENCE_PORT": "8000",
    "LOCAL_SEARCH_ENABLED": "true",
}


def _run_firewall(tmp_path, output_jump_present=False, restore_rc=0, restore6_rc=0):
    """Run firewall-apply.sh for real, recording what reaches iptables-restore.

    What reaches ip6tables-restore lands in tmp_path / "restore6.txt".
    """
    body = _firewall_script().split("\n", 1)[1]
    rendered = subprocess.run(
        ["bash", "-c", f"cat <<FWEOF\n{body}\nFWEOF"],
        env={"PATH": os.environ["PATH"], **_FW_ENV},
        capture_output=True, text=True, check=True,
    ).stdout
    script = tmp_path / "firewall-apply.sh"
    script.write_text(rendered)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    restore_log = tmp_path / "restore.txt"
    calls_log = tmp_path / "calls.txt"
    sudo = bin_dir / "sudo"
    sudo.write_text(f"""#!/bin/sh
if [ "$1" = iptables-restore ]; then
  cat > '{restore_log}'
  exit {restore_rc}
fi
if [ "$1" = ip6tables-restore ]; then
  cat > '{tmp_path / "restore6.txt"}'
  exit {restore6_rc}
fi
echo "$*" >> '{calls_log}'
case "$*" in
  *"-C OUTPUT"*) exit {0 if output_jump_present else 1} ;;
esac
exit 0
""")
    sudo.chmod(0o755)
    proc = subprocess.run(
        ["sh", str(script)],
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True, text=True,
    )
    restore = restore_log.read_text().splitlines() if restore_log.exists() else []
    calls = calls_log.read_text().splitlines() if calls_log.exists() else []
    return proc, restore, calls


def test_firewall_opens_no_container_path_to_host(tmp_path):
    proc, restore, _ = _run_firewall(tmp_path)
    assert proc.returncode == 0, proc.stderr
    forward = [l for l in restore if l.startswith("-A CLAUDE_AGENT ")]
    assert forward[-1].startswith("-A CLAUDE_AGENT -j REJECT")
    assert not [l for l in forward if "192.168.5.2" in l or "--dport 8000" in l], forward


def test_firewall_opens_inference_proxy_on_bridge_only(tmp_path):
    _, restore, _ = _run_firewall(tmp_path)
    inbound = [l for l in restore if l.startswith("-A CLAUDE_AGENT_IN ")]
    assert "-A CLAUDE_AGENT_IN -d 172.17.0.1 -p tcp --dport 8000 -j RETURN" in inbound
    assert inbound[-1].startswith("-A CLAUDE_AGENT_IN -j REJECT")


_TINYPROXY_BLOCKED_RANGES = [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10",
]


def _out_rules(tmp_path):
    _, restore, _ = _run_firewall(tmp_path)
    assert ":CLAUDE_AGENT_OUT - [0:0]" in restore
    return [l for l in restore if l.startswith("-A CLAUDE_AGENT_OUT ")]


_OUT_REPLIES_PASS = "-A CLAUDE_AGENT_OUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"


def test_firewall_blocks_tinyproxy_from_host_and_private_ranges(tmp_path):
    reject = "-p tcp -m owner --uid-owner tinyproxy -j REJECT --reject-with icmp-admin-prohibited"
    assert _out_rules(tmp_path) == [
        _OUT_REPLIES_PASS,
        f"-A CLAUDE_AGENT_OUT -d 192.168.5.2 {reject}",
        *[f"-A CLAUDE_AGENT_OUT -d {r} {reject}" for r in _TINYPROXY_BLOCKED_RANGES],
    ]


def test_tinyproxy_replies_to_containers_pass_before_any_reject(tmp_path):
    # Container subnets (docker0 172.17/16, claude-agent-net 172.18/16) sit inside
    # 172.16.0.0/12, and tinyproxy's replies to them leave through OUTPUT as its
    # user. Rejecting those cut every container off from the Internet.
    assert _out_rules(tmp_path)[0] == _OUT_REPLIES_PASS


def test_tinyproxy_block_rules_limited_to_tinyproxy_and_tcp(tmp_path):
    rules = [r for r in _out_rules(tmp_path) if r != _OUT_REPLIES_PASS]
    assert len(rules) == 1 + len(_TINYPROXY_BLOCKED_RANGES)
    for rule in rules:
        assert "-p tcp" in rule, rule
        assert "-m owner --uid-owner tinyproxy" in rule, rule


def test_tinyproxy_block_does_not_cover_loopback(tmp_path):
    # DNS may fall back to TCP on 127.0.0.53; loopback reaches only VM services.
    for rule in _out_rules(tmp_path):
        assert "127." not in rule and "0.0.0.0/0" not in rule, rule


def test_tinyproxy_block_active_in_the_same_commit(tmp_path):
    # The jump rides in the restore, so the host carve-out and the tinyproxy
    # block change together. It must precede COMMIT to be part of that commit.
    _, restore, calls = _run_firewall(tmp_path)
    assert restore.count("-I OUTPUT 1 -j CLAUDE_AGENT_OUT") == 1
    assert restore.index("-I OUTPUT 1 -j CLAUDE_AGENT_OUT") < restore.index("COMMIT")
    assert not [c for c in calls if "-I OUTPUT" in c], "OUTPUT jump added outside the commit"


def test_tinyproxy_jump_not_duplicated_on_reapply(tmp_path):
    _, restore, _ = _run_firewall(tmp_path, output_jump_present=True)
    assert ":CLAUDE_AGENT_OUT - [0:0]" in restore
    assert not [l for l in restore if "-I OUTPUT" in l]


def test_output_jump_checked_before_restore_starts():
    # Inside the pipeline, `iptables -C` would race iptables-restore for the
    # xtables lock, and the restore fails outright if it loses.
    fw = _firewall_script()
    assert -1 < fw.find("iptables -C OUTPUT") < fw.find("echo '*filter'")


def test_firewall_apply_failure_is_fatal(tmp_path):
    proc, _, calls = _run_firewall(tmp_path, restore_rc=1)
    assert proc.returncode != 0
    assert not [c for c in calls if "-j CLAUDE_AGENT" in c and "-I" in c], (
        "jumps installed after a failed restore"
    )


def test_opencode_update_checks_disabled():
    # Accepting OpenCode's 2.0 upgrade prompt left no working binary: the new
    # package's postinstall is not covered by npm's allow-scripts.
    assert "data['autoupdate'] = False" in _SCRIPT_TEXT


def _restore6(tmp_path, **kwargs):
    proc, _, calls = _run_firewall(tmp_path, **kwargs)
    return proc, (tmp_path / "restore6.txt").read_text().splitlines(), calls


def test_firewall_refuses_tinyproxy_all_ipv6_but_loopback(tmp_path):
    proc, restore6, _ = _restore6(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert ":CLAUDE_AGENT_OUT6 - [0:0]" in restore6
    assert [l for l in restore6 if l.startswith("-A CLAUDE_AGENT_OUT6 ")] == [
        "-A CLAUDE_AGENT_OUT6 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN",
        "-A CLAUDE_AGENT_OUT6 -o lo -j RETURN",
        "-A CLAUDE_AGENT_OUT6 -p tcp -m owner --uid-owner tinyproxy -j REJECT --reject-with icmp6-adm-prohibited",
    ]


def test_ipv6_block_active_in_its_own_commit(tmp_path):
    _, restore6, calls = _restore6(tmp_path)
    jump = "-I OUTPUT 1 -j CLAUDE_AGENT_OUT6"
    assert restore6.count(jump) == 1
    assert restore6.index(jump) < restore6.index("COMMIT")
    assert not [c for c in calls if "ip6tables -I" in c], "OUTPUT jump added outside the commit"


def test_ipv6_jump_not_duplicated_on_reapply(tmp_path):
    _, restore6, _ = _restore6(tmp_path, output_jump_present=True)
    assert ":CLAUDE_AGENT_OUT6 - [0:0]" in restore6
    assert not [l for l in restore6 if "-I OUTPUT" in l]


def test_ipv6_jump_checked_before_restore_starts():
    # Same xtables lock race as test_output_jump_checked_before_restore_starts.
    fw = _firewall_script()
    assert -1 < fw.find("ip6tables -C OUTPUT") < fw.find("ip6tables-restore")


def test_ipv6_failure_is_fatal_with_ipv4_rules_already_applied(tmp_path):
    proc, restore, calls = _run_firewall(tmp_path, restore6_rc=1)
    assert proc.returncode != 0
    assert ":CLAUDE_AGENT_OUT - [0:0]" in restore
    assert [c for c in calls if c.startswith("iptables -C INPUT") and "CLAUDE_AGENT_IN" in c], (
        "IPv4 jumps must be handled before the IPv6 step can fail"
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


def test_firewall_smoke_covers_inference_proxy():
    smoke = (SCRIPT.parent / "tests" / "test-agent-firewall.sh").read_text()
    for fn in ("test_inference_allowed_routes", "test_inference_streaming", "test_proxy_refusals",
               "test_no_direct_host_path", "test_tinyproxy_refuses_host", "test_real_key_absent",
               "test_omlx_vision"):
        assert f"{fn}() {{" in smoke, f"{fn} not defined in test-agent-firewall.sh"
        assert re.search(rf"^{fn}$", smoke, re.MULTILINE), f"{fn} defined but never called"
    # Without it curl resolves ../ client-side and the traversal cases prove nothing.
    assert "--path-as-is" in smoke


def test_firewall_smoke_placeholder_matches_script():
    # The container cannot read start-agent.sh, so the smoke test has its own copy.
    smoke = (SCRIPT.parent / "tests" / "test-agent-firewall.sh").read_text()
    theirs = re.search(r'^INFERENCE_PLACEHOLDER_KEY="([^"]+)"$', smoke, re.MULTILINE)
    ours = re.search(r'^INFERENCE_PLACEHOLDER_KEY="([^"]+)"$', _SCRIPT_TEXT, re.MULTILINE)
    assert theirs and ours and theirs.group(1) == ours.group(1)


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


# ── inference proxy (nginx in the VM) ────────────────────────────────────────
# Generated config and VM push script are run for real. The traffic tests need
# an nginx binary (NGINX_BIN or PATH) and skip without one.

import http.server
import os
import shutil
import socket
import threading
import time

import pytest

_FAKE_KEY = "sk-test-KEY.123"
_OMLX_ROUTES = {
    ("POST", "/v1/chat/completions"), ("GET", "/v1/models"),
    ("POST", "/v1/messages"), ("POST", "/v1/messages/count_tokens"),
}
_OLLAMA_ROUTES = {
    ("POST", "/v1/chat/completions"), ("GET", "/v1/models"), ("GET", "/api/tags"),
}
_CATCH_ALL = "  location / { return 403; }"
_NGINX = os.environ.get("NGINX_BIN") or shutil.which("nginx")
needs_nginx = pytest.mark.skipif(not _NGINX, reason="no nginx binary (set NGINX_BIN)")
# `nginx -t` with the script's minimal wrapper uses the compiled-in paths,
# which only an installed package creates.
needs_installed_nginx = pytest.mark.skipif(
    not _NGINX or not (os.path.isdir("/var/lib/nginx") and os.path.isdir("/var/log/nginx")),
    reason="needs an installed nginx (/var/lib/nginx, /var/log/nginx)")


def _generate_conf(tmp_path, backend="omlx", key=_FAKE_KEY, bridge="172.17.0.1",
                   host="192.168.5.2", port="8000") -> str:
    fn = "generate_inference_proxy_conf() " + _function_body("generate_inference_proxy_conf")
    out = tmp_path / "inference.conf"
    env = {"PATH": "/usr/bin:/bin", "BACKEND": backend, "BRIDGE_IP": bridge,
           "HOST_IP": host, "INFERENCE_PORT": port}
    if key is not None:
        env["OMLX_API_KEY"] = key
    r = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n{fn}\ngenerate_inference_proxy_conf "$1"', "_", str(out)],
        env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return out.read_text()


def _conf_routes(conf: str) -> set:
    return {(m, p) for p, m in re.findall(
        r"location = (\S+) \{\s*if \(\$request_method != (\w+)\)", conf)}


@pytest.mark.parametrize("backend,routes", [("omlx", _OMLX_ROUTES), ("ollama", _OLLAMA_ROUTES)])
def test_proxy_allowlist_is_exact(tmp_path, backend, routes):
    conf = _generate_conf(tmp_path, backend=backend)
    assert _conf_routes(conf) == routes
    # Every location is an exact match, apart from the catch-all.
    locations = re.findall(r"location\s+(.*?)\s*\{", conf)
    assert len(locations) == len(routes) + 1
    assert sorted(l for l in locations if not l.startswith("= ")) == ["/"]
    assert _CATCH_ALL in conf.splitlines()


def test_proxy_listens_on_bridge_only(tmp_path):
    conf = _generate_conf(tmp_path)
    assert re.findall(r"^\s*listen\s+(\S+)", conf, re.M) == ["172.17.0.1:8000"]


@pytest.mark.parametrize("backend,key,expected", [
    ("omlx", _FAKE_KEY, f'proxy_set_header Authorization "Bearer {_FAKE_KEY}";'),
    ("omlx", None, 'proxy_set_header Authorization "";'),
    ("omlx", "", 'proxy_set_header Authorization "";'),
    ("ollama", _FAKE_KEY, 'proxy_set_header Authorization "";'),
])
def test_proxy_credentials(tmp_path, backend, key, expected):
    conf = _generate_conf(tmp_path, backend=backend, key=key)
    assert expected in conf
    assert 'proxy_set_header x-api-key "";' in conf
    if backend == "ollama":
        assert _FAKE_KEY not in conf, "Ollama config must never carry the omlx key"


@pytest.mark.parametrize("key", [
    'a"b', "a'b", "a$b", "a b", "a\tb", "a\nb", "a\\b", "a;b",
])
def test_unsafe_omlx_key_refused_without_echoing_it(key):
    fn = "check_omlx_key_safe() " + _function_body("check_omlx_key_safe")
    r = subprocess.run(["bash", "-c", f"{fn}\ncheck_omlx_key_safe"],
                       env={"PATH": "/usr/bin:/bin", "OMLX_API_KEY": key},
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "OMLX_API_KEY" in r.stderr
    assert key not in r.stdout + r.stderr


@pytest.mark.parametrize("key", ["", _FAKE_KEY, "abc-DEF_123.~+/=:"])
def test_safe_omlx_key_accepted(key):
    fn = "check_omlx_key_safe() " + _function_body("check_omlx_key_safe")
    r = subprocess.run(["bash", "-c", f"{fn}\ncheck_omlx_key_safe"],
                       env={"PATH": "/usr/bin:/bin", "OMLX_API_KEY": key},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_omlx_key_checked_before_vm_work():
    check = _SCRIPT_TEXT.find("\n  check_omlx_key_safe\n")
    assert check != -1, "check_omlx_key_safe is not called"
    assert check < _SCRIPT_TEXT.find("vm-probe.sh"), "bad key must be refused before VM work"


def _render_proxy_push(tmp_path, active="true", conf_text=None, path="/usr/bin:/bin",
                       keep="false"):
    conf = tmp_path / "inference-proxy.conf"
    conf.write_text(_generate_conf(tmp_path) if conf_text is None else conf_text)
    fn = "write_inference_proxy_push() " + _function_body("write_inference_proxy_push")
    r = subprocess.run(
        ["bash", "-c", f"set -eo pipefail\n{fn}\nwrite_inference_proxy_push"],
        env={"PATH": path, "TMP_WORK": str(tmp_path),
             "NEW_INFERENCE_PROXY_HASH": "newhash", "NGINX_ACTIVE": active,
             "KEEP_PROXY_KEY": keep,
             "BRIDGE_IP": "172.17.0.1", "INFERENCE_PORT": "8000"},
        capture_output=True, text=True,
    )
    out = tmp_path / "inference-proxy-push.sh"
    return r.returncode, r.stderr, (out.read_text() if out.exists() else None)


def _run_proxy_push(tmp_path, script, nginx_rc=0, real_nginx=False, expect_ok=True,
                    restart_rc=0, listen="always"):
    """Run the VM script against a fake /etc/nginx; return (systemctl calls, nginx calls).

    listen: when the stubbed `ss` shows the proxy's listener -- "always", "never",
    or "after-restart" (only once systemctl has been asked to restart nginx).
    """
    etc = tmp_path / "etc-nginx"
    (etc / "conf.d").mkdir(parents=True, exist_ok=True)
    (etc / "sites-enabled").mkdir(exist_ok=True)
    (etc / "sites-enabled" / "default").write_text("default site\n")
    sysl, ngl = tmp_path / "systemctl.log", tmp_path / "nginx.log"
    stub = f'systemctl() {{ echo "$*" >> "{sysl}"; return {restart_rc}; }}\n'
    stub += "sleep() { :; }\n"
    line = 'echo "LISTEN 0 511 172.17.0.1:8000 0.0.0.0:*"'
    show = {"always": line, "never": ":",
            "after-restart": f'grep -q "^restart" "{sysl}" 2>/dev/null && {line} || :'}[listen]
    stub += f'ss() {{ echo "State Recv-Q Send-Q Local:Port Peer:Port"; {show}; }}\n'
    if real_nginx:
        stub += f'nginx() {{ "{_NGINX}" "$@"; }}\n'
    else:
        stub += f'nginx() {{ echo "$*" >> "{ngl}"; return {nginx_rc}; }}\n'
    r = subprocess.run(["sh"], input=stub + script.replace("/etc/nginx/", f"{etc}/"),
                       text=True, capture_output=True)
    assert (r.returncode == 0) == expect_ok, r.stderr
    (tmp_path / "push.out").write_text(r.stdout + r.stderr)
    read = lambda p: p.read_text().splitlines() if p.exists() else []
    return read(sysl), read(ngl)


def test_proxy_push_installs_root_only(tmp_path):
    rc, err, script = _render_proxy_push(tmp_path)
    assert rc == 0, err
    calls, ngx = _run_proxy_push(tmp_path, script)
    etc = tmp_path / "etc-nginx"
    conf = etc / "conf.d" / "claude-agent-inference.conf"
    hash_file = etc / "claude-agent-inference.hash"
    assert conf.read_bytes() == (tmp_path / "inference-proxy.conf").read_bytes()
    assert hash_file.read_text() == "newhash\n"
    assert conf.stat().st_mode & 0o777 == 0o600
    assert hash_file.stat().st_mode & 0o777 == 0o600
    assert not (etc / "sites-enabled" / "default").exists(), "default site must be removed"
    assert any(c.startswith("-t") or " -t" in c for c in ngx), "nginx -t must run"
    assert calls == ["reload nginx"], calls
    assert sorted(p.name for p in etc.iterdir()) == [
        "claude-agent-inference.hash", "conf.d", "sites-enabled",
    ], "no scratch files may be left behind"
    assert [p.name for p in (etc / "conf.d").iterdir()] == ["claude-agent-inference.conf"]


def test_proxy_push_restarts_when_inactive(tmp_path):
    # restart, not start: the package's postinst has already started nginx.
    rc, err, script = _render_proxy_push(tmp_path, active="false")
    assert rc == 0, err
    calls, _ = _run_proxy_push(tmp_path, script)
    assert calls == ["enable nginx", "restart nginx"], calls


def _seed_old_proxy_state(tmp_path):
    etc = tmp_path / "etc-nginx"
    (etc / "conf.d").mkdir(parents=True)
    (etc / "conf.d" / "claude-agent-inference.conf").write_text("previous\n")
    (etc / "claude-agent-inference.hash").write_text("old\n")
    return etc


def _assert_old_proxy_state_kept(etc):
    assert (etc / "conf.d" / "claude-agent-inference.conf").read_text() == "previous\n"
    assert (etc / "claude-agent-inference.hash").read_text() == "old\n", (
        "hash must not advance, so the next run pushes again"
    )
    assert [p.name for p in (etc / "conf.d").iterdir()] == ["claude-agent-inference.conf"]


def test_vm_refuses_proxy_conf_without_catch_all(tmp_path):
    etc = _seed_old_proxy_state(tmp_path)
    rc, err, script = _render_proxy_push(tmp_path, conf_text="server { listen 1.2.3.4:1; }\n")
    assert rc == 0, err
    calls, ngx = _run_proxy_push(tmp_path, script, expect_ok=False)
    assert calls == [] and ngx == [], "must stop before testing or loading the config"
    _assert_old_proxy_state_kept(etc)


def test_vm_refuses_proxy_conf_failing_nginx_t(tmp_path):
    etc = _seed_old_proxy_state(tmp_path)
    rc, err, script = _render_proxy_push(tmp_path)
    assert rc == 0, err
    calls, _ = _run_proxy_push(tmp_path, script, nginx_rc=1, expect_ok=False)
    assert calls == [], "nginx must not be reloaded with a config that fails -t"
    _assert_old_proxy_state_kept(etc)


def test_proxy_hash_not_advanced_when_load_fails(tmp_path):
    # Otherwise the next run would skip the push and never retry the load.
    etc = _seed_old_proxy_state(tmp_path)
    rc, err, script = _render_proxy_push(tmp_path, active="false")
    assert rc == 0, err
    _run_proxy_push(tmp_path, script, restart_rc=1, expect_ok=False)
    assert (etc / "claude-agent-inference.hash").read_text() == "old\n"


def test_proxy_push_restarts_when_reload_leaves_no_listener(tmp_path):
    # reload exits 0 even when nginx keeps the old config, e.g. it cannot bind.
    rc, err, script = _render_proxy_push(tmp_path)
    assert rc == 0, err
    calls, _ = _run_proxy_push(tmp_path, script, listen="after-restart")
    assert calls == ["reload nginx", "restart nginx"], calls
    assert (tmp_path / "etc-nginx" / "claude-agent-inference.hash").read_text() == "newhash\n"


def test_proxy_hash_not_advanced_without_listener(tmp_path):
    # Otherwise warm reattaches skip the push and the new address is never served.
    etc = _seed_old_proxy_state(tmp_path)
    rc, err, script = _render_proxy_push(tmp_path)
    assert rc == 0, err
    calls, _ = _run_proxy_push(tmp_path, script, listen="never", expect_ok=False)
    assert calls == ["reload nginx", "restart nginx"], calls
    assert (etc / "claude-agent-inference.hash").read_text() == "old\n"


def test_proxy_push_aborts_on_empty_conf(tmp_path):
    rc, err, _ = _render_proxy_push(tmp_path, conf_text="")
    assert rc != 0
    assert "inference proxy" in err


def test_proxy_push_aborts_when_encoding_fails(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "base64").write_text("#!/bin/sh\nexit 1\n")
    (stubs / "base64").chmod(0o755)
    rc, err, _ = _render_proxy_push(tmp_path, path=f"{stubs}:/usr/bin:/bin")
    assert rc != 0
    assert "inference proxy" in err


def test_proxy_probe_fields():
    start = _SCRIPT_TEXT.find("vm-probe.sh\" <<PROBE_EOF")
    probe = _SCRIPT_TEXT[start:_SCRIPT_TEXT.find("\nPROBE_EOF", start)]
    for field in ("NGINX_INSTALLED", "NGINX_ACTIVE", "NGINX_STORED_HASH"):
        assert f"{field}=" in probe, f"{field} missing from the batch probe"
        assert re.search(rf"^{field}=\$\(\s*_probe_val {field}\)", _SCRIPT_TEXT, re.M)
    assert "sudo cat /etc/nginx/claude-agent-inference.hash" in probe


def _proxy_push_block() -> str:
    start = _SCRIPT_TEXT.find('if [[ "$NEW_INFERENCE_PROXY_HASH" != "$NGINX_STORED_HASH"')
    assert start != -1, "hash-gated inference proxy push not found"
    return _SCRIPT_TEXT[start:_SCRIPT_TEXT.find("\nfi\n", start)]


def test_proxy_push_gated_and_fails_closed():
    block = _proxy_push_block()
    assert '"$NGINX_ACTIVE" != "true"' in block
    assert "write_inference_proxy_push || exit 1" in block
    assert re.search(r'inference-proxy-push\.sh"\s*\|\|', block) or \
        re.search(r'inference-proxy-push\.sh" *; then', block), "failed push must abort"
    runs = [l for l in _SCRIPT_TEXT.splitlines()
            if "colima ssh" in l and "inference-proxy-push.sh" in l]
    assert len(runs) == 1 and runs[0] in block


def test_proxy_pushed_before_firewall_on_every_path():
    push = _SCRIPT_TEXT.find('if [[ "$NEW_INFERENCE_PROXY_HASH" != "$NGINX_STORED_HASH"')
    firewall = _SCRIPT_TEXT.find('cat > "$TMP_WORK/firewall-apply.sh"')
    assert -1 < push < firewall
    # No --reload-allowlist exit may sit between the probe and the push.
    assert "exit 0" not in _SCRIPT_TEXT[_SCRIPT_TEXT.find("vm-probe.sh"):push]


def test_nginx_install_follows_probe():
    block = re.search(r'if \[\[ "\$NGINX_INSTALLED" != "true" \]\]; then\n(.*?)\nfi\n',
                      _SCRIPT_TEXT, re.S)
    assert block, "nginx install is not gated on the probe"
    assert "--force-confnew nginx" in block.group(1).replace("\\\n", " ").replace("    ", "")


# Keeping the omlx key and the backend across runs ---------------------------
# A run from a shell without OMLX_API_KEY, or one that names no backend, must
# not break a live omlx session by rewriting the proxy.

def _run_fn(name, call):
    fn = f"{name}() " + _function_body(name)
    return subprocess.run(["bash", "-c", f"set -u\n{fn}\n{call}"],
                          env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)


@pytest.mark.parametrize("requested,proxy_port,expected", [
    ("omlx", "", "omlx"),
    ("omlx", "11434", "omlx"),     # a named backend always wins
    ("ollama", "8000", "ollama"),
    ("", "8000", "omlx"),          # none named: keep what the proxy serves
    ("", "11434", "ollama"),
    ("", "", "ollama"),            # no proxy yet: the default
    ("", "1234", "ollama"),
])
def test_backend_named_else_kept_from_proxy(requested, proxy_port, expected):
    r = _run_fn("resolve_backend", f'resolve_backend "{requested}" "{proxy_port}"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected


@pytest.mark.parametrize("backend,key_set,proxy_port,has_key,keep", [
    ("omlx", "", "8000", "true", True),      # keyless run: keep the key
    ("omlx", "set", "8000", "true", False),  # a key given: it replaces (rotation)
    ("omlx", "", "8000", "false", False),    # nothing to keep
    ("omlx", "", "11434", "false", False),   # switching from Ollama
    ("omlx", "", "", "false", False),        # fresh VM
    ("ollama", "", "8000", "true", False),   # explicit switch away from omlx
])
def test_keep_proxy_key_decision(backend, key_set, proxy_port, has_key, keep):
    r = _run_fn("keep_proxy_key", f'keep_proxy_key {backend} "{key_set}" "{proxy_port}" {has_key}')
    assert (r.returncode == 0) == keep, r.stderr


def test_keep_proxy_key_never_handed_the_key():
    calls = re.findall(r"keep_proxy_key [^\n]*", _SCRIPT_TEXT.split("keep_proxy_key() {", 1)[1])
    assert calls, "keep_proxy_key is never called"
    for call in calls:
        assert "OMLX_API_KEY}" not in call.replace("${OMLX_API_KEY:+set}", "") and \
            "$OMLX_API_KEY" not in call, call


def _run_probe(tmp_path, conf_text):
    start = _SCRIPT_TEXT.find('cat > "$TMP_WORK/vm-probe.sh" <<PROBE_EOF')
    end = _SCRIPT_TEXT.find("\nPROBE_EOF\n", start) + len("\nPROBE_EOF\n")
    r = subprocess.run(["bash", "-c", _SCRIPT_TEXT[start:end]],
                       env={"PATH": "/usr/bin:/bin", "TMP_WORK": str(tmp_path),
                            "AGENT_NET_NAME": "claude-agent-net"},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    script = (tmp_path / "vm-probe.sh").read_text()
    etc = tmp_path / "etc-nginx"
    (etc / "conf.d").mkdir(parents=True)
    if conf_text is not None:
        (etc / "conf.d" / "claude-agent-inference.conf").write_text(conf_text)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in {"sudo": 'exec "$@"', "docker": "exit 1", "dpkg-query": "exit 1",
                       "systemctl": "exit 1", "ip": "exit 0", "getent": "exit 1"}.items():
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    r = subprocess.run(["sh"], input=script.replace("/etc/nginx/", f"{etc}/"),
                       env={"PATH": f"{stubs}:/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert _FAKE_KEY not in r.stdout + r.stderr, "the probe must never print the key"
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


@pytest.mark.parametrize("backend,key,port,has_key", [
    ("omlx", _FAKE_KEY, "8000", "true"),
    ("omlx", None, "8000", "false"),
    ("ollama", None, "11434", "false"),
    (None, None, "", "false"),               # no proxy installed yet
])
def test_probe_reports_proxy_backend_and_key_presence(tmp_path, backend, key, port, has_key):
    conf = None
    if backend:
        conf = _generate_conf(tmp_path, backend=backend, key=key,
                              port="8000" if backend == "omlx" else "11434")
    facts = _run_probe(tmp_path, conf)
    assert facts["NGINX_PORT"] == port
    assert facts["NGINX_HAS_KEY"] == has_key


def test_probe_fields_parsed():
    for field in ("NGINX_PORT", "NGINX_HAS_KEY"):
        assert re.search(rf"^{field}=\$\(\s*_probe_val {field}\)", _SCRIPT_TEXT, re.M), field


def _with_key(conf: str) -> str:
    return conf.replace('Authorization "";', f'Authorization "Bearer {_FAKE_KEY}";')


def _install_old_conf(tmp_path, conf: str):
    etc = tmp_path / "etc-nginx"
    (etc / "conf.d").mkdir(parents=True)
    (etc / "conf.d" / "claude-agent-inference.conf").write_text(conf)
    return etc / "conf.d" / "claude-agent-inference.conf"


def test_proxy_push_keeps_installed_key(tmp_path):
    # The rest of the config still updates: here the upstream host moved.
    installed = _install_old_conf(tmp_path, _generate_conf(tmp_path, host="192.168.5.9"))
    new = _generate_conf(tmp_path, key=None)
    rc, err, script = _render_proxy_push(tmp_path, conf_text=new, keep="true")
    assert rc == 0, err
    assert _FAKE_KEY not in script, "the key must never leave the VM"
    calls, _ = _run_proxy_push(tmp_path, script)
    assert installed.read_text() == _with_key(new)
    assert installed.stat().st_mode & 0o777 == 0o600
    assert calls == ["reload nginx"], calls
    assert [p.name for p in installed.parent.iterdir()] == ["claude-agent-inference.conf"]
    assert _FAKE_KEY not in (tmp_path / "push.out").read_text()


def test_proxy_push_key_carry_stays_off_argv(tmp_path):
    rc, err, script = _render_proxy_push(tmp_path, conf_text=_generate_conf(tmp_path, key=None),
                                         keep="true")
    assert rc == 0, err
    carry = script[script.find("KEEP"):]
    assert "Bearer" in carry, "carry-forward block not found"
    # The key is read from the installed file by awk, never expanded into a command.
    assert not re.search(r"\$\{?(key|auth|old_auth)\b", carry), carry


def test_proxy_push_without_keep_replaces_key(tmp_path):
    installed = _install_old_conf(tmp_path, _generate_conf(tmp_path))
    new = _generate_conf(tmp_path, key=None)
    rc, err, script = _render_proxy_push(tmp_path, conf_text=new, keep="false")
    assert rc == 0, err
    _run_proxy_push(tmp_path, script)
    assert installed.read_text() == new


def test_proxy_push_keep_with_no_installed_key_is_harmless(tmp_path):
    installed = _install_old_conf(tmp_path, _generate_conf(tmp_path, key=None))
    new = _generate_conf(tmp_path, key=None, host="192.168.5.9")
    rc, err, script = _render_proxy_push(tmp_path, conf_text=new, keep="true")
    assert rc == 0, err
    _run_proxy_push(tmp_path, script)
    assert installed.read_text() == new


def test_backend_resolved_after_probe_before_use():
    probe_parse = _SCRIPT_TEXT.find("NGINX_PORT=$(")
    resolve = _SCRIPT_TEXT.find('BACKEND=$(resolve_backend "$REQUESTED_BACKEND" "$NGINX_PORT")')
    assert -1 < probe_parse < resolve, "backend must be resolved from the probe's facts"
    head = _SCRIPT_TEXT[:resolve]
    body = head[head.find("# ── constants"):]
    assert not re.search(r"\$\{?(BACKEND|INFERENCE_PORT|INFERENCE_LABEL)\b", body), (
        "BACKEND or its port/label is used before it is resolved")
    assert resolve < _SCRIPT_TEXT.find('echo "==> Probing $INFERENCE_LABEL')
    assert resolve < _SCRIPT_TEXT.find('generate_inference_proxy_conf "$TMP_WORK/')


def test_named_backend_still_validated_before_vm_work():
    check = _SCRIPT_TEXT.find("unknown --backend value")
    assert -1 < check < _SCRIPT_TEXT.find("vm-probe.sh")


def test_keep_decision_feeds_the_push():
    keep = _SCRIPT_TEXT.find("KEEP_PROXY_KEY=")
    assert -1 < keep < _SCRIPT_TEXT.find("\n  write_inference_proxy_push || exit 1")
    assert "KEEP_PROXY_KEY" in _function_body("write_inference_proxy_push")


def test_missing_key_warning_knows_about_the_kept_key():
    resolve = _SCRIPT_TEXT.find('BACKEND=$(resolve_backend')
    warn = _SCRIPT_TEXT.find("OMLX_API_KEY not set")
    assert -1 < resolve < warn, "the missing-key warning must follow the probe"
    assert "keeping the key the inference proxy already holds" in _SCRIPT_TEXT


def test_usage_documents_sticky_backend():
    usage = _SCRIPT_TEXT[_SCRIPT_TEXT.find("--backend=BACKEND"):]
    usage = usage[:usage.find("--git-name")]
    assert "already serves" in usage, usage


# Traffic through a real nginx ------------------------------------------------

class _Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []
    release = threading.Event()

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        remaining = n
        while remaining:
            remaining -= len(self.rfile.read(min(remaining, 65536)))
        _Upstream.seen.append((self.command, self.path, dict(self.headers.items()), n))
        if self.path.startswith("/v1/chat/completions?stream"):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"6\r\nfirst\n\r\n")
            self.wfile.flush()
            _Upstream.release.wait(10)
            self.wfile.write(b"7\r\nsecond\n\r\n0\r\n\r\n")
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"ok")

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = _handle

    def log_message(self, *a):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module", params=["omlx", "ollama"])
def _live_proxy_server(request, tmp_path_factory):
    if not _NGINX:
        pytest.skip("no nginx binary (set NGINX_BIN)")
    tmp_path = tmp_path_factory.mktemp(f"nginx-{request.param}")
    port = _free_port()
    upstream = http.server.ThreadingHTTPServer(("127.0.0.2", port), _Upstream)
    threading.Thread(target=upstream.serve_forever, args=(0.01,), daemon=True).start()
    conf = _generate_conf(tmp_path, backend=request.param, bridge="127.0.0.1",
                          host="127.0.0.2", port=str(port))
    (tmp_path / "server.conf").write_text(conf)
    main = tmp_path / "main.conf"
    temp_dirs = "".join(
        f"  {d}_temp_path {tmp_path}/{d};\n"
        for d in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
    main.write_text(
        f"daemon off;\nuser root;\npid {tmp_path}/nginx.pid;\nerror_log {tmp_path}/err.log;\n"
        f"events {{}}\nhttp {{\n  access_log off;\n{temp_dirs}"
        f"  include {tmp_path}/server.conf;\n}}\n")
    proc = subprocess.Popen([_NGINX, "-c", str(main), "-p", str(tmp_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("nginx did not start: " + (tmp_path / "err.log").read_text())
    yield request.param, port
    proc.terminate()
    proc.wait(5)
    upstream.shutdown()


@pytest.fixture
def live_proxy(_live_proxy_server):
    _Upstream.seen = []
    _Upstream.release = threading.Event()
    yield _live_proxy_server
    _Upstream.release.set()


def _raw(port, method, target, headers=(), body=b"") -> int:
    """Send an unnormalized request; return the status code."""
    lines = [f"{method} {target} HTTP/1.1", "Host: anything", "Connection: close",
             *headers, f"Content-Length: {len(body)}", "", ""]
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall("\r\n".join(lines).encode() + body)
        data = b""
        while b"\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return int(data.split(b" ", 2)[1])


_DENIED_TARGETS = [
    ("POST", "/v1/web/search"), ("POST", "/v1/web/fetch"), ("POST", "/v1/mcp/execute"),
    ("GET", "/admin/api/global-settings"), ("POST", "/v1/responses"),
    ("POST", "/v1/models/x/load"), ("GET", "/v1/models/status"), ("POST", "/api/pull"),
    ("POST", "/api/push"), ("GET", "/v1/realtime"), ("GET", "/"),
    ("GET", "/v1/models/"), ("POST", "/v1/messages/"), ("POST", "/v1/messages/x"),
    # traversal and encoded forms of denied routes
    ("POST", "/v1/chat/completions/../../web/search"),
    ("GET", "/v1/models/../../admin/api/global-settings"),
    ("POST", "/v1/web%2Fsearch"), ("POST", "/v1/%77eb/search"),
    ("POST", "/v1/models%2Fx%2Fload"), ("GET", "/%61dmin/api/global-settings"),
    ("POST", "/v1/models/x/..%2F..%2Fweb/search"),
    # wrong method on an allowed path
    ("GET", "/v1/chat/completions"), ("POST", "/v1/models"), ("HEAD", "/v1/models"),
    ("DELETE", "/v1/models"), ("PUT", "/v1/chat/completions"),
    ("OPTIONS", "/v1/chat/completions"),
]


@pytest.mark.parametrize("method,target", _DENIED_TARGETS)
def test_live_denied_routes_never_reach_upstream(live_proxy, method, target):
    _, port = live_proxy
    assert _raw(port, method, target) in (403, 405)
    assert _Upstream.seen == []


def test_live_allowed_routes_get_injected_credentials(live_proxy):
    backend, port = live_proxy
    routes = _OMLX_ROUTES if backend == "omlx" else _OLLAMA_ROUTES
    for method, path in sorted(routes):
        _Upstream.seen = []
        status = _raw(port, method, path, headers=(
            "Authorization: Bearer agent-guess", "x-api-key: agent-guess",
            "X-API-KEY: agent-guess", "Upgrade: websocket",
        ))
        assert status == 200, (method, path)
        [(m, p, hdrs, _)] = _Upstream.seen
        lower = {k.lower(): v for k, v in hdrs.items()}
        assert (m, p) == (method, path)
        assert "x-api-key" not in lower
        assert "upgrade" not in lower
        if backend == "omlx":
            assert lower["authorization"] == f"Bearer {_FAKE_KEY}"
        else:
            assert "authorization" not in lower


def test_live_unauthenticated_request_gets_key(live_proxy):
    backend, port = live_proxy
    assert _raw(port, "GET", "/v1/models") == 200
    lower = {k.lower(): v for k, v in _Upstream.seen[0][2].items()}
    assert lower.get("authorization") == (f"Bearer {_FAKE_KEY}" if backend == "omlx" else None)


@pytest.mark.parametrize("target", [
    "/admin/../v1/models", "/v1/web/../models", "//v1//models", "/v1%2Fmodels",
])
def test_live_forwards_fixed_path_not_raw_uri(live_proxy, target):
    # These normalize to an allowed route, so they pass, but the upstream must
    # see the fixed path: a raw forward would reach whatever the variant names.
    _, port = live_proxy
    assert _raw(port, "GET", target) == 200
    assert _Upstream.seen[0][1] == "/v1/models"


def test_live_query_string_preserved(live_proxy):
    _, port = live_proxy
    assert _raw(port, "POST", "/v1/chat/completions?beta=true&x=a%2Fb") == 200
    assert _Upstream.seen[0][1] == "/v1/chat/completions?beta=true&x=a%2Fb"


def test_live_large_body_not_capped(live_proxy):
    _, port = live_proxy
    body = b"x" * (5 * 1024 * 1024)
    assert _raw(port, "POST", "/v1/chat/completions", body=body) == 200
    assert _Upstream.seen[0][3] == len(body)


def test_live_streaming_is_not_buffered(live_proxy):
    _, port = live_proxy
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"POST /v1/chat/completions?stream=1 HTTP/1.1\r\nHost: x\r\n"
                  b"Connection: close\r\nContent-Length: 0\r\n\r\n")
        data = b""
        # The upstream holds "second" until released, so seeing "first" here
        # proves nginx forwarded it without waiting for the response to end.
        while b"first" not in data:
            chunk = s.recv(4096)
            assert chunk, "connection closed before the first chunk"
            data += chunk
        assert b"second" not in data
        _Upstream.release.set()
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    assert b"second" in data


@needs_installed_nginx
def test_real_nginx_guard_accepts_generated_conf(tmp_path):
    # The script's own -t wrapper, with a real nginx and a bindable address.
    conf = _generate_conf(tmp_path, bridge="127.0.0.1", host="127.0.0.2", port=str(_free_port()))
    rc, err, script = _render_proxy_push(tmp_path, conf_text=conf)
    assert rc == 0, err
    calls, _ = _run_proxy_push(tmp_path, script, real_nginx=True)
    assert calls == ["reload nginx"]


@needs_nginx
def test_real_nginx_guard_refuses_broken_conf(tmp_path):
    etc = _seed_old_proxy_state(tmp_path)
    broken = f"server {{\n{_CATCH_ALL}\n  not_a_directive on;\n}}\n"
    rc, err, script = _render_proxy_push(tmp_path, conf_text=broken)
    assert rc == 0, err
    calls, _ = _run_proxy_push(tmp_path, script, real_nginx=True, expect_ok=False)
    assert calls == []
    _assert_old_proxy_state_kept(etc)


@needs_installed_nginx
def test_real_nginx_guard_tolerates_running_listener(tmp_path):
    # Warm re-push: nginx already holds the address, and -t must still pass.
    with socket.socket() as held:
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", 0))
        held.listen()
        conf = _generate_conf(tmp_path, bridge="127.0.0.1", host="127.0.0.2",
                              port=str(held.getsockname()[1]))
        rc, err, script = _render_proxy_push(tmp_path, conf_text=conf)
        assert rc == 0, err
        calls, _ = _run_proxy_push(tmp_path, script, real_nginx=True)
    assert calls == ["reload nginx"]


# ── container and client config (real key stays out of the container) ────────
# The env block and both injectors are cut out of the script and run for real
# against fixture files, so the argv wiring is exercised as well as the Python.

import json

_BRIDGE = "172.17.0.1"
_STALE_URL = "http://192.168.5.2:8000/v1"
_PORTS = {"omlx": "8000", "ollama": "11434"}


def _placeholder() -> str:
    m = re.search(r'^INFERENCE_PLACEHOLDER_KEY="([^"]+)"$', _SCRIPT_TEXT, re.M)
    assert m, "INFERENCE_PLACEHOLDER_KEY constant not found"
    return m.group(1)


def _docker_env_args(backend="omlx", key=_FAKE_KEY, host="192.168.5.2", extra=None) -> dict:
    """Run the DOCKER_ENV_ARGS block; return the -e values by name (None if value-less)."""
    start = _SCRIPT_TEXT.index("DOCKER_ENV_ARGS=(")
    end = _SCRIPT_TEXT.index("# ── inject project settings", start)
    env = {"PATH": "/usr/bin:/bin", "BACKEND": backend, "BRIDGE_IP": _BRIDGE, "HOST_IP": host,
           "INFERENCE_PORT": _PORTS[backend], "TINYPROXY_PORT": "8888",
           "GIT_USER_NAME": "n", "GIT_USER_EMAIL": "e", **(extra or {})}
    if key is not None:
        env["OMLX_API_KEY"] = key
    src = "set -euo pipefail\n" + _SCRIPT_TEXT[start:end] + '\nprintf "%s\\n" "${DOCKER_ENV_ARGS[@]}"\n'
    r = subprocess.run(["bash", "-c", src], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    args = r.stdout.splitlines()
    assert all(flag == "-e" for flag in args[::2]), args
    return {name: (value[0] if value else None)
            for name, *value in (a.split("=", 1) for a in args[1::2])}


def _run_injectors(tmp_path, backend="omlx", key=_FAKE_KEY, opencode=None, pi=None, port=None,
                   host="127.0.0.1", path=None):
    """Run both injectors. Port defaults to a closed one, so discovery finds nothing."""
    opencode_dir = tmp_path / "opencode"
    pi_dir = tmp_path / "pi"
    (pi_dir / "agent").mkdir(parents=True, exist_ok=True)
    opencode_dir.mkdir(exist_ok=True)
    opencode_file = opencode_dir / "opencode.json"
    models_file = pi_dir / "agent" / "models.json"
    if opencode is not None:
        opencode_file.write_text(opencode if isinstance(opencode, str) else json.dumps(opencode))
    if pi is not None:
        models_file.write_text(pi if isinstance(pi, str) else json.dumps(pi))
    start = _SCRIPT_TEXT.index("# ── inject OpenCode config")
    end = _SCRIPT_TEXT.index("# ── agent skills seed", start)
    port = port or _free_port()
    env = {"PATH": path or f"{os.path.dirname(sys.executable)}:/usr/bin:/bin",
           "BACKEND": backend, "BRIDGE_IP": _BRIDGE, "HOST_IP": host,
           "INFERENCE_PORT": str(port), "OPENCODE_CONFIG_DIR": str(opencode_dir),
           "PI_CONFIG_DIR": str(pi_dir), "LOCAL_SEARCH_ENABLED": "false"}
    if key is not None:
        env["OMLX_API_KEY"] = key
    r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + _SCRIPT_TEXT[start:end]],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert _FAKE_KEY not in r.stdout + r.stderr, "injector printed the real key"
    return (json.loads(opencode_file.read_text()), json.loads(models_file.read_text()),
            opencode_file.read_text() + models_file.read_text(), f"http://{_BRIDGE}:{port}/v1")


def _stale_entry(url=_STALE_URL, key=_FAKE_KEY):
    return {"npm": "@ai-sdk/openai-compatible", "name": "old", "models": {"m1": {"name": "m1"}},
            "options": {"baseURL": url, "apiKey": key}}


def test_omlx_key_not_in_container_env():
    args = _docker_env_args()
    assert "OMLX_API_KEY" not in args
    assert _FAKE_KEY not in json.dumps(args)
    assert "OMLX_API_KEY" not in _fresh_container_section()


@pytest.mark.parametrize("backend,var", [("omlx", "OMLX_HOST"), ("ollama", "OLLAMA_HOST")])
def test_inference_env_points_at_proxy(backend, var):
    args = _docker_env_args(backend)
    assert args[var] == f"http://{_BRIDGE}:{_PORTS[backend]}"


@pytest.mark.parametrize("var", ["NO_PROXY", "no_proxy"])
def test_host_ip_gone_from_no_proxy(var):
    entries = _docker_env_args()[var].split(",")
    assert "192.168.5.2" not in entries
    assert _BRIDGE in entries, "the proxy must be reached directly, not through tinyproxy"


def test_host_ip_appears_nowhere_in_container_env():
    assert "192.168.5.2" not in json.dumps(_docker_env_args())


def test_cloud_keys_still_forwarded_value_less():
    args = _docker_env_args(extra={"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "b"})
    assert args["ANTHROPIC_API_KEY"] is None and args["OPENAI_API_KEY"] is None


def test_host_gateway_mapping_kept():
    assert "--add-host=host.docker.internal:host-gateway" in _SCRIPT_TEXT


def test_bridge_ip_known_before_every_consumer():
    # The injectors and the env block both run before attach_existing, on the
    # fresh and reattach paths alike, so the probe must come first on both.
    assigned = _SCRIPT_TEXT.index("BRIDGE_IP=$(")
    fallback = _SCRIPT_TEXT.index('BRIDGE_IP="172.17.0.1"')
    consumers = [_SCRIPT_TEXT.index("# ── inject OpenCode config"),
                 _SCRIPT_TEXT.index("DOCKER_ENV_ARGS=("),
                 _SCRIPT_TEXT.index('if docker container inspect "$CONTAINER_NAME"')]
    assert assigned < fallback < min(consumers)


def test_startup_summary_shows_proxy_address():
    line = next(l for l in _SCRIPT_TEXT.splitlines() if l.startswith('echo "    inference:'))
    assert "$BRIDGE_IP:$INFERENCE_PORT" in line and "$HOST_IP" not in line


@pytest.mark.parametrize("backend", ["omlx", "ollama"])
def test_opencode_scrubs_active_and_inactive_entries(tmp_path, backend):
    seeded = {"provider": {"omlx": _stale_entry(), "ollama": _stale_entry()}}
    data, _, raw, url = _run_injectors(tmp_path, backend=backend, opencode=seeded)
    assert _FAKE_KEY not in raw
    for name in ("omlx", "ollama"):
        opts = data["provider"][name]["options"]
        assert opts == {"baseURL": url, "apiKey": _placeholder()}, name
    assert "192.168.5.2" not in raw


@pytest.mark.parametrize("backend", ["omlx", "ollama"])
def test_pi_scrubs_seeded_key(tmp_path, backend):
    seeded = {"providers": {"local": {"baseUrl": _STALE_URL, "apiKey": _FAKE_KEY, "models": [{"id": "m1"}]}}}
    _, models, raw, url = _run_injectors(tmp_path, backend=backend, pi=seeded)
    local = models["providers"]["local"]
    assert _FAKE_KEY not in raw and "192.168.5.2" not in raw
    assert local["apiKey"] == _placeholder()
    assert local["baseUrl"] == url
    assert local["models"] == [{"id": "m1", "input": ["text", "image"]}], (
        "existing models lost when discovery found none"
    )


@pytest.mark.parametrize("backend", ["omlx", "ollama"])
def test_stale_key_scrubbed_when_host_key_unset(tmp_path, backend):
    # What setdefault got wrong: no key this run, so a stale one was kept.
    data, models, raw, _ = _run_injectors(
        tmp_path, backend=backend, key=None,
        opencode={"provider": {"omlx": _stale_entry(), "ollama": _stale_entry()}},
        pi={"providers": {"local": {"apiKey": _FAKE_KEY}}})
    assert _FAKE_KEY not in raw
    assert data["provider"]["omlx"]["options"]["apiKey"] == _placeholder()
    assert models["providers"]["local"]["apiKey"] == _placeholder()


@pytest.mark.parametrize("backend,key", [("omlx", _FAKE_KEY), ("omlx", None), ("ollama", None)])
def test_fresh_configs_get_non_empty_placeholder(tmp_path, backend, key):
    # Clients that insist on a non-empty apiKey must work with no user setup.
    data, models, _, url = _run_injectors(tmp_path, backend=backend, key=key)
    assert _placeholder()
    assert data["provider"][backend]["options"] == {"baseURL": url, "apiKey": _placeholder()}
    assert models["providers"]["local"]["apiKey"] == _placeholder()
    other = "ollama" if backend == "omlx" else "omlx"
    assert other not in data["provider"], "inactive entry must not be invented"


def test_unrelated_settings_survive(tmp_path):
    cloud = {"options": {"baseURL": "https://api.example.com/v1", "apiKey": "sk-cloud-own"}}
    pi_cloud = {"baseUrl": "https://api.example.com/v1", "apiKey": "sk-cloud-own", "api": "x"}
    seeded = {"provider": {"omlx": {**_stale_entry(), "name": "mine", "extra": 1}, "anthropic": cloud},
              "theme": "custom", "mcp": {"mine": {"type": "local"}},
              "permission": {"webfetch": "deny"}}
    data, models, _, _ = _run_injectors(
        tmp_path, opencode=seeded, pi={"providers": {"cloud": pi_cloud}, "note": "keep"})
    assert data["provider"]["anthropic"] == cloud
    assert data["theme"] == "custom" and data["mcp"]["mine"] == {"type": "local"}
    assert data["permission"]["webfetch"] == "deny"
    omlx = data["provider"]["omlx"]
    assert (omlx["name"], omlx["extra"], omlx["models"]) == ("mine", 1, {"m1": {"name": "m1", **_OC_VISION}})
    assert models["providers"]["cloud"] == pi_cloud and models["note"] == "keep"


# Models are assumed to take images: discovery can't tell, and without these
# keys both clients drop images before the request.
_OC_VISION = {"modalities": {"input": ["text", "image"], "output": ["text"]}, "attachment": True}


def test_preserved_models_assumed_image_capable(tmp_path):
    # Discovery fails here (nothing listens), so the existing entries are kept.
    data, models, _, _ = _run_injectors(
        tmp_path, opencode={"provider": {"omlx": _stale_entry()}},
        pi={"providers": {"local": {"models": [{"id": "p1"}]}}})
    assert data["provider"]["omlx"]["models"] == {"m1": {"name": "m1", **_OC_VISION}}
    assert models["providers"]["local"]["models"] == [{"id": "p1", "input": ["text", "image"]}]


def test_declared_text_only_model_kept(tmp_path):
    text_only = {"name": "m1", "modalities": {"input": ["text"], "output": ["text"]}, "attachment": False}
    entry = {**_stale_entry(), "models": {"m1": text_only}}
    data, models, _, _ = _run_injectors(
        tmp_path, opencode={"provider": {"omlx": entry}},
        pi={"providers": {"local": {"models": [{"id": "p1", "input": ["text"]}]}}})
    assert data["provider"]["omlx"]["models"]["m1"] == text_only
    assert models["providers"]["local"]["models"] == [{"id": "p1", "input": ["text"]}]


def test_copies_of_real_key_scrubbed_from_any_entry(tmp_path):
    # A hand-made provider holding the key is still the key inside the container.
    custom = {"options": {"apiKey": _FAKE_KEY, "headers": {"Authorization": f"Bearer {_FAKE_KEY}"}}}
    _, _, raw, _ = _run_injectors(
        tmp_path, opencode={"provider": {"my-omlx": custom}},
        pi={"providers": {"my-omlx": {"apiKey": _FAKE_KEY}}})
    assert _FAKE_KEY not in raw


def test_malformed_config_replaced_without_key(tmp_path):
    bad = '{"apiKey": "' + _FAKE_KEY + '", '
    _, _, raw, _ = _run_injectors(tmp_path, opencode=bad, pi=bad)
    assert _FAKE_KEY not in raw


def test_inactive_entry_without_options_handled(tmp_path):
    data, _, _, url = _run_injectors(
        tmp_path, opencode={"provider": {"ollama": {"options": None}, "custom": "not-a-dict"}})
    assert data["provider"]["ollama"]["options"] == {"baseURL": url, "apiKey": _placeholder()}
    assert data["provider"]["custom"] == "not-a-dict"


def test_discovery_uses_real_key_and_writes_only_the_placeholder(tmp_path):
    seen = []

    class Models(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            body = json.dumps({"data": [{"id": "disc-1"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Models)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        data, models, raw, url = _run_injectors(tmp_path, port=server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
    assert seen and all(s == ("/v1/models", f"Bearer {_FAKE_KEY}") for s in seen)
    assert data["provider"]["omlx"]["models"] == {"disc-1": {"name": "disc-1", **_OC_VISION}}
    assert models["providers"]["local"]["models"] == [{"id": "disc-1", "input": ["text", "image"]}]
    assert _FAKE_KEY not in raw
    assert data["provider"]["omlx"]["options"]["baseURL"] == url
    assert models["providers"]["local"]["baseUrl"] == url


def test_key_not_on_injector_argv(tmp_path):
    # Argv is visible in host `ps`; the injectors must take the key from the env.
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" >> "{argv_log}"\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    _run_injectors(tmp_path, path=f"{shim_dir}:/usr/bin:/bin")
    logged = argv_log.read_text()
    assert _placeholder() in logged, "shim did not see the injector's argv"
    assert _FAKE_KEY not in logged


# ── reattach: container predates the inference proxy ─────────────────────────

_SECRET = "sk-test-SECRETVALUE-123"


def _run_predates_proxy_warning(tmp_path, env_lines, inspect_rc=0):
    """Run the real warning function under bash with a stubbed docker.

    The stub prints env_lines for any `docker container inspect` call (or fails
    with inspect_rc), and records every argv so the test can see what was asked.
    """
    body = _function_body("warn_if_container_predates_proxy")
    env_file = tmp_path / "env.txt"
    env_file.write_text("".join(f"{line}\n" for line in env_lines))
    script = (
        "set -euo pipefail\n"
        f'docker() {{ echo "$*" >> "{tmp_path}/docker.log"; '
        f'[[ {inspect_rc} -eq 0 ]] || return {inspect_rc}; cat "{env_file}"; }}\n'
        'CONTAINER_NAME=claude-agent\n'
        'HOST_IP=192.168.5.2\n'
        'BRIDGE_IP=172.17.0.1\n'
        f"warn_if_container_predates_proxy() {body}\n"
        "warn_if_container_predates_proxy\n"
        'echo SCRIPT_CONTINUED\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)


_CLEAN_ENV = [
    "PATH=/usr/bin",
    "OMLX_HOST=http://172.17.0.1:8000",
    "HTTPS_PROXY=http://172.17.0.1:8888",
]


def test_reattach_warns_when_container_holds_omlx_key(tmp_path):
    proc = _run_predates_proxy_warning(
        tmp_path, _CLEAN_ENV + [f"OMLX_API_KEY={_SECRET}"]
    )
    assert proc.returncode == 0, proc.stderr
    assert "OMLX_API_KEY" in proc.stderr
    assert "--reset-container" in proc.stderr
    assert "read" in proc.stderr, "must say plainly that the agent can read the key"
    assert "SCRIPT_CONTINUED" in proc.stdout, "must warn, not abort or prompt"


def test_reattach_warning_never_prints_key_value(tmp_path):
    proc = _run_predates_proxy_warning(
        tmp_path,
        ["OMLX_HOST=http://192.168.5.2:8000", f"OMLX_API_KEY={_SECRET}"],
    )
    assert "OMLX_API_KEY" in proc.stderr
    assert _SECRET not in proc.stdout + proc.stderr
    assert "SECRETVALUE" not in proc.stdout + proc.stderr


def test_reattach_warns_on_stale_host_url_without_key(tmp_path):
    for var in ("OLLAMA_HOST", "OMLX_HOST"):
        proc = _run_predates_proxy_warning(
            tmp_path, ["PATH=/usr/bin", f"{var}=http://192.168.5.2:11434"]
        )
        assert proc.returncode == 0, proc.stderr
        assert "predates" in proc.stderr, var
        assert "--reset-container" in proc.stderr, var
        assert "SCRIPT_CONTINUED" in proc.stdout


def test_reattach_stale_url_alone_does_not_claim_key_exposure(tmp_path):
    proc = _run_predates_proxy_warning(
        tmp_path, ["OMLX_HOST=http://192.168.5.2:8000"]
    )
    assert "predates" in proc.stderr
    assert "OMLX_API_KEY" not in proc.stderr


def test_reattach_silent_for_container_on_proxy(tmp_path):
    proc = _run_predates_proxy_warning(tmp_path, _CLEAN_ENV)
    assert proc.returncode == 0
    assert proc.stderr == "" and proc.stdout.strip() == "SCRIPT_CONTINUED"


def test_reattach_similarly_named_var_does_not_trigger(tmp_path):
    proc = _run_predates_proxy_warning(
        tmp_path,
        _CLEAN_ENV
        + [f"MY_OMLX_API_KEY_OLD={_SECRET}", f"OMLX_API_KEY_OLD={_SECRET}",
           "NOT_OMLX_HOST=http://192.168.5.2:8000"],
    )
    assert proc.stderr == "", proc.stderr


def test_reattach_proxy_check_survives_inspect_failure(tmp_path):
    proc = _run_predates_proxy_warning(tmp_path, [], inspect_rc=1)
    assert proc.returncode == 0, proc.stderr
    assert "SCRIPT_CONTINUED" in proc.stdout


def test_reattach_proxy_check_inspects_create_time_env_only(tmp_path):
    _run_predates_proxy_warning(tmp_path, _CLEAN_ENV)
    log = (tmp_path / "docker.log").read_text()
    assert ".Config.Env" in log
    assert log.count("\n") == 1, "one inspect call; never dumps the whole container"


def test_reattach_calls_proxy_check_before_attaching():
    block = _reattach_block()
    assert "warn_if_container_predates_proxy" in block
    assert block.find("warn_if_container_predates_proxy") < block.find("attach_existing")
    assert block.find("HostConfig.Dns") < block.find("warn_if_container_predates_proxy")
    # Warn only: never auto-remove (ADR-048) on this path.
    warn_to_attach = block[block.find("warn_if_container_predates_proxy"):block.find("attach_existing\n")]
    assert "docker rm" not in warn_to_attach and "read -" not in warn_to_attach
