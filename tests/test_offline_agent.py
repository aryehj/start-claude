"""Unit tests for offline-agent.py pure helpers and docker argv invariants."""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "offline-agent.py"

# The hyphenated filename is not importable by name; load it by path.
_spec = importlib.util.spec_from_file_location("offline_agent", SCRIPT)
oa = importlib.util.module_from_spec(_spec)
sys.modules["offline_agent"] = oa
_spec.loader.exec_module(oa)

_SOURCE = SCRIPT.read_text()


def _config(backend="omlx"):
    return oa.VmConfig(
        backend=backend,
        bridge_ip="172.17.0.1",
        bridge_cidr="172.17.0.0/16",
        host_ip="192.168.5.2",
        net_cidr="172.30.0.0/16",
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_backend_defaults_to_omlx(monkeypatch):
    monkeypatch.delenv("OFFLINE_AGENT_BACKEND", raising=False)
    assert oa.parse_args([]).backend == "omlx"


def test_backend_env_override(monkeypatch):
    monkeypatch.setenv("OFFLINE_AGENT_BACKEND", "ollama")
    assert oa.parse_args([]).backend == "ollama"


def test_sizing_env_and_flags(monkeypatch):
    monkeypatch.setenv("OFFLINE_AGENT_MEMORY", "6G")
    monkeypatch.setenv("OFFLINE_AGENT_CPUS", "3")
    args = oa.parse_args([])
    assert (args.memory, args.cpus) == (6, 3)
    args = oa.parse_args(["--memory", "2", "--cpus", "1", "--rebuild"])
    assert (args.memory, args.cpus, args.rebuild) == (2, 1, True)


def test_running_vm_size_bytes_to_gib():
    # colima list --json reports memory in bytes; a substring check never warns.
    assert oa.running_vm_size({"cpus": 4, "memory": 4294967296}) == (4, 4)
    assert oa.running_vm_size({"cpus": 6, "memory": 12884901888}) == (6, 12)
    assert oa.running_vm_size({}) == (None, None)


def test_inference_ports():
    assert oa.VmConfig(backend="omlx").inference_port == 8000
    assert oa.VmConfig(backend="ollama").inference_port == 11434


# ── project validation ────────────────────────────────────────────────────────

def test_project_under_home_accepted(tmp_path):
    home = tmp_path / "home"
    proj = home / "code" / "proj"
    proj.mkdir(parents=True)
    assert oa.resolve_project(proj, home) == proj.resolve()


def test_project_symlink_resolved(tmp_path):
    home = tmp_path / "home"
    real = home / "real"
    real.mkdir(parents=True)
    link = home / "link"
    link.symlink_to(real)
    assert oa.resolve_project(link, home) == real.resolve()


def test_project_home_itself_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(oa.ProjectError, match=r"\$HOME"):
        oa.resolve_project(home, home)


def test_project_outside_home_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(oa.ProjectError, match="not under"):
        oa.resolve_project(other, home)


def test_project_symlink_escaping_home_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "escape").symlink_to(outside)
    with pytest.raises(oa.ProjectError):
        oa.resolve_project(home / "escape", home)


@pytest.mark.parametrize("name", ["a,b", "a:b", "a\\b"])
def test_project_overlay_unsafe_chars_rejected(tmp_path, name):
    # ',' splits overlay mount options and ':' separates lowerdirs (and -v fields).
    home = tmp_path / "home"
    proj = home / name
    proj.mkdir(parents=True)
    with pytest.raises(oa.ProjectError, match="character"):
        oa.resolve_project(proj, home)


def test_project_with_space_accepted(tmp_path):
    home = tmp_path / "home"
    proj = home / "My Project"
    proj.mkdir(parents=True)
    assert oa.resolve_project(proj, home) == proj.resolve()


# ── session identity ──────────────────────────────────────────────────────────

def test_session_id_deterministic_and_short_hex():
    sid = oa.session_id(Path("/Users/me/code/proj"))
    assert sid == oa.session_id(Path("/Users/me/code/proj"))
    assert re.fullmatch(r"[0-9a-f]{12}", sid)


def test_session_id_differs_per_path():
    assert oa.session_id(Path("/Users/me/a")) != oa.session_id(Path("/Users/me/b"))


def test_session_names_derive_from_id():
    sid = "abcdef012345"
    assert oa.volume_name(sid) == "offline-agent-abcdef012345"
    assert oa.container_name(sid) == "offline-agent-abcdef012345"
    d = oa.session_dir(sid)
    assert d == "/var/lib/offline-agent/sessions/abcdef012345"


def test_session_setup_script_creates_upper_work_and_marker():
    script = oa.render_session_setup_script("abcdef012345", Path("/Users/me/My Project"))
    d = "/var/lib/offline-agent/sessions/abcdef012345"
    assert f"{d}/upper" in script and f"{d}/work" in script
    assert "mkdir -p" in script and "sudo" in script
    assert f"{d}/path" in script
    # The path is shell-quoted so spaces survive.
    assert "'/Users/me/My Project'" in script


def test_volume_create_argv_is_overlay_over_project():
    argv = oa.volume_create_argv("abcdef012345", Path("/Users/me/proj"))
    d = "/var/lib/offline-agent/sessions/abcdef012345"
    assert argv[:3] == ["docker", "volume", "create"]
    assert argv[-1] == "offline-agent-abcdef012345"
    joined = " ".join(argv)
    assert "--driver local" in joined
    assert "type=overlay" in joined and "device=overlay" in joined
    assert f"o=lowerdir=/Users/me/proj,upperdir={d}/upper,workdir={d}/work" in argv


def test_parse_session_listing():
    text = "abc123\t/Users/me/a\n\ndef456\t/Users/me/My Project\n"
    assert oa.parse_session_listing(text) == [
        ("abc123", "/Users/me/a"),
        ("def456", "/Users/me/My Project"),
    ]


def test_parse_session_listing_missing_marker():
    assert oa.parse_session_listing("abc123\t\n") == [("abc123", "")]


# ── iptables ──────────────────────────────────────────────────────────────────

def _fw(**kw):
    args = dict(
        bridge_cidr="172.17.0.0/16",
        net_cidr="172.30.0.0/16",
        host_ip="192.168.5.2",
        inference_port=8000,
    )
    args.update(kw)
    return oa.render_iptables_apply_script(**args)


def _chain_rules(script, chain):
    return [l for l in script.splitlines() if l.startswith(f"-A {chain} ")]


def test_iptables_no_uninterpolated_vars():
    assert "$" not in _fw().replace("$?", "")


def test_iptables_inference_is_the_only_carve_out():
    rules = _chain_rules(_fw(), "OFFLINE_AGENT")
    assert rules[0] == "-A OFFLINE_AGENT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
    assert rules[1] == "-A OFFLINE_AGENT -d 192.168.5.2 -p tcp --dport 8000 -j RETURN"
    assert rules[-1].endswith("-j REJECT --reject-with icmp-admin-prohibited")
    assert len(rules) == 3


def test_iptables_no_dns_or_proxy_carve_out():
    script = _fw()
    assert "--dport 53" not in script
    assert "8888" not in script
    assert "tinyproxy" not in script.lower()


def test_iptables_input_chain_rejects_all_new():
    rules = _chain_rules(_fw(), "OFFLINE_AGENT_IN")
    assert rules == [
        "-A OFFLINE_AGENT_IN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN",
        "-A OFFLINE_AGENT_IN -j REJECT --reject-with icmp-admin-prohibited",
    ]


def test_iptables_chains_replaced_in_one_restore_commit():
    script = _fw()
    assert "iptables-restore --noflush" in script
    body = script[script.index("*filter"):script.index("COMMIT")]
    assert ":OFFLINE_AGENT - [0:0]" in body
    assert ":OFFLINE_AGENT_IN - [0:0]" in body
    # No separate flush that would leave a chain empty mid-apply.
    assert "-F OFFLINE_AGENT" not in script


def test_iptables_jumps_for_both_networks():
    script = _fw()
    for cidr in ("172.17.0.0/16", "172.30.0.0/16"):
        assert f"-I DOCKER-USER 1 -s {cidr} -j OFFLINE_AGENT" in script
        assert f"-I INPUT 1 -s {cidr} -j OFFLINE_AGENT_IN" in script


def test_iptables_uses_backend_port():
    assert "--dport 11434" in _fw(inference_port=11434)


# ── opencode config merge ─────────────────────────────────────────────────────

def _oc(data=None, backend="omlx", discovered=("m1", "m2"), default_model="", api_key=""):
    return oa.merge_opencode_config(
        data or {},
        backend=backend,
        runtime_url="http://192.168.5.2:8000/v1",
        api_key=api_key,
        discovered=list(discovered),
        default_model=default_model,
    )


def test_opencode_offline_switches():
    out = _oc()
    assert out["autoupdate"] is False
    assert out["permission"]["webfetch"] == "deny"
    assert out["permission"]["websearch"] == "deny"
    assert out["enabled_providers"] == ["omlx"]
    assert "mcp" not in out


def test_opencode_deny_overrides_existing_allow():
    out = _oc({"permission": {"webfetch": "allow", "websearch": "allow", "bash": "ask"}})
    assert out["permission"] == {"webfetch": "deny", "websearch": "deny", "bash": "ask"}


def test_opencode_preserves_user_keys():
    out = _oc({"theme": "nord", "mcp": {"mine": {"type": "local"}}, "keybinds": {"a": "b"}})
    assert out["theme"] == "nord"
    assert out["mcp"] == {"mine": {"type": "local"}}
    assert out["keybinds"] == {"a": "b"}


def test_opencode_does_not_mutate_input():
    data = {"permission": {"webfetch": "allow"}}
    _oc(data)
    assert data == {"permission": {"webfetch": "allow"}}


def test_opencode_provider_entry():
    entry = _oc()["provider"]["omlx"]
    assert entry["npm"] == "@ai-sdk/openai-compatible"
    assert entry["options"]["baseURL"] == "http://192.168.5.2:8000/v1"
    assert entry["models"] == {"m1": {"name": "m1"}, "m2": {"name": "m2"}}


def test_opencode_ollama_provider_and_key():
    out = _oc(backend="ollama")
    assert out["enabled_providers"] == ["ollama"]
    assert out["provider"]["ollama"]["options"]["apiKey"] == "ollama"
    assert out["model"] == "ollama/m1"


def test_opencode_omlx_api_key():
    assert _oc(api_key="sekrit")["provider"]["omlx"]["options"]["apiKey"] == "sekrit"
    assert _oc()["provider"]["omlx"]["options"]["apiKey"] == "omlx"


def test_opencode_models_preserved_when_discovery_fails():
    prior = {"provider": {"omlx": {"models": {"old": {"name": "old"}}}}, "model": "omlx/old"}
    out = _oc(prior, discovered=())
    assert out["provider"]["omlx"]["models"] == {"old": {"name": "old"}}
    assert out["model"] == "omlx/old"


def test_opencode_stale_cloud_model_replaced():
    assert _oc({"model": "opencode/big-pickle"})["model"] == "omlx/m1"


def test_opencode_local_model_choice_kept():
    assert _oc({"model": "omlx/m2"})["model"] == "omlx/m2"


def test_opencode_default_model_override_qualified():
    assert _oc(default_model="m2")["model"] == "omlx/m2"
    assert _oc(default_model="omlx/x")["model"] == "omlx/x"


# ── pi config merge ───────────────────────────────────────────────────────────

def _pi(models=None, settings=None, backend="omlx", discovered=("m1", "m2"), default_model="", api_key=""):
    return oa.merge_pi_config(
        models or {},
        settings or {},
        backend=backend,
        runtime_url="http://192.168.5.2:8000/v1",
        api_key=api_key,
        discovered=list(discovered),
        default_model=default_model,
    )


def test_pi_local_provider_entry():
    models, _ = _pi()
    entry = models["providers"]["local"]
    assert entry["baseUrl"] == "http://192.168.5.2:8000/v1"
    assert entry["api"] == "openai-completions"
    assert entry["models"] == [{"id": "m1"}, {"id": "m2"}]


def test_pi_default_selection():
    _, settings = _pi()
    assert settings["defaultProvider"] == "local"
    assert settings["defaultModel"] == "m1"


def test_pi_stale_provider_replaced_and_user_keys_kept():
    _, settings = _pi(settings={"defaultProvider": "anthropic", "defaultModel": "x", "theme": "dark"})
    assert settings == {"defaultProvider": "local", "defaultModel": "m1", "theme": "dark"}


def test_pi_override_and_other_providers_kept():
    models, settings = _pi(
        models={"providers": {"mine": {"baseUrl": "u"}}}, default_model="m2"
    )
    assert models["providers"]["mine"] == {"baseUrl": "u"}
    assert settings["defaultModel"] == "m2"


def test_pi_ollama_key():
    models, _ = _pi(backend="ollama")
    assert models["providers"]["local"]["apiKey"] == "ollama"


# ── docker run / exec argv ────────────────────────────────────────────────────

ENV = {
    "TERM": "xterm-ghostty",
    "COLORTERM": "truecolor",
    "TERM_PROGRAM": "ghostty",
    "GIT_USER_NAME": "Me",
    "GIT_USER_EMAIL": "me@example.com",
    "OMLX_API_KEY": "sekrit",
    "ANTHROPIC_API_KEY": "sk-ant",
    "OPENAI_API_KEY": "sk-oai",
    "HTTPS_PROXY": "http://proxy:1",
}
PROJECT = Path("/Users/me/My Project")
SID = "abcdef012345"


def _run(backend="omlx", env=ENV):
    return oa.docker_run_argv(PROJECT, SID, _config(backend), oa.Paths(base=Path("/Users/me/.offline-agent")), env)


def _flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_run_is_ephemeral_and_interactive():
    argv = _run()
    assert argv[:2] == ["docker", "run"]
    assert "--rm" in argv and "-it" in argv
    assert _flag_values(argv, "--name") == ["offline-agent-abcdef012345"]
    assert argv[-2:] == ["offline-agent:latest", "bash"]


def test_run_publishes_no_ports():
    argv = _run()
    assert not any(a in ("-p", "--publish", "-P", "--publish-all") or a.startswith("--publish") for a in argv)


def test_run_network_and_dns_blackhole():
    argv = _run()
    assert _flag_values(argv, "--network") == ["offline-agent-net"]
    assert _flag_values(argv, "--dns") == ["192.0.2.1"]
    assert _flag_values(argv, "--add-host") == ["host.docker.internal:192.168.5.2"]


def test_run_mounts_overlay_at_project_path():
    argv = _run()
    vols = _flag_values(argv, "-v")
    assert "offline-agent-abcdef012345:/Users/me/My Project:nocopy" in vols
    assert _flag_values(argv, "-w") == ["/Users/me/My Project"]


def test_run_mounts_harness_state_only():
    vols = _flag_values(_run(), "-v")
    assert vols[1:] == [
        "/Users/me/.offline-agent/opencode/config:/root/.config/opencode",
        "/Users/me/.offline-agent/opencode/data:/root/.local/share/opencode",
        "/Users/me/.offline-agent/pi:/root/.pi",
    ]


def test_run_has_no_proxy_env():
    envs = _flag_values(_run(), "-e")
    names = {e.split("=", 1)[0].upper() for e in envs}
    assert not names & {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "NODE_USE_ENV_PROXY"}


def test_run_forwards_terminal_and_git_identity():
    envs = _flag_values(_run(), "-e")
    for e in (
        "TERM=xterm-ghostty", "COLORTERM=truecolor", "TERM_PROGRAM=ghostty",
        "GIT_AUTHOR_NAME=Me", "GIT_AUTHOR_EMAIL=me@example.com",
        "GIT_COMMITTER_NAME=Me", "GIT_COMMITTER_EMAIL=me@example.com",
        "TMPDIR=/tmp",
    ):
        assert e in envs


def test_run_git_identity_defaults():
    envs = _flag_values(_run(env={}), "-e")
    assert "GIT_AUTHOR_NAME=Dev" in envs and "GIT_AUTHOR_EMAIL=dev@localhost" in envs
    assert "TERM=xterm-256color" in envs


def test_run_omlx_key_value_less_and_no_cloud_keys():
    argv = _run()
    assert "sekrit" not in " ".join(argv)
    envs = _flag_values(argv, "-e")
    assert "OMLX_API_KEY" in envs
    assert "OMLX_HOST=http://192.168.5.2:8000" in envs
    assert not any(e.startswith(("ANTHROPIC_API_KEY", "OPENAI_API_KEY")) for e in envs)


def test_run_omlx_key_omitted_when_unset():
    envs = _flag_values(_run(env={}), "-e")
    assert "OMLX_API_KEY" not in envs


def test_run_ollama_host_env():
    envs = _flag_values(_run(backend="ollama"), "-e")
    assert "OLLAMA_HOST=http://192.168.5.2:11434" in envs
    assert not any(e.startswith("OMLX") for e in envs)


def test_exec_argv_attaches_second_shell():
    argv = oa.docker_exec_argv(PROJECT, SID, _config(), ENV)
    assert argv[:3] == ["docker", "exec", "-it"]
    assert _flag_values(argv, "-w") == ["/Users/me/My Project"]
    assert argv[-2:] == ["offline-agent-abcdef012345", "bash"]
    assert "sekrit" not in " ".join(argv)


# ── static invariants on the script source ────────────────────────────────────

def test_docker_context_not_persisted():
    # `docker context use` would hijack the user's CLI for the sibling VMs.
    assert "context\", \"use" not in _SOURCE and "context use" not in _SOURCE


def test_colima_start_has_no_mount_flag():
    start = _SOURCE[_SOURCE.index('"colima", "start"'):]
    start = start[:start.index("]")]
    assert "--mount\"" not in start and "--mount-type" in start


def test_image_build_uses_host_network():
    assert '"--network=host"' in _SOURCE


def test_rebuild_warns_about_unexported_sessions():
    body = _SOURCE[_SOURCE.index("def rebuild_teardown"):]
    body = body[:body.index("\ndef ")]
    assert "list_sessions" in body and "colima\", \"delete" in body
