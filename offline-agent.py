#!/usr/bin/env python3
"""
offline-agent.py — run a local-model agent with no egress over a copy-on-write
view of the current directory.

Creates a dedicated Colima VM (profile: offline-agent) and drops you into a
shell in an ephemeral container with opencode and pi wired to the local
inference server on the macOS host. That server is the only destination the
container can reach: no proxy, no DNS, no other VM service.

The directory you run this from is mounted at its real path through an
overlayfs volume: the host directory is the read-only lower layer, and every
write the agent makes (including git commits) lands in an upper layer on the
VM's disk, keyed by the directory's path. The host directory is untouched.
Overlay state survives container exits, script re-runs, and colima stop/start.

Host-side state lives in ~/.offline-agent/:
  opencode/config/    opencode.json (provider injected on every run)
  opencode/data/      opencode sessions and auth
  pi/                 pi models.json + settings.json (provider injected on every run)

Usage:
  cd ~/some/project && offline-agent.py     start (or attach to) this directory's session
  offline-agent.py --rebuild                rebuild the image (optionally the VM too)
  offline-agent.py --backend=ollama         use Ollama instead of omlx

Adding tools: edit the user-packages block at the end of
dockerfiles/offline-agent.Dockerfile, then run --rebuild.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Tuple


# ── Constants ──────────────────────────────────────────────────────────────────

DOCKERFILE = Path(__file__).parent / "dockerfiles" / "offline-agent.Dockerfile"

COLIMA_PROFILE = "offline-agent"
IMAGE_TAG = "offline-agent:latest"
NET_NAME = "offline-agent-net"
# TEST-NET-1: never routed, and REJECTed by the OFFLINE_AGENT chain, so every
# external lookup fails fast instead of timing out.
DNS_BLACKHOLE = "192.0.2.1"
# Per-session overlay upper/work dirs on the VM's native disk (overlayfs
# forbids a network filesystem as the upper layer).
SESSIONS_ROOT = "/var/lib/offline-agent/sessions"
OVERLAY_UNSAFE_CHARS = ",:\\\n"

DEFAULT_MEMORY_GIB = 2
DEFAULT_CPUS = 2


# ── Paths ──────────────────────────────────────────────────────────────────────

@dataclass
class Paths:
    base: Path = field(default_factory=lambda: Path.home() / ".offline-agent")

    @property
    def opencode_config_dir(self) -> Path:
        return self.base / "opencode" / "config"

    @property
    def opencode_data_dir(self) -> Path:
        return self.base / "opencode" / "data"

    @property
    def pi_dir(self) -> Path:
        return self.base / "pi"

    @property
    def opencode_config_file(self) -> Path:
        return self.opencode_config_dir / "opencode.json"

    @property
    def pi_models_file(self) -> Path:
        return self.pi_dir / "agent" / "models.json"

    @property
    def pi_settings_file(self) -> Path:
        return self.pi_dir / "agent" / "settings.json"


# ── VmConfig ───────────────────────────────────────────────────────────────────

@dataclass
class VmConfig:
    profile_name: str = COLIMA_PROFILE
    memory_gib: int = DEFAULT_MEMORY_GIB
    cpus: int = DEFAULT_CPUS
    backend: str = "omlx"
    # Populated by discover_network() / ensure_docker_network():
    bridge_ip: Optional[str] = None
    bridge_cidr: Optional[str] = None
    host_ip: Optional[str] = None
    net_cidr: Optional[str] = None

    @property
    def inference_port(self) -> int:
        return 11434 if self.backend == "ollama" else 8000

    @property
    def inference_label(self) -> str:
        return "Ollama" if self.backend == "ollama" else "omlx"


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="offline-agent.py",
        description=(
            "Run opencode/pi against a local model in a no-egress container, "
            "over a copy-on-write view of the current directory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ISOLATION:
  The container reaches only the local inference server on the macOS host.
  The current directory is the read-only lower layer of an overlay; the
  agent's writes stay in the offline-agent VM and never reach the host
  directory. The current directory must be under $HOME (Colima mounts only
  $HOME into the VM) and must not be $HOME itself.

ADDING PACKAGES:
  Edit the user-packages block at the end of
  dockerfiles/offline-agent.Dockerfile, then run with --rebuild.

ENVIRONMENT:
  OFFLINE_AGENT_MEMORY         Default VM memory GiB (overridden by --memory)
  OFFLINE_AGENT_CPUS           Default VM CPU count (overridden by --cpus)
  OFFLINE_AGENT_BACKEND        Default backend: omlx (default) or ollama
  OFFLINE_AGENT_DEFAULT_MODEL  Default model for opencode and pi
  OMLX_API_KEY                 API key for the omlx backend
  GIT_USER_NAME, GIT_USER_EMAIL  Commit identity inside the container
""",
    )
    p.add_argument(
        "--rebuild",
        action="store_true",
        help="Remove the image (and any running session containers) and rebuild. "
             "With confirmation, also delete the Colima VM.",
    )
    p.add_argument(
        "--backend",
        choices=["omlx", "ollama"],
        default=os.environ.get("OFFLINE_AGENT_BACKEND", "omlx"),
        help="Local inference backend (default: omlx).",
    )
    p.add_argument(
        "--memory",
        type=_parse_gib,
        default=_parse_gib(os.environ.get("OFFLINE_AGENT_MEMORY", str(DEFAULT_MEMORY_GIB))),
        metavar="GIB",
        help=f"VM memory in GiB (default: {DEFAULT_MEMORY_GIB}).",
    )
    p.add_argument(
        "--cpus",
        type=int,
        default=int(os.environ.get("OFFLINE_AGENT_CPUS", str(DEFAULT_CPUS))),
        metavar="N",
        help=f"VM CPU count (default: {DEFAULT_CPUS}).",
    )
    return p.parse_args(argv)


def _parse_gib(raw: str) -> int:
    """Accept '2', '2G', '2GB', '2GiB' and return integer GiB."""
    cleaned = raw.strip().lower().rstrip("b").rstrip("i").rstrip("g")
    try:
        return int(cleaned)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid memory value {raw!r} — use integer GiB (e.g. 2, 2G, 2GB)"
        )


# ── Pure helpers ───────────────────────────────────────────────────────────────

class ProjectError(Exception):
    """The current directory cannot be served as an overlay lower layer."""


def resolve_project(path: Path, home: Path) -> Path:
    """Return the real path of the project dir, or raise ProjectError.

    The overlay's lower layer is the host path as Colima mounts it in the VM,
    and Colima mounts only $HOME. $HOME itself is refused: the agent would see
    every credential and dotfile the user owns.
    """
    project = path.resolve()
    home = home.resolve()
    if project == home:
        raise ProjectError(
            f"refusing to run in $HOME itself ({home}); cd into a project directory under it"
        )
    if home not in project.parents:
        raise ProjectError(
            f"{project} is not under $HOME ({home}). Colima mounts only $HOME into "
            f"the VM; move or clone the project somewhere under {home}."
        )
    bad = sorted({c for c in str(project) if c in OVERLAY_UNSAFE_CHARS})
    if bad:
        raise ProjectError(
            f"{project} contains the character(s) {' '.join(repr(c) for c in bad)}, "
            "which cannot appear in an overlay mount option. Rename the directory."
        )
    return project


def session_id(project: Path) -> str:
    """Short, stable id for a project path; keys the overlay volume and VM dirs."""
    return hashlib.sha256(str(project).encode()).hexdigest()[:12]


def session_dir(sid: str) -> str:
    return f"{SESSIONS_ROOT}/{sid}"


def volume_name(sid: str) -> str:
    return f"offline-agent-{sid}"


def container_name(sid: str) -> str:
    return f"offline-agent-{sid}"


def render_session_setup_script(sid: str, project: Path) -> str:
    """Shell script that creates the session's upper/work dirs and path marker.

    The overlay mount fails unless upper and work already exist (Phase 1).
    The `path` marker maps the opaque id back to the project for listings.
    """
    d = session_dir(sid)
    return (
        "set -e\n"
        f"sudo mkdir -p {d}/upper {d}/work\n"
        f"printf '%s\\n' {shlex.quote(str(project))} | sudo tee {d}/path >/dev/null\n"
    )


def volume_create_argv(sid: str, project: Path) -> List[str]:
    """`docker volume create` argv for the session's overlay volume.

    The local driver mounts overlayfs when a container first uses the volume;
    lowerdir is the project path, which Colima mounts at the same path in the VM.
    """
    d = session_dir(sid)
    return [
        "docker", "volume", "create",
        "--driver", "local",
        "--opt", "type=overlay",
        "--opt", "device=overlay",
        "--opt", f"o=lowerdir={project},upperdir={d}/upper,workdir={d}/work",
        volume_name(sid),
    ]


def parse_session_listing(text: str) -> List[Tuple[str, str]]:
    """Parse `<id>\\t<path>` lines (see list_sessions) into (id, path) pairs."""
    sessions = []
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        sid, _, path = line.partition("\t")
        sessions.append((sid.strip(), path))
    return sessions


def running_vm_size(info: Mapping[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """(cpus, memory GiB) from `colima list --json`; memory is reported in bytes."""
    cpus = info.get("cpus")
    mem = info.get("memory")
    return (
        int(cpus) if cpus not in (None, "") else None,
        int(mem) // (1024 ** 3) if mem not in (None, "") else None,
    )


def render_iptables_apply_script(
    bridge_cidr: str,
    net_cidr: str,
    host_ip: str,
    inference_port: int,
) -> str:
    """Return a shell script that applies the OFFLINE_AGENT chains.

    OFFLINE_AGENT (from DOCKER-USER, i.e. FORWARD) lets out replies and the
    inference endpoint and REJECTs everything else, DNS included. There is no
    proxy, so OFFLINE_AGENT_IN (from INPUT) REJECTs every new connection from a
    container to the VM itself (sshd, dnsmasq, docker0 gateway). Both chains
    are replaced in one `iptables-restore --noflush` commit, so neither is
    ever empty mid-apply (an empty chain falls through and allows all).
    """
    jumps = ""
    for cidr in dict.fromkeys((bridge_cidr, net_cidr)):
        jumps += (
            f"iptables -C DOCKER-USER -s {cidr} -j OFFLINE_AGENT 2>/dev/null \\\n"
            f"  || iptables -I DOCKER-USER 1 -s {cidr} -j OFFLINE_AGENT\n"
            f"iptables -C INPUT -s {cidr} -j OFFLINE_AGENT_IN 2>/dev/null \\\n"
            f"  || iptables -I INPUT 1 -s {cidr} -j OFFLINE_AGENT_IN\n"
        )
    return f"""\
#!/bin/sh
set -e

# Ensure DOCKER-USER exists (docker creates it on fresh daemons).
iptables -N DOCKER-USER 2>/dev/null || true
iptables -C FORWARD -j DOCKER-USER 2>/dev/null || iptables -I FORWARD 1 -j DOCKER-USER

# Create-or-replace both chains atomically, rules in order.
iptables-restore --noflush <<'RULES'
*filter
:OFFLINE_AGENT - [0:0]
:OFFLINE_AGENT_IN - [0:0]
-A OFFLINE_AGENT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
-A OFFLINE_AGENT -d {host_ip} -p tcp --dport {inference_port} -j RETURN
-A OFFLINE_AGENT -j REJECT --reject-with icmp-admin-prohibited
-A OFFLINE_AGENT_IN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
-A OFFLINE_AGENT_IN -j REJECT --reject-with icmp-admin-prohibited
COMMIT
RULES

# Jumps by source CIDR, for the default bridge and offline-agent-net (idempotent;
# never removed, so adding them after the chains are populated opens no window).
{jumps}"""


def _qualify(model: str, provider_key: str) -> str:
    return model if "/" in model else f"{provider_key}/{model}"


def merge_opencode_config(
    data: Mapping[str, Any],
    *,
    backend: str,
    runtime_url: str,
    api_key: str,
    discovered: List[str],
    default_model: str,
) -> dict:
    """Return opencode.json with the local provider injected and offline switches set.

    Ported from start-agent.sh's opencode injection. Differences: only the
    active local provider is enabled (hides the bundled cloud models, which
    cannot work offline), autoupdate is off, and webfetch/websearch are
    forced to deny. User keys are otherwise preserved; the input is not mutated.
    """
    data = copy.deepcopy(dict(data))
    data.setdefault("$schema", "https://opencode.ai/config.json")
    provider_key = backend
    entry = data.setdefault("provider", {}).setdefault(provider_key, {})
    entry.setdefault("npm", "@ai-sdk/openai-compatible")
    entry.setdefault("name", "Ollama (host)" if backend == "ollama" else "omlx (host)")
    opts = entry.setdefault("options", {})
    opts["baseURL"] = runtime_url
    # @ai-sdk/openai-compatible requires a non-empty apiKey in some versions.
    if backend == "ollama":
        opts["apiKey"] = "ollama"
    elif api_key:
        opts["apiKey"] = api_key
    else:
        opts.setdefault("apiKey", "omlx")

    if discovered:
        # Always refresh on success — stale model lists are worse than useless.
        entry["models"] = {m: {"name": m} for m in discovered}

    # Env override wins; otherwise reset to a local model whenever the persisted
    # `model` isn't from the active provider (e.g. a stale cloud selection).
    if default_model:
        data["model"] = _qualify(default_model, provider_key)
    elif discovered and (not data.get("model") or data["model"].split("/")[0] != provider_key):
        data["model"] = _qualify(discovered[0], provider_key)

    data["autoupdate"] = False
    data["enabled_providers"] = [provider_key]
    perms = data.setdefault("permission", {})
    perms["webfetch"] = "deny"
    perms["websearch"] = "deny"
    return data


def merge_pi_config(
    models_data: Mapping[str, Any],
    settings_data: Mapping[str, Any],
    *,
    backend: str,
    runtime_url: str,
    api_key: str,
    discovered: List[str],
    default_model: str,
) -> Tuple[dict, dict]:
    """Return (models.json, settings.json) with the `local` provider injected.

    Ported from start-agent.sh's pi injection. Phase 1 found pi makes no
    update or telemetry calls, so no offline switches are needed.
    """
    models_data = copy.deepcopy(dict(models_data))
    settings_data = copy.deepcopy(dict(settings_data))
    provider_key = "local"
    entry = models_data.setdefault("providers", {}).setdefault(provider_key, {})
    entry["name"] = "Local (Ollama)" if backend == "ollama" else "Local (omlx)"
    entry["baseUrl"] = runtime_url
    entry["api"] = "openai-completions"
    if backend == "ollama":
        entry["apiKey"] = "ollama"
    elif api_key:
        entry["apiKey"] = api_key
    else:
        entry.setdefault("apiKey", "omlx")
    entry["compat"] = {
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
    }
    if discovered:
        entry["models"] = [{"id": m} for m in discovered]

    current_ids = [m["id"] for m in entry.get("models", [])]
    if default_model:
        settings_data["defaultProvider"] = provider_key
        settings_data["defaultModel"] = default_model
    elif current_ids and (
        not settings_data.get("defaultModel")
        or settings_data.get("defaultProvider") != provider_key
    ):
        settings_data["defaultProvider"] = provider_key
        settings_data["defaultModel"] = current_ids[0]
    return models_data, settings_data


def container_env_args(config: VmConfig, env: Mapping[str, str]) -> List[str]:
    """`-e` args shared by docker run and docker exec.

    No proxy variables: there is no proxy. Cloud API keys are not forwarded:
    there is no route to a cloud API. OMLX_API_KEY is passed value-less so
    docker reads it from its own environment, keeping it out of the argv.
    """
    git_name = env.get("GIT_USER_NAME") or "Dev"
    git_email = env.get("GIT_USER_EMAIL") or "dev@localhost"
    args = [
        "-e", f"TERM={env.get('TERM') or 'xterm-256color'}",
        "-e", f"COLORTERM={env.get('COLORTERM', '')}",
        "-e", f"TERM_PROGRAM={env.get('TERM_PROGRAM', '')}",
        "-e", f"GIT_AUTHOR_NAME={git_name}",
        "-e", f"GIT_AUTHOR_EMAIL={git_email}",
        "-e", f"GIT_COMMITTER_NAME={git_name}",
        "-e", f"GIT_COMMITTER_EMAIL={git_email}",
        "-e", "TMPDIR=/tmp",
    ]
    inference_url = f"http://{config.host_ip}:{config.inference_port}"
    if config.backend == "ollama":
        args += ["-e", f"OLLAMA_HOST={inference_url}"]
    else:
        args += ["-e", f"OMLX_HOST={inference_url}"]
        if env.get("OMLX_API_KEY"):
            args += ["-e", "OMLX_API_KEY"]
    return args


def docker_run_argv(
    project: Path, sid: str, config: VmConfig, paths: Paths, env: Mapping[str, str]
) -> List[str]:
    """argv for the session container: ephemeral, no published ports, no egress."""
    return [
        "docker", "run",
        "--rm", "-it",
        "--name", container_name(sid),
        "--network", NET_NAME,
        "--dns", DNS_BLACKHOLE,
        # The VM's default-route gateway (the macOS host), not Docker's
        # host-gateway alias, which under Colima resolves to the VM's own
        # bridge gateway. Same IP as the firewall RETURN rule.
        "--add-host", f"host.docker.internal:{config.host_ip}",
        # nocopy: never copy image content into the overlay on first mount.
        "-v", f"{volume_name(sid)}:{project}:nocopy",
        "-v", f"{paths.opencode_config_dir}:/root/.config/opencode",
        "-v", f"{paths.opencode_data_dir}:/root/.local/share/opencode",
        "-v", f"{paths.pi_dir}:/root/.pi",
        "-w", str(project),
        *container_env_args(config, env),
        IMAGE_TAG,
        "bash",
    ]


def docker_exec_argv(project: Path, sid: str, config: VmConfig, env: Mapping[str, str]) -> List[str]:
    """argv for a second shell in an already-running session container."""
    return [
        "docker", "exec", "-it",
        "-w", str(project),
        *container_env_args(config, env),
        container_name(sid),
        "bash",
    ]


# ── Subprocess wrappers ────────────────────────────────────────────────────────

def vm_sh(cmd: str, *, profile: str = COLIMA_PROFILE, check: bool = True) -> subprocess.CompletedProcess:
    """Run a raw shell command string inside the Colima VM via stdin pipe.

    Piping via stdin bypasses colima's argv-join quoting so shell pipelines,
    redirections, and heredocs work correctly on the remote side.
    """
    return subprocess.run(
        ["colima", "ssh", "-p", profile, "--", "bash"],
        input=cmd,
        text=True,
        capture_output=True,
        check=check,
    )


def colima_info(profile: str = COLIMA_PROFILE) -> Optional[dict]:
    result = subprocess.run(
        ["colima", "list", "-p", profile, "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        info = json.loads(result.stdout.strip().splitlines()[0])
    except json.JSONDecodeError:
        return None
    return info if isinstance(info, dict) else None


def colima_profile_running(profile: str = COLIMA_PROFILE) -> bool:
    info = colima_info(profile)
    return bool(info) and info.get("status") == "Running"


def docker_container_running(name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def in_background(fn: Callable[[], Any]) -> Callable[[], Any]:
    """Run fn on a daemon thread; return a function that joins and yields its result."""
    box: list = [None]

    def _run() -> None:
        box[0] = fn()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    def join() -> Any:
        thread.join()
        return box[0]

    return join


# ── Phase functions ────────────────────────────────────────────────────────────

def ensure_colima_vm(config: VmConfig) -> None:
    info = colima_info(config.profile_name)
    if info and info.get("status") == "Running":
        running_cpus, running_gib = running_vm_size(info)
        if running_cpus is not None and running_cpus != config.cpus:
            print(
                f"warning: running VM has {running_cpus} CPUs; requested {config.cpus}. "
                "Use --rebuild to resize.",
                file=sys.stderr,
            )
        if running_gib is not None and running_gib != config.memory_gib:
            print(
                f"warning: running VM has {running_gib} GiB; requested {config.memory_gib} GiB. "
                "Use --rebuild to resize.",
                file=sys.stderr,
            )
        return

    print(
        f"==> Starting Colima VM '{config.profile_name}' "
        f"({config.memory_gib} GiB RAM, {config.cpus} CPUs)"
    )
    # No --mount: Colima's default mounts $HOME at the same path, which is
    # exactly the overlay lower layer we need, so switching projects never
    # restarts the VM.
    subprocess.run(
        [
            "colima", "start", "-p", config.profile_name,
            "--vm-type", "vz",
            "--runtime", "docker",
            "--cpu", str(config.cpus),
            "--memory", str(config.memory_gib),
            "--mount-type", "virtiofs",
            "--network-address",
        ],
        check=True,
    )


def ensure_docker_context(profile: str = COLIMA_PROFILE) -> None:
    """Point this process's docker CLI at the profile's daemon.

    Set via DOCKER_CONTEXT for this process and its children only, so the
    user's selected context (and the sibling VMs) are left alone. DOCKER_HOST
    would take precedence over the context, so drop it.
    """
    os.environ.pop("DOCKER_HOST", None)
    os.environ["DOCKER_CONTEXT"] = f"colima-{profile}"


def probe_vm(config: VmConfig) -> dict:
    """Gather every read-only VM fact the bring-up needs in one colima ssh call."""
    script = r"""#!/bin/sh
bridge_info=$(docker network inspect bridge \
  -f '{{(index .IPAM.Config 0).Gateway}} {{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)
echo "BRIDGE_IP=${bridge_info%% *}"
echo "BRIDGE_CIDR=${bridge_info##* }"

# Default-route host IP; fall back to getent if the route table is empty.
host_ip=$(ip route show default 2>/dev/null | awk '/^default/ {print $3; exit}')
if [ -z "$host_ip" ]; then
  for candidate in host.lima.internal host.docker.internal; do
    host_ip=$(getent hosts "$candidate" 2>/dev/null | awk '{print $1; exit}')
    [ -n "$host_ip" ] && break
  done
fi
echo "HOST_IP=$host_ip"

echo "NET_CIDR=$(docker network inspect %NET% -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)"

if docker image inspect %IMAGE% >/dev/null 2>&1; then
  echo "IMAGE_PRESENT=true"
else
  echo "IMAGE_PRESENT=false"
fi
""".replace("%NET%", NET_NAME).replace("%IMAGE%", IMAGE_TAG)

    result = vm_sh(script, profile=config.profile_name, check=False)
    facts: dict = {}
    for line in result.stdout.replace("\r", "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            facts[key] = value.strip()
    return facts


def discover_network(config: VmConfig, facts: dict) -> None:
    """Populate bridge IP/CIDR and host IP on config from the probe facts."""
    config.bridge_ip = facts.get("BRIDGE_IP") or "172.17.0.1"
    config.bridge_cidr = facts.get("BRIDGE_CIDR") or "172.17.0.0/16"
    host_ip = facts.get("HOST_IP", "")
    if not host_ip:
        # Without the host IP there is no inference carve-out, and a guessed
        # address could open the wrong destination. Stop here.
        raise SystemExit(
            "error: could not determine the macOS host IP from inside the VM; "
            f"try `colima ssh -p {config.profile_name} -- ip route show default`."
        )
    config.host_ip = host_ip
    print(f"==> VM network: bridge={config.bridge_ip} cidr={config.bridge_cidr} host={host_ip}")


def ensure_docker_network(config: VmConfig, facts: dict) -> None:
    """Use the probed offline-agent-net CIDR; create the network if it's missing."""
    cidr = facts.get("NET_CIDR", "")
    if not cidr:
        vm_sh(f"docker network create {NET_NAME} >/dev/null", profile=config.profile_name)
        cidr = vm_sh(
            f"docker network inspect {NET_NAME} -f '{{{{(index .IPAM.Config 0).Subnet}}}}'",
            profile=config.profile_name,
            check=False,
        ).stdout.strip()
    if not cidr:
        # The firewall jump matches on this CIDR; guessing would leave the
        # container's real subnet unfiltered.
        raise SystemExit(f"error: could not determine the {NET_NAME} subnet; refusing to continue.")
    config.net_cidr = cidr
    print(f"==> Network: {NET_NAME} cidr={cidr}")


def apply_firewall(config: VmConfig) -> None:
    """Re-assert both iptables chains. Runs on every start; any failure is fatal."""
    assert config.bridge_cidr and config.net_cidr and config.host_ip
    script = render_iptables_apply_script(
        bridge_cidr=config.bridge_cidr,
        net_cidr=config.net_cidr,
        host_ip=config.host_ip,
        inference_port=config.inference_port,
    )
    result = subprocess.run(
        ["colima", "ssh", "-p", config.profile_name, "--", "sudo", "sh"],
        input=script,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"error: firewall apply failed (exit {result.returncode}); not starting the container.\n"
            f"{result.stderr.strip()}"
        )
    print(f"==> Firewall applied (egress: {config.host_ip}:{config.inference_port} only)")


def ensure_image(facts: dict) -> None:
    if facts.get("IMAGE_PRESENT") == "true":
        return
    print(f"==> Building {IMAGE_TAG} from {DOCKERFILE.name}")
    # --network=host: build steps run in the VM's network namespace, outside the
    # DOCKER-USER REJECT that would otherwise block apt/npm (ADR-011).
    subprocess.run(
        [
            "docker", "build",
            "--network=host",
            "-t", IMAGE_TAG,
            "-f", str(DOCKERFILE),
            str(DOCKERFILE.parent),
        ],
        check=True,
    )


def list_sessions(profile: str = COLIMA_PROFILE) -> List[Tuple[str, str]]:
    """(id, project path) for every session whose overlay upper dir is non-empty."""
    script = f"""\
for d in {SESSIONS_ROOT}/*/; do
  [ -d "$d" ] || continue
  if [ -n "$(sudo ls -A "$d/upper" 2>/dev/null)" ]; then
    printf '%s\\t%s\\n' "$(basename "$d")" "$(sudo cat "$d/path" 2>/dev/null)"
  fi
done
"""
    return parse_session_listing(vm_sh(script, profile=profile, check=False).stdout)


def rebuild_teardown(config: VmConfig) -> None:
    """Remove session containers and the image; optionally delete the Colima VM."""
    running = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=^offline-agent-"],
        capture_output=True, text=True,
    ).stdout.split()
    if running:
        print(f"==> --rebuild: removing {len(running)} session container(s) (overlay volumes are kept)")
        subprocess.run(["docker", "rm", "-f", *running], capture_output=True)
    print(f"==> --rebuild: removing image {IMAGE_TAG}")
    subprocess.run(["docker", "rmi", "-f", IMAGE_TAG], capture_output=True)

    print()
    sessions = list_sessions(config.profile_name)
    if sessions:
        print(
            f"WARNING: {len(sessions)} session(s) hold agent changes that exist only "
            "inside this VM. Deleting the VM destroys them:"
        )
        for sid, path in sessions:
            print(f"    {sid}  {path or '(unknown path)'}")
    answer = input(
        f"Also delete and recreate the Colima VM '{config.profile_name}'? "
        "This is NOT reversible. [y/N] "
    )
    if answer.strip().lower() in ("y", "yes"):
        print(f"==> Destroying Colima VM '{config.profile_name}'")
        subprocess.run(
            ["colima", "delete", "-p", config.profile_name, "--force"],
            capture_output=True,
        )
    else:
        print("==> Keeping Colima VM; only the image will be rebuilt.")


def ensure_session(config: VmConfig, project: Path, sid: str) -> None:
    """Create the session's VM-side dirs and its overlay volume (idempotent)."""
    vm_sh(render_session_setup_script(sid, project), profile=config.profile_name)
    exists = subprocess.run(
        ["docker", "volume", "inspect", volume_name(sid)], capture_output=True,
    ).returncode == 0
    if not exists:
        subprocess.run(volume_create_argv(sid, project), capture_output=True, check=True)
        print(f"==> Created overlay volume {volume_name(sid)}")


def probe_inference(config: VmConfig) -> Optional[str]:
    """Non-fatal probe from inside the VM; return a warning string on failure."""
    url = f"http://{config.host_ip}:{config.inference_port}"
    if config.backend == "ollama":
        cmd = f"curl -sf --max-time 3 {url}/api/tags"
    else:
        key = os.environ.get("OMLX_API_KEY", "")
        auth = f'-H "Authorization: Bearer {key}" ' if key else ""
        cmd = f"curl -sf --max-time 3 {auth}{url}/v1/models"
    if vm_sh(cmd, profile=config.profile_name, check=False).returncode == 0:
        return None
    hint = (
        f"Ensure Ollama is bound to 0.0.0.0: `launchctl setenv OLLAMA_HOST 0.0.0.0:{config.inference_port}` "
        "and restart the Ollama app."
        if config.backend == "ollama"
        else "Ensure omlx is running on the host."
    )
    return (
        f"warning: {config.inference_label} not reachable at {url} from inside the VM. {hint}\n"
        "         opencode will hang (not error) until it is up."
    )


def discover_models(config: VmConfig) -> List[str]:
    """Model ids from the inference server, probed from the macOS host. [] on failure."""
    port = config.inference_port
    api_key = os.environ.get("OMLX_API_KEY", "")
    # Bypass any host proxy settings: these are local addresses.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for base in (f"http://127.0.0.1:{port}", f"http://{config.host_ip}:{port}"):
        if config.backend == "ollama":
            req = urllib.request.Request(f"{base}/api/tags")
        else:
            req = urllib.request.Request(f"{base}/v1/models")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with opener.open(req, timeout=3) as resp:
                body = json.loads(resp.read())
        except Exception:
            continue
        if config.backend == "ollama":
            return [m["name"] for m in body.get("models", [])]
        return [m["id"] for m in body.get("data", [])]
    return []


def _load_json(path: Path) -> Optional[dict]:
    """Parsed JSON object, {} if the file is absent, None (with a warning) if unparseable."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"warning: {path} is not valid JSON ({exc}); leaving it untouched.", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def write_harness_configs(paths: Paths, config: VmConfig, discovered: List[str]) -> None:
    for d in (paths.opencode_config_dir, paths.opencode_data_dir, paths.pi_dir / "agent"):
        d.mkdir(parents=True, exist_ok=True)
    common = dict(
        backend=config.backend,
        runtime_url=f"http://{config.host_ip}:{config.inference_port}/v1",
        api_key=os.environ.get("OMLX_API_KEY", ""),
        discovered=discovered,
        default_model=os.environ.get("OFFLINE_AGENT_DEFAULT_MODEL", ""),
    )
    if discovered:
        print(f"==> Discovered {len(discovered)} {config.inference_label} model(s)")
    else:
        print(f"==> No {config.inference_label} models discovered; keeping existing model lists")

    oc = _load_json(paths.opencode_config_file)
    if oc is not None:
        _write_json(paths.opencode_config_file, merge_opencode_config(oc, **common))

    pi_models = _load_json(paths.pi_models_file)
    pi_settings = _load_json(paths.pi_settings_file)
    if pi_models is not None and pi_settings is not None:
        models, settings = merge_pi_config(pi_models, pi_settings, **common)
        _write_json(paths.pi_models_file, models)
        if settings:
            _write_json(paths.pi_settings_file, settings)


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    try:
        project = resolve_project(Path.cwd(), Path.home())
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    sid = session_id(project)

    paths = Paths()
    config = VmConfig(memory_gib=args.memory, cpus=args.cpus, backend=args.backend)
    ensure_docker_context(config.profile_name)

    if args.rebuild:
        if colima_profile_running(config.profile_name):
            rebuild_teardown(config)
        else:
            print(f"==> VM '{config.profile_name}' not running; nothing to remove.")

    ensure_colima_vm(config)

    facts = probe_vm(config)
    discover_network(config, facts)
    ensure_docker_network(config, facts)

    # Both carry 3s timeouts; overlap them with the firewall/image/session work.
    join_probe = in_background(lambda: probe_inference(config))
    join_discovery = in_background(lambda: discover_models(config))

    apply_firewall(config)
    ensure_image(facts)
    ensure_session(config, project, sid)

    write_harness_configs(paths, config, join_discovery())
    warning = join_probe()
    if warning:
        print(warning, file=sys.stderr)

    name = container_name(sid)
    print()
    print("==> Offline agent session")
    print(f"    project  : {project}  (host copy is read-only; writes stay in the VM)")
    print(f"    session  : {sid}  (overlay in {session_dir(sid)})")
    print(f"    inference: {config.inference_label} at http://{config.host_ip}:{config.inference_port}")
    print(f"    network  : {NET_NAME}, no egress except inference")
    print("    run `opencode` or `pi` in the shell")
    if docker_container_running(name):
        print(f"==> {name} is already running; attaching a second shell")
        argv = docker_exec_argv(project, sid, config, os.environ)
    else:
        argv = docker_run_argv(project, sid, config, paths, os.environ)
    os.execvp("docker", argv)


if __name__ == "__main__":
    main()
