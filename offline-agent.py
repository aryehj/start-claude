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

Storage is bounded. Each session gets its own sparse ext4 image in the VM
(default 5 GiB) holding the overlay upper layer and the harnesses' writable
state; the container root is read-only with small tmpfs mounts for scratch;
both VM disks are capped. Nothing in the container can write to the Mac.

Host-side state lives in ~/.offline-agent/, mounted read-only:
  opencode/config/    opencode.json (provider injected on every run)
  pi/agent/           pi models.json + settings.json (provider injected on every run)
opencode and pi history, sessions and auth live in the session image instead,
and go away with the session.

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
import re
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
# render_session_setup_script prints this when it finds a pre-image session.
# A stdout marker, not an exit code: `colima ssh` exits 1 for any remote
# failure and only logs the real status ("exit status 42") on stderr.
LEGACY_SESSION_MARKER = "OFFLINE_AGENT_LEGACY_SESSION"

DEFAULT_MEMORY_GIB = 2
DEFAULT_CPUS = 2
# Colima has two disks: the data disk (--disk) holds /var/lib/docker (image,
# build cache), the root disk (--root-disk) holds the OS and SESSIONS_ROOT.
DEFAULT_DISK_GIB = 10
DEFAULT_ROOT_DISK_GIB = 10
DEFAULT_SESSION_DISK = "5G"

# Container limits. tmpfs pages are charged to the memory cgroup, so the two
# tmpfs sizes must fit inside CONTAINER_MEMORY with room for the harness.
CONTAINER_MEMORY = "1536m"
CONTAINER_PIDS_LIMIT = 512
TMPFS_MOUNTS = (
    # exec: uv venvs ($TMPDIR/.venv) and tool caches run binaries from here.
    ("/tmp", "rw,exec,nosuid,nodev,mode=1777,size=512m"),
    ("/root/.cache", "rw,exec,nosuid,nodev,mode=0755,size=256m"),
)


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
    def pi_agent_dir(self) -> Path:
        return self.pi_dir / "agent"

    @property
    def opencode_config_file(self) -> Path:
        return self.opencode_config_dir / "opencode.json"

    @property
    def pi_models_file(self) -> Path:
        return self.pi_agent_dir / "models.json"

    @property
    def pi_settings_file(self) -> Path:
        return self.pi_agent_dir / "settings.json"

    @property
    def moved_state_notice(self) -> Path:
        return self.base / "README-state-moved.txt"


# ── VmConfig ───────────────────────────────────────────────────────────────────

@dataclass
class VmConfig:
    profile_name: str = COLIMA_PROFILE
    memory_gib: int = DEFAULT_MEMORY_GIB
    cpus: int = DEFAULT_CPUS
    disk_gib: int = DEFAULT_DISK_GIB
    root_disk_gib: int = DEFAULT_ROOT_DISK_GIB
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

STORAGE:
  Each session's writes (project overlay + opencode/pi state) live in a sparse
  ext4 image capped by --session-disk; past it the agent gets ENOSPC. Images
  are sized when the session is created; to grow one, in the VM run
  `truncate -s <new> disk.img && resize2fs <loop device>` on the mounted image.
  VM disk sizes apply only when the VM is created (--rebuild + delete to change).

ADDING PACKAGES:
  Edit the user-packages block at the end of
  dockerfiles/offline-agent.Dockerfile, then run with --rebuild.

ENVIRONMENT:
  OFFLINE_AGENT_MEMORY         Default VM memory GiB (overridden by --memory)
  OFFLINE_AGENT_CPUS           Default VM CPU count (overridden by --cpus)
  OFFLINE_AGENT_DISK           VM data disk GiB: image + build cache (overridden by --disk)
  OFFLINE_AGENT_ROOT_DISK      VM root disk GiB: OS + session images (overridden by --root-disk)
  OFFLINE_AGENT_SESSION_DISK   Per-session cap, e.g. 5G or 256M (overridden by --session-disk)
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
    p.add_argument(
        "--disk",
        type=_parse_gib,
        default=_parse_gib(os.environ.get("OFFLINE_AGENT_DISK", str(DEFAULT_DISK_GIB))),
        metavar="GIB",
        help=f"VM data disk (docker image, build cache) in GiB, at VM creation (default: {DEFAULT_DISK_GIB}).",
    )
    p.add_argument(
        "--root-disk",
        type=_parse_gib,
        default=_parse_gib(os.environ.get("OFFLINE_AGENT_ROOT_DISK", str(DEFAULT_ROOT_DISK_GIB))),
        metavar="GIB",
        help=f"VM root disk (OS, session images) in GiB, at VM creation (default: {DEFAULT_ROOT_DISK_GIB}).",
    )
    p.add_argument(
        "--session-disk",
        type=_parse_size_mib,
        default=_parse_size_mib(os.environ.get("OFFLINE_AGENT_SESSION_DISK", DEFAULT_SESSION_DISK)),
        metavar="SIZE",
        help=f"Per-session storage cap, e.g. 5G or 256M, at session creation (default: {DEFAULT_SESSION_DISK}).",
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


def _parse_size_mib(raw: str) -> int:
    """Accept '5', '5G', '5GiB', '256M', '256MB' and return MiB (bare numbers are GiB)."""
    m = re.fullmatch(r"\s*(\d+)\s*(?:([gm])(?:i?b)?)?\s*", raw.lower())
    if not m or int(m.group(1)) == 0:
        raise argparse.ArgumentTypeError(
            f"invalid size {raw!r} — use a positive integer with G or M (e.g. 5G, 256M)"
        )
    n = int(m.group(1))
    return n if m.group(2) == "m" else n * 1024


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


def render_session_setup_script(sid: str, project: Path, cap_mib: int) -> str:
    """Shell script that creates and mounts the session image (idempotent).

    Every agent-writable byte of the session lives in `disk.img`, a sparse
    ext4 image of `cap_mib`, loop-mounted at `mnt/`: the overlay upper/work
    dirs (which must exist before the overlay mounts) and the harness state.
    The image is formatted under a temporary name so a failed mkfs is retried
    rather than mounted. Loop mounts do not survive a VM restart, so the mount
    is re-done whenever it is absent; this runs before every container start.
    `discard` punches holes in the image file when the agent deletes files.
    The `path` marker maps the opaque id back to the project for listings.

    A session dir with `upper/` at its top level predates the image and is
    refused with LEGACY_SESSION_MARKER: its volume's mount options are fixed.
    """
    d = session_dir(sid)
    m = f"{d}/mnt"
    return f"""\
set -e
if [ -d {d}/upper ] && [ ! -e {d}/disk.img ]; then
  echo {LEGACY_SESSION_MARKER}
  exit 1
fi
sudo mkdir -p {m}
if [ ! -e {d}/disk.img ]; then
  sudo truncate -s {cap_mib}M {d}/disk.img.new
  sudo mkfs.ext4 -q -F {d}/disk.img.new
  sudo mv {d}/disk.img.new {d}/disk.img
fi
sudo mountpoint -q {m} || sudo mount -o loop,discard {d}/disk.img {m}
sudo mkdir -p {m}/upper {m}/work {m}/harness/opencode {m}/harness/state {m}/harness/pi
printf '%s\\n' {shlex.quote(str(project))} | sudo tee {d}/path >/dev/null
"""


def legacy_session_message(sid: str, project: Path) -> str:
    """Remediation for a session created before per-session images."""
    ctx = f"docker --context colima-{COLIMA_PROFILE}"
    vol = volume_name(sid)
    out = f"~/offline-agent-{sid}"
    return f"""\
error: the session for {project} ({sid}) predates per-session disk images
and cannot be migrated in place. Its changes exist only inside the VM.

To keep them, export first. A git repo (all refs, including new commits):
  {ctx} run --rm -v {vol}:/w -w /w {IMAGE_TAG} git bundle create - --all > {out}.bundle
Any directory (the full merged view, host files included):
  {ctx} run --rm -v {vol}:/w {IMAGE_TAG} tar -C /w -cf - . > {out}.tar

Then remove the old session and re-run:
  {ctx} volume rm {vol}
  colima ssh -p {COLIMA_PROFILE} -- sudo rm -rf {session_dir(sid)}"""


def low_space_warning(avail_kib: Optional[int], cap_mib: int) -> Optional[str]:
    """Warn when the VM root disk cannot hold one full session.

    Session images are sparse, so overcommitting the caps is normal, but one
    session that fills its cap on a nearly full root disk takes the VM down.
    """
    if avail_kib is None or avail_kib >= cap_mib * 1024:
        return None
    return (
        f"warning: the VM root disk has {avail_kib // 1024} MiB free, less than one "
        f"session cap ({cap_mib} MiB). A session that fills its cap would exhaust it.\n"
        f"         Free space by removing old sessions under {SESSIONS_ROOT}, or recreate "
        "the VM with a larger --root-disk (--rebuild, then delete the VM)."
    )


def volume_create_argv(sid: str, project: Path) -> List[str]:
    """`docker volume create` argv for the session's overlay volume.

    The local driver mounts overlayfs when a container first uses the volume;
    lowerdir is the project path, which Colima mounts at the same path in the VM.
    upper/work are on the session image, so the image caps the overlay.
    """
    d = session_dir(sid)
    return [
        "docker", "volume", "create",
        "--driver", "local",
        "--opt", "type=overlay",
        "--opt", "device=overlay",
        "--opt", f"o=lowerdir={project},upperdir={d}/mnt/upper,workdir={d}/mnt/work",
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


def running_vm_size(info: Mapping[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """(cpus, memory GiB, data disk GiB) from `colima list --json`, which reports bytes.

    The root disk size is not in the listing.
    """
    def gib(key: str) -> Optional[int]:
        value = info.get(key)
        return int(value) // (1024 ** 3) if value not in (None, "") else None

    cpus = info.get("cpus")
    return (int(cpus) if cpus not in (None, "") else None, gib("memory"), gib("disk"))


def colima_start_argv(config: VmConfig, *, create: bool) -> List[str]:
    """`colima start` argv. Disk sizes are passed only when creating the VM.

    No --mount: Colima's default mounts $HOME at the same path, which is
    exactly the overlay lower layer we need, so switching projects never
    restarts the VM. Disks are never resized on an existing VM: Colima ignores
    a smaller --disk with a warning, and a smaller --root-disk is unverified.
    """
    argv = [
        "colima", "start", "-p", config.profile_name,
        "--vm-type", "vz",
        "--runtime", "docker",
        "--cpu", str(config.cpus),
        "--memory", str(config.memory_gib),
        "--mount-type", "virtiofs",
        "--network-address",
    ]
    if create:
        argv += ["--disk", str(config.disk_gib), "--root-disk", str(config.root_disk_gib)]
    return argv


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
        # The root is read-only; keep shell history with the session's state.
        "-e", "HISTFILE=/root/.local/state/bash_history",
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
    """argv for the session container: ephemeral, no published ports, no egress.

    Storage is bounded: the root is read-only, scratch is sized tmpfs, and every
    writable mount is on the session image. Mac paths (~/.offline-agent) are
    read-only config only. No log driver: a runaway TTY stream would otherwise
    be persisted to the VM disk.
    """
    harness = f"{session_dir(sid)}/mnt/harness"
    tmpfs: List[str] = []
    for target, opts in TMPFS_MOUNTS:
        tmpfs += ["--tmpfs", f"{target}:{opts}"]
    return [
        "docker", "run",
        "--rm", "-it",
        "--name", container_name(sid),
        "--read-only",
        *tmpfs,
        "--memory", CONTAINER_MEMORY,
        "--pids-limit", str(CONTAINER_PIDS_LIMIT),
        "--log-driver", "none",
        "--network", NET_NAME,
        "--dns", DNS_BLACKHOLE,
        # The VM's default-route gateway (the macOS host), not Docker's
        # host-gateway alias, which under Colima resolves to the VM's own
        # bridge gateway. Same IP as the firewall RETURN rule.
        "--add-host", f"host.docker.internal:{config.host_ip}",
        # nocopy: never copy image content into the overlay on first mount.
        "-v", f"{volume_name(sid)}:{project}:nocopy",
        # opencode runs with its config dir read-only (it then skips the
        # plugin npm install it would otherwise put there).
        "-v", f"{paths.opencode_config_dir}:/root/.config/opencode:ro",
        # pi has one agent dir for config and state: config files are
        # mounted read-only over a writable dir on the session image.
        "-v", f"{paths.pi_models_file}:/root/.pi/agent/models.json:ro",
        "-v", f"{paths.pi_settings_file}:/root/.pi/agent/settings.json:ro",
        "-v", f"{harness}/opencode:/root/.local/share/opencode",
        "-v", f"{harness}/state:/root/.local/state",
        "-v", f"{harness}/pi:/root/.pi",
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
        running_cpus, running_gib, running_disk = running_vm_size(info)
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
        if running_disk is not None and running_disk != config.disk_gib:
            print(
                f"warning: running VM has a {running_disk} GiB data disk; requested {config.disk_gib} GiB. "
                "Disk sizes apply only when the VM is created: --rebuild and delete the VM to change them.",
                file=sys.stderr,
            )
        return

    create = info is None
    disks = f", {config.disk_gib} GiB data + {config.root_disk_gib} GiB root disk" if create else ""
    print(
        f"==> {'Creating' if create else 'Starting'} Colima VM '{config.profile_name}' "
        f"({config.memory_gib} GiB RAM, {config.cpus} CPUs{disks})"
    )
    subprocess.run(colima_start_argv(config, create=create), check=True)


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

# Free space on the root disk, which holds the session images.
echo "ROOT_AVAIL_KIB=$(df -Pk /var/lib 2>/dev/null | awk 'NR==2 {print $4}')"

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


def render_list_sessions_script() -> str:
    """Shell loop printing `<id>\\t<path>` for every session that may hold agent changes.

    A session image that is not mounted (none are, right after a VM boot)
    cannot be inspected cheaply, so it is listed: over-warning before a VM
    delete is cheap, under-warning loses work. Pre-image sessions keep upper/
    at the top level.
    """
    return f"""\
for d in {SESSIONS_ROOT}/*/; do
  [ -d "$d" ] || continue
  d=${{d%/}}
  if [ -e "$d/disk.img" ]; then
    if sudo mountpoint -q "$d/mnt" && [ -z "$(sudo ls -A "$d/mnt/upper" 2>/dev/null)" ]; then
      continue
    fi
  elif [ -z "$(sudo ls -A "$d/upper" 2>/dev/null)" ]; then
    continue
  fi
  printf '%s\\t%s\\n' "$(basename "$d")" "$(sudo cat "$d/path" 2>/dev/null)"
done
"""


def list_sessions(profile: str = COLIMA_PROFILE) -> List[Tuple[str, str]]:
    """(id, project path) for every session that may hold agent changes."""
    script = render_list_sessions_script()
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


def ensure_session(config: VmConfig, project: Path, sid: str, cap_mib: int, facts: dict) -> None:
    """Create and mount the session image, then its overlay volume (idempotent)."""
    avail = facts.get("ROOT_AVAIL_KIB", "")
    warning = low_space_warning(int(avail) if avail.isdigit() else None, cap_mib)
    if warning:
        print(warning, file=sys.stderr)
    result = vm_sh(render_session_setup_script(sid, project, cap_mib), profile=config.profile_name, check=False)
    if LEGACY_SESSION_MARKER in result.stdout.splitlines():
        raise SystemExit(legacy_session_message(sid, project))
    if result.returncode != 0:
        raise SystemExit(
            f"error: session setup failed in the VM (exit {result.returncode}); not starting the container.\n"
            f"{result.stderr.strip()}"
        )
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
    # Truncates in place, keeping the inode: pi's config files are per-file
    # bind mounts, which pin the inode. An atomic-rename writer would leave an
    # attached container reading the old file.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def write_harness_configs(paths: Paths, config: VmConfig, discovered: List[str]) -> None:
    for d in (paths.opencode_config_dir, paths.pi_agent_dir):
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
        # Always written, even as {}: both files are bind-mount sources, and
        # docker creates a directory for a missing source.
        _write_json(paths.pi_settings_file, settings)


def notice_moved_harness_state(paths: Paths) -> None:
    """Say once that harness state on the Mac is no longer used; never delete it.

    Before per-session images, opencode's data dir and pi's whole dir were
    mounted read-write from ~/.offline-agent. That state now lives in each
    session image. The old files are left in place, unmounted.
    """
    if paths.moved_state_notice.exists():
        return
    pi_config = {paths.pi_models_file.name, paths.pi_settings_file.name}
    stale = []
    if paths.opencode_data_dir.is_dir() and any(paths.opencode_data_dir.iterdir()):
        stale.append(paths.opencode_data_dir)
    if paths.pi_agent_dir.is_dir():
        stale += sorted(p for p in paths.pi_agent_dir.iterdir() if p.name not in pi_config)
    if not stale:
        return
    text = (
        "opencode and pi now keep their history, sessions and auth inside each\n"
        "offline-agent session image, not on the Mac. These paths are no longer\n"
        "mounted and are left untouched; delete them when you no longer need them:\n"
        + "".join(f"  {p}\n" for p in stale)
    )
    paths.moved_state_notice.write_text(text)
    print(f"==> Note: {text.rstrip()}\n    (shown once; see {paths.moved_state_notice})")


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
    config = VmConfig(
        memory_gib=args.memory,
        cpus=args.cpus,
        disk_gib=args.disk,
        root_disk_gib=args.root_disk,
        backend=args.backend,
    )
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
    ensure_session(config, project, sid, args.session_disk, facts)

    write_harness_configs(paths, config, join_discovery())
    notice_moved_harness_state(paths)
    warning = join_probe()
    if warning:
        print(warning, file=sys.stderr)

    name = container_name(sid)
    print()
    print("==> Offline agent session")
    print(f"    project  : {project}  (host copy is read-only; writes stay in the VM)")
    print(f"    session  : {sid}  (writes capped by {session_dir(sid)}/disk.img)")
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
