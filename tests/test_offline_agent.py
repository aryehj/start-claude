"""Unit tests for offline-agent.py pure helpers and docker argv invariants."""
import argparse
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
    # colima list --json reports memory and disk in bytes; a substring check never warns.
    assert oa.running_vm_size({"cpus": 4, "memory": 4294967296, "disk": 10737418240}) == (4, 4, 10)
    assert oa.running_vm_size({"cpus": 6, "memory": 12884901888}) == (6, 12, None)
    assert oa.running_vm_size({}) == (None, None, None)


@pytest.mark.parametrize("raw,mib", [
    ("5", 5120), ("5G", 5120), ("5GiB", 5120), ("5gb", 5120),
    ("256M", 256), ("256MiB", 256), ("512mb", 512),
])
def test_parse_size_mib(raw, mib):
    assert oa._parse_size_mib(raw) == mib


@pytest.mark.parametrize("raw", ["", "big", "5T", "0", "-1G"])
def test_parse_size_mib_rejects(raw):
    with pytest.raises(argparse.ArgumentTypeError):
        oa._parse_size_mib(raw)


def test_storage_defaults(monkeypatch):
    for var in ("OFFLINE_AGENT_DISK", "OFFLINE_AGENT_ROOT_DISK", "OFFLINE_AGENT_SESSION_DISK"):
        monkeypatch.delenv(var, raising=False)
    args = oa.parse_args([])
    assert (args.disk, args.root_disk, args.session_disk) == (10, 10, 5120)


def test_storage_env_and_flags(monkeypatch):
    monkeypatch.setenv("OFFLINE_AGENT_DISK", "20G")
    monkeypatch.setenv("OFFLINE_AGENT_ROOT_DISK", "12")
    monkeypatch.setenv("OFFLINE_AGENT_SESSION_DISK", "256M")
    args = oa.parse_args([])
    assert (args.disk, args.root_disk, args.session_disk) == (20, 12, 256)
    args = oa.parse_args(["--disk", "15", "--root-disk", "8", "--session-disk", "2G"])
    assert (args.disk, args.root_disk, args.session_disk) == (15, 8, 2048)


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


D = "/var/lib/offline-agent/sessions/abcdef012345"


def _setup(cap_mib=5120):
    return oa.render_session_setup_script("abcdef012345", Path("/Users/me/My Project"), cap_mib)


def test_session_setup_script_creates_upper_work_and_marker():
    script = _setup()
    for sub in ("upper", "work", "harness/opencode", "harness/state", "harness/pi"):
        assert f"{D}/mnt/{sub}" in script
    assert "mkdir -p" in script and "sudo" in script
    assert f"{D}/path" in script
    # The path is shell-quoted so spaces survive.
    assert "'/Users/me/My Project'" in script


def test_session_setup_script_sizes_sparse_image():
    script = _setup(cap_mib=256)
    assert "truncate -s 256M" in script
    assert "mkfs.ext4 -q -F" in script


def test_session_setup_script_is_idempotent():
    lines = _setup().splitlines()
    # mkfs runs only inside an existence guard on disk.img...
    guard = next(i for i, l in enumerate(lines) if l.strip() == f'if [ ! -e {D}/disk.img ]; then')
    mkfs = next(i for i, l in enumerate(lines) if "mkfs.ext4" in l)
    end = next(i for i, l in enumerate(lines) if i > guard and l.strip() == "fi")
    assert guard < mkfs < end
    # ...and the image only appears under its final name once formatted, so a
    # failed mkfs is retried instead of mounting a half-made image.
    assert any(l.strip().startswith("sudo mv") and l.rstrip().endswith(f"{D}/disk.img") for l in lines[mkfs:end])
    # The loop mount does not survive a VM restart, so it is re-done whenever absent.
    mount = next(l for l in lines if "mount -o loop,discard" in l)
    assert mount.startswith(f"mountpoint -q {D}/mnt ||") or mount.startswith(f"sudo mountpoint -q {D}/mnt ||")
    assert f"{D}/disk.img {D}/mnt" in mount


def test_session_setup_script_layout_lives_on_the_image():
    script = _setup()
    # Nothing agent-writable is created outside the mounted image (the
    # legacy-session check only tests for a top-level upper/).
    body = "\n".join(l for l in script.splitlines() if not l.startswith(f"if [ -d {D}/upper ]"))
    assert f"{D}/upper" not in body.replace(f"{D}/mnt/upper", "")
    assert f"{D}/work" not in body.replace(f"{D}/mnt/work", "")
    mount_at = script.index("mount -o loop,discard")
    assert script.index(f"{D}/mnt/upper") > mount_at


def test_session_setup_script_detects_legacy_session_first():
    script = _setup()
    legacy = script.index(f'[ -d {D}/upper ]')
    assert legacy < script.index("mkdir")
    guard = script[legacy:script.index("mkdir")]
    assert f"echo {oa.LEGACY_SESSION_MARKER}" in guard and "exit 1" in guard


def test_legacy_session_message_has_export_and_cleanup_recipe():
    msg = oa.legacy_session_message("abcdef012345", Path("/Users/me/proj"))
    assert "/Users/me/proj" in msg
    assert "git bundle create - --all" in msg
    assert "offline-agent-abcdef012345:" in msg
    assert "docker --context colima-offline-agent volume rm offline-agent-abcdef012345" in msg
    assert f"sudo rm -rf {D}" in msg


def test_ensure_session_handles_legacy_exit():
    body = _SOURCE[_SOURCE.index("def ensure_session"):]
    body = body[:body.index("\ndef ")]
    # colima ssh does not propagate the remote exit code, so the marker is
    # matched on stdout rather than compared against returncode.
    assert "LEGACY_SESSION_MARKER in result.stdout" in body and "legacy_session_message" in body
    assert "result.returncode == " not in body


@pytest.mark.parametrize("avail_kib,warns", [
    (None, False),
    (6 * 1024 * 1024, False),
    (5 * 1024 * 1024, False),
    (5 * 1024 * 1024 - 1, True),
    (100, True),
])
def test_low_space_warning_threshold(avail_kib, warns):
    msg = oa.low_space_warning(avail_kib, cap_mib=5120)
    assert bool(msg) == warns
    if warns:
        assert "--root-disk" in msg


def test_probe_reports_root_free_space():
    body = _SOURCE[_SOURCE.index("def probe_vm"):]
    body = body[:body.index("\ndef ")]
    assert "ROOT_AVAIL_KIB=" in body and "df -Pk" in body


def test_volume_create_argv_is_overlay_over_project():
    argv = oa.volume_create_argv("abcdef012345", Path("/Users/me/proj"))
    d = "/var/lib/offline-agent/sessions/abcdef012345"
    assert argv[:3] == ["docker", "volume", "create"]
    assert argv[-1] == "offline-agent-abcdef012345"
    joined = " ".join(argv)
    assert "--driver local" in joined
    assert "type=overlay" in joined and "device=overlay" in joined
    assert f"o=lowerdir=/Users/me/proj,upperdir={d}/mnt/upper,workdir={d}/mnt/work" in argv


def test_parse_session_listing():
    text = "abc123\t/Users/me/a\n\ndef456\t/Users/me/My Project\n"
    assert oa.parse_session_listing(text) == [
        ("abc123", "/Users/me/a"),
        ("def456", "/Users/me/My Project"),
    ]


def test_list_sessions_script_lists_unmounted_images():
    # After a VM boot no image is mounted; those must still be listed so the
    # --rebuild VM-delete prompt warns about them.
    script = oa.render_list_sessions_script()
    assert '"$d/disk.img"' in script and 'mountpoint -q "$d/mnt"' in script
    assert '"$d/mnt/upper"' in script
    # Pre-image sessions are still detected by their top-level upper/.
    assert '"$d/upper"' in script
    assert "{" not in script.replace("${d%/}", "")


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


def test_run_mounts_harness_config_ro_and_state_on_session_image():
    vols = _flag_values(_run(), "-v")
    assert vols[1:] == [
        "/Users/me/.offline-agent/opencode/config:/root/.config/opencode:ro",
        "/Users/me/.offline-agent/pi/agent/models.json:/root/.pi/agent/models.json:ro",
        "/Users/me/.offline-agent/pi/agent/settings.json:/root/.pi/agent/settings.json:ro",
        f"{D}/mnt/harness/opencode:/root/.local/share/opencode",
        f"{D}/mnt/harness/state:/root/.local/state",
        f"{D}/mnt/harness/pi:/root/.pi",
    ]


def test_run_every_host_mount_is_read_only():
    # Anything not the overlay volume or the session image is a Mac path via
    # virtiofs; none of those may be writable.
    for vol in _flag_values(_run(), "-v"):
        src = vol.split(":", 1)[0]
        if src == "offline-agent-abcdef012345" or src.startswith(D + "/mnt/"):
            continue
        assert vol.endswith(":ro"), vol


def test_run_root_is_read_only_with_sized_tmpfs():
    argv = _run()
    assert "--read-only" in argv
    tmpfs = {t.split(":", 1)[0]: t.split(":", 1)[1] for t in _flag_values(argv, "--tmpfs")}
    assert set(tmpfs) == {"/tmp", "/root/.cache"}
    for opts in tmpfs.values():
        assert re.search(r"(^|,)size=\d+[kmg]\b", opts), opts
        # uv venvs and node caches run binaries from here.
        assert "noexec" not in opts


def test_run_caps_memory_pids_and_logs():
    argv = _run()
    assert _flag_values(argv, "--memory") == [oa.CONTAINER_MEMORY]
    assert _flag_values(argv, "--pids-limit") == [str(oa.CONTAINER_PIDS_LIMIT)]
    assert _flag_values(argv, "--log-driver") == ["none"]


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
        # The root is read-only; keep shell history with the session.
        "HISTFILE=/root/.local/state/bash_history",
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
    argv = oa.colima_start_argv(_config(), create=True)
    assert argv[:4] == ["colima", "start", "-p", "offline-agent"]
    assert "--mount" not in argv and _flag_values(argv, "--mount-type") == ["virtiofs"]


def test_colima_start_sizes_both_disks_on_create_only():
    config = _config()
    config.disk_gib, config.root_disk_gib = 12, 9
    argv = oa.colima_start_argv(config, create=True)
    assert _flag_values(argv, "--disk") == ["12"]
    assert _flag_values(argv, "--root-disk") == ["9"]
    # An existing VM is never asked to shrink: Colima only warns on --disk and
    # the --root-disk behaviour is unverified.
    argv = oa.colima_start_argv(config, create=False)
    assert "--disk" not in argv and "--root-disk" not in argv


def test_image_build_uses_host_network():
    assert '"--network=host"' in _SOURCE


def test_rebuild_warns_about_unexported_sessions():
    body = _SOURCE[_SOURCE.index("def rebuild_teardown"):]
    body = body[:body.index("\ndef ")]
    assert "list_sessions" in body and "colima\", \"delete" in body


# ── harness config files ──────────────────────────────────────────────────────

def _write_configs(tmp_path, monkeypatch):
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    monkeypatch.delenv("OFFLINE_AGENT_DEFAULT_MODEL", raising=False)
    paths = oa.Paths(base=tmp_path / ".offline-agent")
    oa.write_harness_configs(paths, _config(), [])
    return paths


def test_harness_configs_exist_before_mount(tmp_path, monkeypatch):
    # The per-file :ro mounts need real files: docker would create a directory
    # (on the Mac, through virtiofs) for a missing bind-mount source.
    paths = _write_configs(tmp_path, monkeypatch)
    assert paths.opencode_config_file.is_file()
    assert paths.pi_models_file.is_file()
    assert paths.pi_settings_file.is_file()


def test_harness_state_dir_not_created_on_host(tmp_path, monkeypatch):
    paths = _write_configs(tmp_path, monkeypatch)
    assert not paths.opencode_data_dir.exists()


def test_write_json_keeps_inode(tmp_path):
    # A per-file bind mount pins the inode; an atomic-rename writer would leave
    # a running container reading the old file.
    f = tmp_path / "x.json"
    f.write_text("{}")
    inode = f.stat().st_ino
    oa._write_json(f, {"a": 1})
    assert f.stat().st_ino == inode


def test_moved_state_notice_once(tmp_path, capsys):
    paths = oa.Paths(base=tmp_path / ".offline-agent")
    oa.notice_moved_harness_state(paths)
    assert capsys.readouterr().out == ""
    (paths.opencode_data_dir / "log").mkdir(parents=True)
    (paths.pi_dir / "agent" / "sessions").mkdir(parents=True)
    oa.notice_moved_harness_state(paths)
    out = capsys.readouterr().out
    assert str(paths.opencode_data_dir) in out and "sessions" in out
    oa.notice_moved_harness_state(paths)
    assert capsys.readouterr().out == ""
    # The old state is left in place, never deleted.
    assert (paths.opencode_data_dir / "log").is_dir()


# ── Phase 4: export / apply / discard / list ──────────────────────────────────

import hashlib
import shutil
import subprocess

GIT = shutil.which("git")
RSYNC = shutil.which("rsync")
needs_git = pytest.mark.skipif(GIT is None, reason="git not installed")
needs_rsync = pytest.mark.skipif(RSYNC is None, reason="rsync not installed")
BASE = "a" * 40


@pytest.mark.parametrize("argv", [
    ["--diff", "--apply"],
    ["--export", "x.patch", "--discard"],
    ["--list-sessions", "--diff"],
    ["--rebuild", "--diff"],
    ["--dry-run"],
    ["--diff", "--dry-run"],
    ["--force"],
    ["--apply", "--force"],
    ["--diff", "--session", "abcdef012345"],
    ["--discard", "--session", "../etc"],
])
def test_op_flags_rejected(argv):
    with pytest.raises(SystemExit):
        oa.parse_args(argv)


def test_op_flags_accepted():
    args = oa.parse_args(["--apply", "--dry-run"])
    assert args.apply and args.dry_run
    args = oa.parse_args(["--export", "out.patch", "--session", "abcdef012345"])
    assert (args.export, args.session) == ("out.patch", "abcdef012345")
    args = oa.parse_args(["--discard", "--force"])
    assert args.discard and args.force
    assert oa.parse_args(["--list-sessions"]).list_sessions
    assert oa.parse_args([]).export is None


def _git(cwd, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "user.email", "t@example.com")
    return path


@needs_git
def test_project_baseline_git_repo(tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / "a").write_text("a\n")
    _git(repo, "add", "a")
    _git(repo, "commit", "-qm", "one")
    assert oa.project_baseline(repo) == ("git", _git(repo, "rev-parse", "HEAD"))


@needs_git
def test_project_baseline_unborn_repo(tmp_path):
    assert oa.project_baseline(_repo(tmp_path / "r")) == ("git", "")


def test_project_baseline_plain_dir_and_worktree_file(tmp_path):
    assert oa.project_baseline(tmp_path) == ("dir", "")
    # A linked worktree's .git is a file pointing outside the overlay.
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
    assert oa.project_baseline(tmp_path) == ("dir", "")


def _creation_block(script):
    lines = script.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == f"if [ ! -e {D}/disk.img ]; then")
    end = next(i for i, l in enumerate(lines) if i > start and l.strip() == "fi")
    return "\n".join(lines[start:end]), "\n".join(lines[:start] + lines[end:])


def test_setup_script_records_git_base_at_creation_only():
    script = oa.render_session_setup_script(SID, PROJECT, 5120, mode="git", base=BASE)
    inside, outside = _creation_block(script)
    assert f"{D}/mode" in inside and f"{D}/base" in inside and BASE in inside
    for marker in ("/mode", "/base", "/manifest"):
        assert f"{D}{marker}" not in outside
    assert "find " not in script


def test_setup_script_records_dir_manifest_at_creation_only():
    script = oa.render_session_setup_script(SID, PROJECT, 5120, mode="dir")
    inside, outside = _creation_block(script)
    assert f"{D}/manifest" in inside and "find " in inside
    assert "'/Users/me/My Project'" in inside
    assert f"{D}/manifest" not in outside and f"{D}/base" not in script


@pytest.mark.parametrize("base", ["HEAD", "abc; rm -rf /", "a" * 39, "A" * 40])
def test_base_must_be_a_commit_id(base):
    with pytest.raises(ValueError):
        oa.render_session_setup_script(SID, PROJECT, 5120, mode="git", base=base)
    with pytest.raises(ValueError):
        oa.render_git_script("inspect", base)


def test_manifest_round_trip(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f").write_text("hello")
    (tmp_path / "sp ace\tt").write_text("x")
    (tmp_path / "link").symlink_to("sub/f")
    out = subprocess.run(
        ["bash", "-c", oa.manifest_find_command(tmp_path)], check=True, capture_output=True,
    ).stdout
    manifest = oa.parse_manifest(out)
    assert set(manifest) == {"sub/f", "sp ace\tt", "link"}
    kind, size, mtime = manifest["sub/f"].split(" ")
    assert (kind, size) == ("f", "5") and float(mtime) > 0
    assert manifest["link"].startswith("l ")


# Captured from rsync 3.2.7 with render_rsync_script's flags.
ITEMIZE = """\
*deleting   gone/x
*deleting   gone/
*deleting   del
>f.s....... mod
>f+++++++++ new
.f...p..... perm
>f+++++++++ sp ace
cd+++++++++ newdir/
.d..t...... sub/
.d..t...... ./
cL+++++++++ sub/link
>f+++++++++ sub/z
"""


def test_parse_itemize():
    C = oa.Change
    assert oa.parse_itemize(ITEMIZE) == [
        C("delete", "gone/x", "f"),
        C("delete", "gone", "d"),
        C("delete", "del", "f"),
        C("modify", "mod", "f"),
        C("create", "new", "f"),
        C("modify", "perm", "f"),
        C("create", "sp ace", "f"),
        C("create", "newdir", "d"),
        C("create", "sub/link", "L"),
        C("create", "sub/z", "f"),
    ]


def test_parse_itemize_flags_escaped_names():
    changes = oa.parse_itemize(">f+++++++++ bad\\#012name\n>f+++++++++ ok\n")
    assert oa.unsupported_names(changes) == ["bad\\#012name"]


def _m(size="1", mtime="100.0", kind="f"):
    return f"{kind} {size} {mtime}"


def test_find_conflicts():
    C = oa.Change
    baseline = {"mod": _m(), "del": _m(), "hostedit": _m(), "hostgone": _m()}
    current = {"mod": _m(), "del": _m(), "hostedit": _m(mtime="200.0"), "hostnew": _m()}
    changes = [
        C("modify", "mod", "f"),        # agent-only edit: fine
        C("delete", "del", "f"),        # agent-only delete: fine
        C("create", "fresh", "f"),      # agent-only create: fine
        C("modify", "hostedit", "f"),   # host edited it too
        C("modify", "hostgone", "f"),   # host deleted what the agent edited
        C("modify", "hostnew", "f"),    # host and agent both created it
        C("delete", "somedir", "d"),    # dirs are judged by their contents
        C("create", "newdir", "d"),
    ]
    assert oa.find_conflicts(changes, baseline, current) == ["hostedit", "hostgone", "hostnew"]


def test_export_members():
    tar, deleted = oa.export_members(oa.parse_itemize(ITEMIZE))
    assert tar == ["mod", "new", "perm", "sp ace", "newdir", "sub/link", "sub/z"]
    assert deleted == ["gone/x", "gone/", "del"]


def test_rsync_script_flags():
    dry = oa.render_rsync_script(dry_run=True)
    real = oa.render_rsync_script(dry_run=False)
    for s in (dry, real):
        assert "rsync -rlpt -8 --delete" in s and "--out-format='%i %n'" in s
        assert f"{oa.MERGED_MOUNT}/ {oa.HOST_MOUNT}/" in s
        # No owner/group/devices: virtiofs shows everything as 0:0.
        assert not re.search(r"rsync[^\n]* -a", s) and " -o" not in s and " -D" not in s
        assert oa.NO_RSYNC_MARKER in s
    assert " -n " in dry and " -n " not in real


def _tree(root):
    return {
        str(p.relative_to(root)): (p.is_symlink(), p.read_bytes() if p.is_file() and not p.is_symlink() else None)
        for p in sorted(root.rglob("*"))
    }


@needs_rsync
def test_rsync_scripts_round_trip(tmp_path):
    host, merged = tmp_path / "host", tmp_path / "merged"
    (host / "gone").mkdir(parents=True)
    for name, text in {"keep": "k", "mod": "m", "del": "d", "gone/x": "x"}.items():
        (host / name).write_text(text)
    shutil.copytree(host, merged, symlinks=True)
    shutil.rmtree(merged / "gone")
    (merged / "del").unlink()
    (merged / "mod").write_text("m2")
    (merged / "new").write_text("n")
    (merged / "lnk").symlink_to("keep")

    def run(dry_run):
        s = oa.render_rsync_script(dry_run=dry_run, src=str(merged), dst=str(host))
        return subprocess.run(["bash", "-c", s], check=True, capture_output=True, text=True).stdout

    kinds = {(c.kind, c.path) for c in oa.parse_itemize(run(True))}
    assert kinds == {("delete", "gone/x"), ("delete", "gone"), ("delete", "del"),
                     ("modify", "mod"), ("create", "new"), ("create", "lnk")}
    assert (host / "del").exists()  # dry run wrote nothing
    run(False)
    assert _tree(host) == _tree(merged)


def test_helper_argv_read_only_and_offline():
    argv = oa.helper_argv(SID, "echo hi")
    assert argv[:3] == ["docker", "run", "--rm"]
    assert _flag_values(argv, "--network") == ["none"]
    assert "--read-only" in argv and _flag_values(argv, "--log-driver") == ["none"]
    assert "-t" not in argv and "-it" not in argv and "-i" not in argv
    assert "-p" not in argv and "--publish" not in argv
    assert _flag_values(argv, "-v") == [f"offline-agent-{SID}:{oa.MERGED_MOUNT}:ro,nocopy"]
    assert argv[-4:] == [oa.IMAGE_TAG, "bash", "-c", "echo hi"]


def test_helper_argv_host_mount():
    ro = oa.helper_argv(SID, "x", host=PROJECT)
    assert f"{PROJECT}:{oa.HOST_MOUNT}:ro" in _flag_values(ro, "-v")
    rw = oa.helper_argv(SID, "x", host=PROJECT, host_rw=True)
    assert f"{PROJECT}:{oa.HOST_MOUNT}" in _flag_values(rw, "-v")
    assert "-i" in oa.helper_argv(SID, "x", stdin=True)


@pytest.mark.parametrize("action", ["inspect", "log", "diff", "wip", "patch", "bundle"])
def test_git_script_neutralises_repo_config(action):
    s = oa.render_git_script(action, BASE)
    assert "core.fsmonitor=false" in s and "core.hooksPath=/dev/null" in s
    assert "GIT_OPTIONAL_LOCKS=0" in s
    for cmd in ("g log", "g diff", "g format-patch"):
        for line in (l for l in s.splitlines() if cmd in l):
            assert "--no-ext-diff" in line and "--no-textconv" in line


def test_git_script_unknown_action():
    with pytest.raises(ValueError):
        oa.render_git_script("push", BASE)


def _digest(path):
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        h.update(str(p.relative_to(path)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def _session_repo(tmp_path):
    """A host repo with one commit, and a 'merged' clone the agent worked in."""
    host = _repo(tmp_path / "host")
    (host / "a.txt").write_text("one\n")
    _git(host, "add", "a.txt")
    _git(host, "commit", "-qm", "base")
    base = _git(host, "rev-parse", "HEAD")
    merged = tmp_path / "merged"
    subprocess.run(["git", "clone", "-q", str(host), str(merged)], check=True)
    _git(merged, "config", "user.name", "Agent")
    _git(merged, "config", "user.email", "agent@example.com")
    (merged / "b.txt").write_text("agent\n")
    _git(merged, "add", "b.txt")
    _git(merged, "commit", "-qm", "agent work")
    return host, merged, base


def _run_git_script(action, base, root, binary=False):
    return subprocess.run(
        ["bash", "-c", oa.render_git_script(action, base, root=str(root))],
        check=True, capture_output=True, text=not binary,
    ).stdout


@needs_git
def test_git_scripts_against_a_real_repo(tmp_path):
    host, merged, base = _session_repo(tmp_path)
    (merged / "a.txt").write_text("one\nedited\n")
    (merged / "untracked.txt").write_text("new file\n")
    before = _digest(merged / ".git")

    facts = oa.parse_kv(_run_git_script("inspect", base, merged))
    assert facts["HEAD"] == _git(merged, "rev-parse", "HEAD")
    assert facts["ANCESTOR"] == "yes" and facts["DIRTY"] == "2"

    wip = _run_git_script("wip", base, merged)
    assert "+edited" in wip and "untracked.txt" in wip and "+new file" in wip
    diff = _run_git_script("diff", base, merged)
    assert "agent work" in diff and "b.txt" in diff and "untracked.txt" in diff
    patch = _run_git_script("patch", base, merged)
    assert "Subject: [PATCH] agent work" in patch and "base" not in patch.split("Subject:")[1].splitlines()[0]

    # None of it wrote to the agent's repo (the helper mounts it read-only).
    assert _digest(merged / ".git") == before

    bundle = tmp_path / "s.bundle"
    bundle.write_bytes(_run_git_script("bundle", base, merged, binary=True))
    _git(host, "fetch", "-q", str(bundle), "HEAD")
    _git(host, "merge", "--ff-only", "-q", "FETCH_HEAD")
    assert _git(host, "rev-parse", "HEAD") == facts["HEAD"]


@needs_git
def test_git_scripts_unborn_base(tmp_path):
    merged = _repo(tmp_path / "merged")
    assert oa.parse_kv(_run_git_script("inspect", "", merged))["HEAD"] == ""
    (merged / "x").write_text("x\n")
    assert "+x" in _run_git_script("wip", "", merged)
    _git(merged, "add", "x")
    _git(merged, "commit", "-qm", "first")
    assert "Subject: [PATCH] first" in _run_git_script("patch", "", merged)
    host = _repo(tmp_path / "host")
    bundle = tmp_path / "s.bundle"
    bundle.write_bytes(_run_git_script("bundle", "", merged, binary=True))
    _git(host, "fetch", "-q", str(bundle), "HEAD")
    _git(host, "merge", "--ff-only", "-q", "FETCH_HEAD")
    assert _git(host, "log", "--format=%s") == "first"


@needs_git
def test_git_scripts_ignore_hostile_repo_config(tmp_path):
    host, merged, base = _session_repo(tmp_path)
    pwned = tmp_path / "pwned"
    (merged / "a.txt").write_text("changed\n")
    hook = merged / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {pwned}\n")
    hook.chmod(0o755)
    _git(merged, "config", "core.fsmonitor", f"touch {pwned}; false")
    _git(merged, "config", "diff.external", f"touch {pwned}; true")
    _git(merged, "config", "core.pager", f"touch {pwned}; cat")
    (merged / ".gitattributes").write_text("*.txt diff=evil\n")
    _git(merged, "config", "diff.evil.textconv", f"touch {pwned}; cat")
    for action in ("inspect", "diff", "wip", "patch", "bundle"):
        _run_git_script(action, base, merged, binary=True)
    assert not pwned.exists()
    assert "+changed" in _run_git_script("wip", base, merged)


@pytest.mark.parametrize("kw,verdict", [
    (dict(host_head="b" * 40), "refuse"),                    # host moved
    (dict(dirty=1), "refuse"),                               # uncommitted work
    (dict(merged_head=BASE), "noop"),                        # agent made no commits
    (dict(ancestor=False), "refuse"),                        # history rewritten
    (dict(), "apply"),
    (dict(base="", host_head="", ancestor=None), "apply"),   # unborn host repo
    (dict(base="", host_head="", merged_head=""), "noop"),
])
def test_git_apply_decision(kw, verdict):
    args = dict(base=BASE, host_head=BASE, merged_head="c" * 40, ancestor=True, dirty=0)
    args.update(kw)
    decision, message = oa.git_apply_decision(**args)
    assert decision == verdict
    assert message


@pytest.mark.parametrize("kw,unexported", [
    (dict(), True),
    (dict(dirty=3), True),
    (dict(merged_head=BASE), False),
    (dict(host_contains=True), False),
    (dict(host_contains=True, dirty=1), True),
    (dict(base="", merged_head=""), False),
])
def test_git_session_unexported(kw, unexported):
    args = dict(base=BASE, merged_head="c" * 40, dirty=0, host_contains=False)
    args.update(kw)
    assert oa.git_session_unexported(**args) is unexported


def test_parse_session_info():
    text = (
        "EXISTS=yes\nMODE=git\nBASE=" + BASE + "\nHAS_MODE=yes\n"
        "PROJECT=/Users/me/My Project\nMANIFEST=no\nLEGACY=no\n"
    )
    info = oa.parse_session_info(text, SID, None)
    assert info.exists and not info.legacy and info.baseline and not info.manifest
    assert (info.mode, info.base, info.project) == ("git", BASE, PROJECT)


def test_parse_session_info_missing_and_pre_baseline(tmp_path):
    assert not oa.parse_session_info("EXISTS=no\n", SID, PROJECT).exists
    # Sessions from before Phase 4 have no mode marker: mode is inferred from
    # the host, and there is no baseline to apply against.
    info = oa.parse_session_info("EXISTS=yes\nHAS_MODE=no\nPROJECT=\n", SID, tmp_path)
    assert (info.mode, info.baseline, info.project) == ("dir", False, tmp_path)


def test_session_info_script_reads_markers():
    s = oa.render_session_info_script(SID)
    for marker in ("mode", "base", "path", "manifest", "disk.img", "upper"):
        assert f"{D}/{marker}" in s


def test_session_table_parse_and_format():
    text = (
        f"{SID}\tgit\tmounted\t2048\t5368709120\t/Users/me/My Project\n"
        "0123456789ab\t\tlegacy\t10\t\t/Users/me/old\n"
    )
    rows = oa.parse_session_table(text)
    assert rows[0]["path"] == "/Users/me/My Project" and rows[1]["state"] == "legacy"
    out = oa.format_session_table(rows, running={f"offline-agent-{SID}"})
    first, second = out.splitlines()[1:3]
    assert SID in first and "2.0M / 5.0G" in first and "running" in first
    assert "legacy" in second and "?" in second and "running" not in second


def test_discard_script():
    s = oa.render_discard_script(SID)
    lines = s.splitlines()
    umount = next(l for l in lines if "umount" in l)
    assert f"mountpoint -q {D}/mnt" in umount
    assert any(l.strip() == f"sudo rm -rf {D}" for l in lines)
    assert oa.DISCARDED_MARKER in s
    with pytest.raises(ValueError):
        oa.render_discard_script("../../etc")


def _body(name):
    body = _SOURCE[_SOURCE.index(f"def {name}("):]
    return body[:body.index("\ndef ")]


def test_apply_and_discard_refuse_while_session_runs():
    for name in ("cmd_apply", "cmd_discard"):
        assert "require_session_stopped" in _body(name)
    assert "docker_container_running" in _body("require_session_stopped")


def test_git_apply_runs_host_git_with_fsck_and_ff_only():
    body = _body("apply_git")
    assert "transfer.fsckObjects=true" in body and "--ff-only" in body
    assert "FETCH_HEAD" in body and "git_apply_decision" in body
    # The bundle comes out of a helper with no host mount.
    assert "host=" not in body


def test_dir_apply_checks_conflicts_before_writing():
    body = _body("apply_dir")
    assert body.index("find_conflicts") < body.index("host_rw=True")
    assert body.index("unsupported_names") < body.index("host_rw=True")


def test_export_refuses_to_overwrite():
    assert '"xb"' in _body("_write_new")


# ── Phase 4: command flows, with docker and colima ssh replaced by local bash ──

class _LocalVm:
    """Run helper scripts and VM scripts locally: /merged, /host and SESSIONS_ROOT
    are rewritten to temp dirs, sudo is dropped."""

    def __init__(self, tmp_path, merged, host):
        self.sessions = tmp_path / "sessions"
        self.merged, self.host = merged, host
        self.helper_calls = []

    def _local(self, script):
        return script.replace(oa.SESSIONS_ROOT, str(self.sessions)).replace("sudo ", "")

    def vm_sh(self, cmd, *, profile=None, check=True):
        return subprocess.run(["bash"], input=self._local(cmd), text=True, capture_output=True)

    def run_helper(self, sid, script, *, host=None, host_rw=False, stdin=None):
        self.helper_calls.append((script, host, host_rw))
        local = script.replace(oa.MERGED_MOUNT, str(self.merged)).replace(oa.HOST_MOUNT, str(self.host))
        r = subprocess.run(["bash", "-c", local], input=stdin, capture_output=True)
        assert r.returncode == 0, r.stderr.decode()
        return r.stdout


@pytest.fixture
def local_vm(tmp_path, monkeypatch):
    def make(merged, host):
        vm = _LocalVm(tmp_path, merged, host)
        monkeypatch.setattr(oa, "vm_sh", vm.vm_sh)
        monkeypatch.setattr(oa, "run_helper", vm.run_helper)
        monkeypatch.setattr(oa, "docker_container_running", lambda name: False)
        monkeypatch.setattr(oa, "_confirm", lambda prompt: True)
        return vm
    return make


def _git_info(host, base):
    return oa.SessionInfo(sid=SID, project=host, exists=True, mode="git", base=base, baseline=True)


@needs_git
def test_cmd_apply_git_fast_forwards_host(tmp_path, local_vm):
    host, merged, base = _session_repo(tmp_path)
    local_vm(merged, host)
    info = _git_info(host, base)
    oa.cmd_apply(info, _config(), dry_run=True)
    assert _git(host, "rev-parse", "HEAD") == base
    oa.cmd_apply(info, _config(), dry_run=False)
    assert _git(host, "rev-parse", "HEAD") == _git(merged, "rev-parse", "HEAD")
    assert (host / "b.txt").read_text() == "agent\n"
    # Now on the host, so the session is safe to discard.
    assert oa.session_unexported(info) is None


@needs_git
def test_cmd_apply_git_refuses_moved_host_and_dirty_session(tmp_path, local_vm):
    host, merged, base = _session_repo(tmp_path)
    local_vm(merged, host)
    (merged / "a.txt").write_text("uncommitted\n")
    with pytest.raises(SystemExit, match="uncommitted"):
        oa.cmd_apply(_git_info(host, base), _config(), dry_run=False)
    _git(host, "commit", "-q", "--allow-empty", "-m", "host moved")
    with pytest.raises(SystemExit, match="HEAD moved"):
        oa.cmd_apply(_git_info(host, base), _config(), dry_run=False)
    assert "agent work" not in _git(host, "log", "--format=%s")
    assert oa.session_unexported(_git_info(host, base))


def _dir_session(tmp_path, local_vm):
    host, merged = tmp_path / "H", tmp_path / "M"
    host.mkdir()
    for name in ("keep", "mod", "del"):
        (host / name).write_text(name)
    vm = local_vm(merged, host)
    d = vm.sessions / SID
    d.mkdir(parents=True)
    # What render_session_setup_script records at creation.
    subprocess.run(
        ["bash", "-c", f"{oa.manifest_find_command(host)} > {d}/manifest"], check=True,
    )
    shutil.copytree(host, merged)
    (merged / "mod").write_text("mod2")
    (merged / "del").unlink()
    (merged / "new").write_text("new")
    info = oa.SessionInfo(sid=SID, project=host, exists=True, mode="dir", baseline=True, manifest=True)
    return host, merged, info, vm


@needs_rsync
def test_cmd_apply_dir_syncs_host(tmp_path, local_vm):
    host, merged, info, vm = _dir_session(tmp_path, local_vm)
    oa.cmd_apply(info, _config(), dry_run=True)
    assert (host / "del").exists() and (host / "mod").read_text() == "mod"
    oa.cmd_apply(info, _config(), dry_run=False)
    assert _tree(host) == _tree(merged)
    # Only the final rsync got the host read-write.
    assert [rw for _, _, rw in vm.helper_calls].count(True) == 1
    assert oa.session_unexported(info) is None


@needs_rsync
def test_cmd_apply_dir_refuses_host_side_conflict(tmp_path, local_vm):
    host, merged, info, _ = _dir_session(tmp_path, local_vm)
    (host / "mod").write_text("host edit, different size")
    with pytest.raises(SystemExit, match="changed on the host"):
        oa.cmd_apply(info, _config(), dry_run=False)
    assert (host / "mod").read_text() == "host edit, different size"
    assert (host / "del").exists()


@needs_rsync
def test_cmd_export_dir(tmp_path, local_vm):
    host, merged, info, _ = _dir_session(tmp_path, local_vm)
    out = tmp_path / "out.tar"
    oa.cmd_export(info, out)
    names = subprocess.run(["tar", "-tf", str(out)], check=True, capture_output=True, text=True).stdout.split()
    assert sorted(names) == ["mod", "new"]
    assert (tmp_path / "out.tar.deleted").read_text() == "del\n"
    with pytest.raises(SystemExit, match="already exists"):
        oa.cmd_export(info, out)


@needs_git
def test_cmd_export_git(tmp_path, local_vm):
    host, merged, base = _session_repo(tmp_path)
    local_vm(merged, host)
    (merged / "c.txt").write_text("wip\n")
    out = tmp_path / "s.patch"
    oa.cmd_export(_git_info(host, base), out)
    assert "Subject: [PATCH] agent work" in out.read_text()
    assert "+wip" in (tmp_path / "s.patch.wip.diff").read_text()
    # The exported patch applies on the host with git am.
    _git(host, "am", "-q", str(out))
    assert (host / "b.txt").read_text() == "agent\n"
