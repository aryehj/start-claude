#!/usr/bin/env python3
"""
research.py — spin up an isolated Vane + SearXNG research environment.

Creates a dedicated Colima VM (profile: research) with its own egress firewall
(Squid + iptables RESEARCH chain) and runs two containers:
  - research-searxng: SearXNG meta-search engine
  - research-vane: Vane AI research UI, accessible at http://localhost:3000

Host-side state lives in ~/.research/:
  denylist-sources.txt      pinned upstream feed URLs; --refresh-denylist re-fetches
  denylist-additions.txt    locally-curated extra blocks (exfil-capable services)
  denylist-overrides.txt    FP escape hatch; entries here are removed from the final filter
  denylist-cache/           fetched copies of each upstream feed
  searxng/settings.yml      seeded on first run
  vane-data/                Vane persistent state (LLM config survives --rebuild)

The composed denylist is: (cached-upstream ∪ additions) − overrides.

Usage:
  ./research.py                         bring up the environment
  ./research.py --reload-denylist       recompose filter from local files (no network)
  ./research.py --refresh-denylist      re-fetch upstream feeds, then reload
  ./research.py --reseed-denylist       overwrite sources/additions templates from repo
  ./research.py --rebuild               recreate containers (optionally VM too)
  ./research.py --backend=omlx          use omlx instead of Ollama
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


# ── Constants ──────────────────────────────────────────────────────────────────

TEMPLATE_DENYLIST_SOURCES = Path(__file__).parent / "templates" / "research-denylist-sources.txt"
TEMPLATE_DENYLIST_ADDITIONS = Path(__file__).parent / "templates" / "research-denylist-additions.txt"

COLIMA_PROFILE = "research"
CONTAINER_SEARXNG = "research-searxng"
CONTAINER_VANE = "research-vane"
RESEARCH_NET_NAME = "research-net"
SQUID_PORT = 8888

# Marker file in the VM holding the SHA-256 of the last-pushed squid.conf +
# denylist. Used to skip the Squid config push + restart on warm reattach when
# nothing changed (see apply_firewall / probe_vm).
SQUID_HASH_MARKER = "/etc/squid/.research-config.hash"

DEFAULT_MEMORY_GIB = 2
DEFAULT_CPUS = 2
DEFAULT_VANE_PORT = 3000


# ── Paths ──────────────────────────────────────────────────────────────────────

@dataclass
class Paths:
    base: Path = field(default_factory=lambda: Path.home() / ".research")

    @property
    def denylist_sources_file(self) -> Path:
        return self.base / "denylist-sources.txt"

    @property
    def denylist_additions_file(self) -> Path:
        return self.base / "denylist-additions.txt"

    @property
    def denylist_overrides_file(self) -> Path:
        return self.base / "denylist-overrides.txt"

    @property
    def denylist_cache_dir(self) -> Path:
        return self.base / "denylist-cache"

    @property
    def searxng_dir(self) -> Path:
        return self.base / "searxng"

    @property
    def searxng_settings(self) -> Path:
        return self.searxng_dir / "settings.yml"

    @property
    def vane_data_dir(self) -> Path:
        return self.base / "vane-data"

    @property
    def vane_config_file(self) -> Path:
        # Mount is {vane_data_dir}:/home/vane/data; Vane writes config.json
        # to /home/vane/data/config.json → host path is vane_data_dir/config.json.
        return self.vane_data_dir / "config.json"


# ── VmConfig ───────────────────────────────────────────────────────────────────

@dataclass
class VmConfig:
    profile_name: str = COLIMA_PROFILE
    memory_gib: int = DEFAULT_MEMORY_GIB
    cpus: int = DEFAULT_CPUS
    backend: str = "ollama"
    vane_port: int = DEFAULT_VANE_PORT
    # Populated by discover_network():
    bridge_ip: Optional[str] = None
    bridge_cidr: Optional[str] = None
    host_ip: Optional[str] = None
    research_net_cidr: Optional[str] = None

    @property
    def inference_port(self) -> int:
        return 11434 if self.backend == "ollama" else 8000

    @property
    def inference_label(self) -> str:
        return "Ollama" if self.backend == "ollama" else "omlx"


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="research.py",
        description=(
            "Spin up an isolated Vane + SearXNG research environment on a "
            "dedicated Colima VM with egress firewall."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
DENYLIST:
  research.py uses a denylist (default-allow) so Vane can scrape arbitrary
  search-result URLs. The composed denylist is:
      (cached upstream feeds ∪ denylist-additions.txt) − denylist-overrides.txt

  All three files live in ~/.research/ on the macOS host.
    --reload-denylist    recompose filter from local files (no network)
    --refresh-denylist   re-fetch upstream feeds, then reload
    --reseed-denylist    overwrite sources/additions from repo templates
                         (overrides.txt is never overwritten — it is user state)

ENVIRONMENT:
  RESEARCH_MEMORY        Default VM memory GiB (overridden by --memory)
  RESEARCH_CPUS          Default VM CPU count (overridden by --cpus)
  RESEARCH_BACKEND       Default backend: ollama (default) or omlx
  OMLX_API_KEY           API key for omlx backend
""",
    )
    p.add_argument(
        "--rebuild",
        action="store_true",
        help="Remove containers and recreate. With confirmation, also delete the Colima VM.",
    )
    p.add_argument(
        "--reload-denylist",
        action="store_true",
        dest="reload_denylist",
        help="Recompose the Squid denylist from local files (cache + additions − overrides) and reconfigure Squid. No network. Fast path; does not restart containers.",
    )
    p.add_argument(
        "--refresh-denylist",
        action="store_true",
        dest="refresh_denylist",
        help="Re-fetch each URL in denylist-sources.txt into denylist-cache/, then recompose and reload (implies --reload-denylist).",
    )
    p.add_argument(
        "--reseed-denylist",
        action="store_true",
        dest="reseed_denylist",
        help="Overwrite ~/.research/denylist-sources.txt and denylist-additions.txt with current repo templates. Use after pulling repo updates. denylist-overrides.txt is never overwritten.",
    )
    p.add_argument(
        "--backend",
        choices=["ollama", "omlx"],
        default=os.environ.get("RESEARCH_BACKEND", "ollama"),
        help="Local inference backend (default: ollama).",
    )
    p.add_argument(
        "--memory",
        type=_parse_gib,
        default=_parse_gib(os.environ.get("RESEARCH_MEMORY", str(DEFAULT_MEMORY_GIB))),
        metavar="GIB",
        help=f"VM memory in GiB (default: {DEFAULT_MEMORY_GIB}).",
    )
    p.add_argument(
        "--cpus",
        type=int,
        default=int(os.environ.get("RESEARCH_CPUS", str(DEFAULT_CPUS))),
        metavar="N",
        help=f"VM CPU count (default: {DEFAULT_CPUS}).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_VANE_PORT,
        dest="vane_port",
        metavar="PORT",
        help=f"Host port for Vane UI (default: {DEFAULT_VANE_PORT}).",
    )
    return p.parse_args()


def _parse_gib(raw: str) -> int:
    """Accept '2', '2G', '2GB', '2GiB' and return integer GiB."""
    cleaned = raw.strip().lower().rstrip("b").rstrip("i").rstrip("g")
    try:
        return int(cleaned)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid memory value {raw!r} — use integer GiB (e.g. 2, 2G, 2GB)"
        )


# ── Denylist seed / compose / fetch ────────────────────────────────────────────


def _check_legacy_allowlist(paths: Paths) -> None:
    """Exit loudly if the old allowlist-based layout is detected."""
    legacy = paths.base / "allowlist.txt"
    if legacy.exists():
        print(
            f"error: {legacy} exists — this installation predates the denylist migration.\n"
            "\n"
            "Manual steps required:\n"
            f"  1. rm -rf {paths.base}\n"
            "  2. ./research.py --rebuild\n"
            "\n"
            "The old allowlist.txt is no longer used. Delete the directory and let\n"
            "research.py recreate it from the current templates.",
            file=sys.stderr,
        )
        sys.exit(1)


def _seed_file(template: Path, dest: Path, label: str, force: bool) -> bool:
    """Copy template → dest unless dest exists (or force=True). Returns True if written."""
    if dest.exists() and not force:
        return False
    try:
        text = template.read_text()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"{label} template not found: {template}\n"
            "Ensure you are running research.py from a complete checkout of the repo."
        ) from None
    dest.write_text(text)
    verb = "Reseeded" if force else "Seeded"
    print(f"==> {verb} {label} at {dest}")
    return True


def seed_denylist_files(paths: Paths, force: bool = False) -> None:
    """Bootstrap ~/.research/ denylist files from templates.

    Creates the base dir, denylist-cache/, an empty denylist-overrides.txt,
    and seeds denylist-sources.txt + denylist-additions.txt from templates.
    overrides.txt is never overwritten by --reseed (it is user state).
    """
    paths.base.mkdir(parents=True, exist_ok=True)
    paths.denylist_cache_dir.mkdir(parents=True, exist_ok=True)
    _seed_file(TEMPLATE_DENYLIST_SOURCES, paths.denylist_sources_file, "denylist sources", force)
    _seed_file(TEMPLATE_DENYLIST_ADDITIONS, paths.denylist_additions_file, "denylist additions", force)
    if not paths.denylist_overrides_file.exists():
        paths.denylist_overrides_file.write_text(
            "# research.py denylist overrides — entries here are removed from the\n"
            "# final filter. Use this to undo a false positive pulled in by an\n"
            "# upstream feed. One domain per line; '#' for comments.\n"
        )


def _read_domain_lines(path: Path) -> List[str]:
    """Read a denylist file and return cleaned bare-domain entries.

    Strips comments, blank lines, hagezi 'wildcard' prefix (`*.`), and
    hosts-file IP prefix (`0.0.0.0 `). Lowercases for stable dedupe.
    """
    if not path.exists():
        return []
    out: List[str] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        # Strip "0.0.0.0 example.com" or "127.0.0.1 example.com" hosts format.
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("0.0.0.0", "127.0.0.1"):
            line = parts[1]
        elif len(parts) > 1:
            # Unknown multi-token line — skip rather than guess.
            continue
        # Strip hagezi wildcard prefix.
        if line.startswith("*."):
            line = line[2:]
        out.append(line.lower())
    return out


def compose_denylist(paths: Paths) -> List[str]:
    """Build the final denylist as: (cached-upstream ∪ additions) − overrides.

    Returns a sorted, deduped list of bare domain strings.
    """
    domains: set[str] = set()
    if paths.denylist_cache_dir.is_dir():
        for cached in sorted(paths.denylist_cache_dir.glob("*.txt")):
            domains.update(_read_domain_lines(cached))
    domains.update(_read_domain_lines(paths.denylist_additions_file))
    overrides = set(_read_domain_lines(paths.denylist_overrides_file))
    domains -= overrides
    return sorted(domains)


def _read_source_urls(sources_file: Path) -> List[str]:
    """Read denylist-sources.txt and return uncommented URLs."""
    if not sources_file.exists():
        return []
    urls: List[str] = []
    for raw in sources_file.read_text().splitlines():
        url = raw.split("#", 1)[0].strip()
        if url:
            urls.append(url)
    return urls


def _expected_cache_basenames(urls: List[str]) -> set[str]:
    """Map source URLs to the basenames refresh_denylist_cache writes."""
    return {(url.rsplit("/", 1)[-1] or "feed.txt") for url in urls}


def prune_orphan_cache_files(paths: Paths) -> List[str]:
    """Delete cached feeds whose source URL is no longer in sources.txt.

    Returns the list of removed basenames. Self-healing for the common case
    where a template SHA bump or feed-path change leaves stale `.txt` files
    in denylist_cache_dir — without this, compose_denylist's `*.txt` glob
    would silently merge orphans alongside the new feeds.
    """
    if not paths.denylist_cache_dir.is_dir():
        return []
    expected = _expected_cache_basenames(_read_source_urls(paths.denylist_sources_file))
    removed: List[str] = []
    for cached in sorted(paths.denylist_cache_dir.glob("*.txt")):
        if cached.name not in expected:
            cached.unlink()
            removed.append(cached.name)
    return removed


def refresh_denylist_cache(paths: Paths, *, abort_on_any_failure: bool = False) -> None:
    """Download each URL in denylist-sources.txt into denylist_cache_dir.

    Each response is written atomically (via .tmp + rename). On fetch failure,
    the existing cached copy is left in place and the next URL is attempted.
    If abort_on_any_failure=True (first-run bootstrap with no cache), any
    failure raises RuntimeError so we don't start a VM with a partial denylist.

    Orphaned cache files (URLs no longer in sources.txt) are pruned first so
    they aren't merged into the next compose pass.
    """
    paths.denylist_cache_dir.mkdir(parents=True, exist_ok=True)
    removed = prune_orphan_cache_files(paths)
    for name in removed:
        print(f"==> Pruned orphan cache file: {name}")
    urls = _read_source_urls(paths.denylist_sources_file)
    if not urls:
        print(f"==> No upstream denylist sources configured in {paths.denylist_sources_file}")
        return

    failures: List[str] = []
    for url in urls:
        basename = url.rsplit("/", 1)[-1] or "feed.txt"
        dest = paths.denylist_cache_dir / basename
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        print(f"==> Fetching {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research.py/denylist"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                tmp.write_bytes(resp.read())
            tmp.replace(dest)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            print(f"warning: failed to fetch {url}: {exc}", file=sys.stderr)
            tmp.unlink(missing_ok=True)
            failures.append(url)

    if failures and abort_on_any_failure:
        raise RuntimeError(
            f"First-run denylist bootstrap failed: {len(failures)} of {len(urls)} "
            f"upstream feeds could not be fetched. Check connectivity and re-run "
            f"with --refresh-denylist before the research VM is brought up."
        )


# ── Pure helpers ───────────────────────────────────────────────────────────────

def _prune_subdomains(domains: List[str]) -> List[str]:
    """Remove entries that are subdomains of other entries in the list.

    Squid 6 rejects a dstdomain ACL file that contains both a domain and one
    of its subdomains — the subdomain is redundant and treated as a fatal error.
    If both 'sub.example.com' and 'example.com' are present, only 'example.com'
    is kept (it already covers the subdomain via suffix matching).
    """
    domain_set = set(domains)
    result: List[str] = []
    for domain in domains:
        pos = domain.find(".")
        covered = False
        while pos != -1:
            if domain[pos + 1:] in domain_set:
                covered = True
                break
            pos = domain.find(".", pos + 1)
        if not covered:
            result.append(domain)
    return result


def denylist_to_squid_acl(domains: List[str]) -> str:
    """Convert bare domain names to a Squid dstdomain ACL file body.

    Expects clean bare-domain strings as returned by compose_denylist().
    Each entry becomes .example.com (dotted-suffix form). Subdomains of
    entries already present are pruned — Squid 6 rejects redundant ones.
    """
    pruned = _prune_subdomains([d for d in domains if d])
    return "\n".join(f".{d}" for d in pruned) + "\n" if pruned else ""


def render_searxng_settings(bridge_ip: str, proxy_port: int, secret: str) -> str:
    """Return the body of settings.yml for the research SearXNG instance."""
    return f"""\
use_default_settings:
  engines:
    keep_only:
      - google
      - bing
      - duckduckgo
      - brave
      - qwant
      - wikipedia
      - arxiv
      - google scholar
      - semantic scholar

server:
  secret_key: "{secret}"
  base_url: "http://research-searxng:8080/"
  limiter: false

search:
  formats:
    - html
    - json

outgoing:
  proxies:
    all://: "http://{bridge_ip}:{proxy_port}"
"""


def render_squid_conf(bridge_ip: str, squid_port: int) -> str:
    """Return a minimal squid.conf for the research VM.

    Explicitly omits Debian's default squid.conf.default so only these
    directives are active. cache deny all makes this a pure filtering
    forward proxy, not a caching proxy.
    """
    return f"""\
http_port {bridge_ip}:{squid_port}
visible_hostname research-squid

acl denylist dstdomain "/etc/squid/denylist.txt"
acl CONNECT method CONNECT
acl SSL_ports port 443
acl Safe_ports port 80 443

http_access deny denylist
http_access deny CONNECT !SSL_ports
http_access deny !Safe_ports
http_access allow all

access_log /var/log/squid/access.log
cache deny all
"""


def squid_config_hash(conf_body: str, acl_body: str) -> str:
    """SHA-256 over the squid.conf + denylist contents.

    Stored in the VM (SQUID_HASH_MARKER) after each push so a warm reattach can
    skip re-pushing Squid config and restarting the daemon when nothing changed.
    The NUL separator keeps conf/acl boundaries unambiguous.
    """
    h = hashlib.sha256()
    h.update(conf_body.encode())
    h.update(b"\0")
    h.update(acl_body.encode())
    return h.hexdigest()


def render_iptables_apply_script(
    bridge_ip: str,
    bridge_cidr: str,
    research_net_cidr: str,
    host_ip: str,
    proxy_port: int,
    inference_port: int,
    has_hashlimit: bool = True,
) -> str:
    """Return a shell script that applies the RESEARCH iptables chain.

    All variables are interpolated here at template-render time so the
    resulting shell script has no $VAR references — no nested escaping needed.

    has_hashlimit selects the rate-limit rule shape: xt_hashlimit gives
    per-source-IP limits, plain `-m limit` is a coarser global cap fallback.
    """
    if has_hashlimit:
        rate_limit_rules = (
            "# Rate limit: max 30 new connections/sec per source IP (burst 50).\n"
            "# Defense-in-depth against bulk exfil; secondary to denylist.\n"
            "iptables -A RESEARCH -m conntrack --ctstate NEW -m hashlimit \\\n"
            "  --hashlimit-above 30/sec --hashlimit-burst 50 \\\n"
            "  --hashlimit-mode srcip --hashlimit-name research_newconn \\\n"
            "  -j DROP\n"
        )
    else:
        rate_limit_rules = (
            "# Rate limit fallback (no xt_hashlimit): coarse global cap.\n"
            "iptables -A RESEARCH -m conntrack --ctstate NEW -m limit \\\n"
            "  --limit 100/sec --limit-burst 150 -j RETURN\n"
            "iptables -A RESEARCH -m conntrack --ctstate NEW -j DROP\n"
        )

    return f"""\
#!/bin/sh
set -e

# Ensure DOCKER-USER exists (docker creates it on fresh daemons).
iptables -N DOCKER-USER 2>/dev/null || true
iptables -C FORWARD -j DOCKER-USER 2>/dev/null || iptables -I FORWARD 1 -j DOCKER-USER

# Dedicated RESEARCH chain: create if absent, then flush to start clean.
iptables -N RESEARCH 2>/dev/null || true
iptables -F RESEARCH

# Jump into our chain from DOCKER-USER for bridge traffic (idempotent).
iptables -C DOCKER-USER -s {bridge_cidr} -j RESEARCH 2>/dev/null \\
  || iptables -I DOCKER-USER 1 -s {bridge_cidr} -j RESEARCH

# Also jump for user-defined research-net traffic.
iptables -C DOCKER-USER -s {research_net_cidr} -j RESEARCH 2>/dev/null \\
  || iptables -I DOCKER-USER 2 -s {research_net_cidr} -j RESEARCH

# Rules in order:
iptables -A RESEARCH -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A RESEARCH -d {bridge_ip} -p tcp --dport {proxy_port} -j RETURN
iptables -A RESEARCH -d {host_ip}   -p tcp --dport {inference_port} -j RETURN
iptables -A RESEARCH -d {bridge_ip} -p udp --dport 53 -j RETURN
iptables -A RESEARCH -d {bridge_ip} -p tcp --dport 53 -j RETURN
# Allow Vane → SearXNG on port 8080 within research-net.
iptables -A RESEARCH -s {research_net_cidr} -d {research_net_cidr} -p tcp --dport 8080 -j RETURN
{rate_limit_rules}iptables -A RESEARCH -j REJECT --reject-with icmp-admin-prohibited
"""


def patch_vane_searxng_url(config_text: str, desired: str) -> Optional[str]:
    """Return patched config JSON if search.searxngURL differs from desired, else None.

    Returns None on JSONDecodeError (caller should warn and leave file untouched).
    Idempotent: returns None when the value is already correct.
    """
    import json as _json
    try:
        config = _json.loads(config_text)
    except _json.JSONDecodeError:
        return None
    if not isinstance(config.get("search"), dict):
        config["search"] = {}
    search = config["search"]
    if search.get("searxngURL") == desired:
        return None
    search["searxngURL"] = desired
    return _json.dumps(config, indent=2)


def vm_has_hashlimit(profile: str = COLIMA_PROFILE) -> bool:
    """Probe the VM for xt_hashlimit availability. Best-effort; default True on error.

    Tries `iptables -m hashlimit -h` (no kernel module required for help text)
    then falls back to a real rule probe via a throwaway chain.
    """
    result = vm_sh(
        "sudo iptables -m hashlimit --help 2>&1 | grep -q hashlimit-name && echo ok || echo missing",
        check=False,
    )
    if "ok" in result.stdout:
        return True
    if "missing" in result.stdout:
        return False
    return True


# ── Subprocess wrappers ────────────────────────────────────────────────────────

def _colima_profile() -> str:
    return COLIMA_PROFILE


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


def vm_ssh(args: List[str], *, profile: str = COLIMA_PROFILE, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command (argv list) inside the Colima VM.

    Builds a quoted command string and pipes it via vm_sh to avoid
    colima's argv-join double-quoting issue.
    """
    import shlex
    cmd = " ".join(shlex.quote(a) for a in args)
    return vm_sh(cmd, profile=profile, check=check)


def vm_put_file(local_path: Path, remote_path: str, *, profile: str = COLIMA_PROFILE, mode: str = "644") -> None:
    """Copy a host-side file into the VM at remote_path via sudo tee."""
    content = local_path.read_bytes()
    subprocess.run(
        ["colima", "ssh", "-p", profile, "--", "sudo", "tee", remote_path],
        input=content,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["colima", "ssh", "-p", profile, "--", "sudo", "chmod", mode, remote_path],
        capture_output=True,
        check=True,
    )


def colima_profile_running(profile: str = COLIMA_PROFILE) -> bool:
    result = subprocess.run(
        ["colima", "list", "-p", profile, "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False
    import json
    try:
        return json.loads(result.stdout.strip()).get("status") == "Running"
    except (json.JSONDecodeError, AttributeError):
        return False


def docker_container_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "container", "inspect", name],
        capture_output=True,
    )
    return result.returncode == 0


def docker_container_running(name: str) -> bool:
    """Return True only if the container exists AND its State.Running is true."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def start_or_recreate(name: str, create: Callable[[], None]) -> bool:
    """Start an existing container or recreate it if start fails. Returns True if newly created.

    If the container doesn't exist, or if docker start fails / the container
    isn't running after start (e.g. RWLayer nil, stale run config), the
    container is removed and create() is called for a fresh docker run.
    """
    if docker_container_exists(name):
        result = subprocess.run(
            ["docker", "start", name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and docker_container_running(name):
            print(f"    {name}: started (existing container)")
            return False
        stderr = result.stderr.strip() or "(no stderr)"
        print(f"warning: docker start {name} failed, recreating: {stderr}", file=sys.stderr)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=True)
    create()
    return True


def docker_network_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
    )
    return result.returncode == 0


# ── Phase functions ────────────────────────────────────────────────────────────

def ensure_colima_vm(config: VmConfig) -> None:
    if colima_profile_running(config.profile_name):
        # Warn if the running VM was sized differently.
        result = subprocess.run(
            ["colima", "list", "-p", config.profile_name, "--json"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            import json
            try:
                info = json.loads(result.stdout.strip())
                running_cpus = info.get("cpus", "")
                running_mem = info.get("memory", "")
                if running_cpus and str(running_cpus) != str(config.cpus):
                    print(
                        f"warning: running VM has {running_cpus} CPUs; "
                        f"requested {config.cpus}. Use --rebuild to resize.",
                        file=sys.stderr,
                    )
                if running_mem and str(config.memory_gib) not in str(running_mem):
                    print(
                        f"warning: running VM memory is {running_mem}; "
                        f"requested {config.memory_gib} GiB. Use --rebuild to resize.",
                        file=sys.stderr,
                    )
            except (json.JSONDecodeError, AttributeError):
                pass
        return

    print(
        f"==> Starting Colima VM '{config.profile_name}' "
        f"({config.memory_gib} GiB RAM, {config.cpus} CPUs)"
    )
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


def probe_vm(config: VmConfig) -> dict:
    """Gather every read-only VM fact the warm path needs in one colima ssh call.

    Replaces the former chain of separate vm_sh probes (bridge gateway, bridge
    subnet, host route + getent fallbacks, research-net CIDR, squid install/active
    status, hashlimit availability, stored config hash). Emits KEY=value lines
    parsed on the host. The only write that can follow is research-net creation
    when RESEARCH_NET_CIDR comes back empty (handled in ensure_docker_network).
    """
    script = r"""#!/bin/sh
# Bridge gateway + subnet in one inspect (was two separate calls).
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

# research-net CIDR (empty when the network does not exist yet).
research_cidr=$(docker network inspect %RESEARCH_NET% \
  -f '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)
echo "RESEARCH_NET_CIDR=$research_cidr"

# Squid package + service status.
if command -v squid >/dev/null 2>&1; then echo "SQUID_INSTALLED=true"; else echo "SQUID_INSTALLED=false"; fi
if systemctl is-active --quiet squid 2>/dev/null; then echo "SQUID_ACTIVE=true"; else echo "SQUID_ACTIVE=false"; fi

# xt_hashlimit availability (help text needs no kernel module load).
if sudo iptables -m hashlimit --help 2>&1 | grep -q hashlimit-name; then
  echo "HASHLIMIT=true"
else
  echo "HASHLIMIT=false"
fi

# Stored squid config hash (used to skip the push when nothing changed).
echo "SQUID_STORED_HASH=$(sudo cat %SQUID_HASH_MARKER% 2>/dev/null || true)"
""".replace("%RESEARCH_NET%", RESEARCH_NET_NAME).replace("%SQUID_HASH_MARKER%", SQUID_HASH_MARKER)

    out = vm_sh(script, check=False).stdout
    facts: dict = {}
    for line in out.replace("\r", "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            facts[key] = value.strip()
    return facts


def discover_network(config: VmConfig, facts: dict) -> VmConfig:
    """Populate bridge IP, host IP, and CIDR on config from batched probe facts."""
    bridge_ip = facts.get("BRIDGE_IP", "")
    if not bridge_ip:
        bridge_ip = "172.17.0.1"
        print(f"warning: could not discover docker bridge gateway; falling back to {bridge_ip}", file=sys.stderr)

    bridge_cidr = facts.get("BRIDGE_CIDR", "") or "172.17.0.0/16"

    host_ip = facts.get("HOST_IP", "")
    if not host_ip:
        print(
            f"warning: could not determine the macOS host IP from inside the VM; "
            f"local inference ({config.inference_label}) will not work.",
            file=sys.stderr,
        )
        host_ip = "127.0.0.1"

    print(f"==> VM network: bridge={bridge_ip} cidr={bridge_cidr} host={host_ip}")

    config.bridge_ip = bridge_ip
    config.bridge_cidr = bridge_cidr
    config.host_ip = host_ip
    return config


def ensure_docker_context(profile: str = COLIMA_PROFILE) -> None:
    result = subprocess.run(
        ["docker", "context", "use", f"colima-{profile}"],
        capture_output=True,
    )
    if result.returncode != 0:
        print(
            f"warning: could not switch docker context to colima-{profile}; "
            "assuming current context talks to the right daemon.",
            file=sys.stderr,
        )


def ensure_docker_network(config: VmConfig, facts: dict) -> None:
    """Use the probed research-net CIDR; create the network only if it's missing."""
    cidr = facts.get("RESEARCH_NET_CIDR", "")

    if not cidr:
        # First run only: the batched probe found no research-net. Create it and
        # re-read its CIDR (the one read that can also trigger a write).
        vm_sh(f"docker network create {RESEARCH_NET_NAME} >/dev/null")
        cidr = vm_sh(
            f"docker network inspect {RESEARCH_NET_NAME} -f '{{{{(index .IPAM.Config 0).Subnet}}}}' 2>/dev/null || true",
            check=False,
        ).stdout.strip().strip("\r")

    if not cidr:
        cidr = "172.20.0.0/24"
        print(f"warning: could not discover {RESEARCH_NET_NAME} CIDR; falling back to {cidr}", file=sys.stderr)

    config.research_net_cidr = cidr
    print(f"==> Research network: {RESEARCH_NET_NAME} cidr={cidr}")


def install_squid(config: VmConfig, facts: dict) -> None:
    if facts.get("SQUID_INSTALLED") != "true":
        print("==> Installing Squid in Colima VM")
        vm_sh("sudo apt-get update -qq")
        vm_sh("sudo apt-get install -y squid")
        # Squid auto-starts on install with default config; stop it so
        # apply_firewall can write the minimal config before restarting.
        vm_sh("sudo systemctl stop squid 2>/dev/null || true")
        # The freshly-installed daemon was just stopped; reflect that so
        # apply_firewall takes its start-from-cold path.
        facts["SQUID_INSTALLED"] = "true"
        facts["SQUID_ACTIVE"] = "false"


def _reload_squid(config: VmConfig, *, cold: bool) -> None:
    """Bring Squid up with the freshly-pushed config; raise on failure.

    cold=True starts a stopped/first-install daemon. cold=False does an in-place
    `squid -k reconfigure` (far cheaper than a restart) on an already-running
    daemon, falling back to a restart if reconfigure fails.
    """
    if cold:
        vm_sh("sudo systemctl enable --now squid >/dev/null 2>&1 || true")
        cmd = (
            "sudo systemctl restart squid 2>&1; RC=$?;"
            " [ $RC -ne 0 ] && sudo journalctl -u squid --no-pager -n 30 2>/dev/null || true;"
            " exit $RC"
        )
    else:
        cmd = (
            "sudo squid -k reconfigure 2>&1 || {"
            " sudo systemctl restart squid 2>&1; RC=$?;"
            " [ $RC -ne 0 ] && sudo journalctl -u squid --no-pager -n 30 2>/dev/null || true;"
            " exit $RC; }"
        )
    result = vm_sh(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"squid failed to start (exit {result.returncode}).\n{result.stdout.strip()}"
        )


def _write_squid_hash_marker(config: VmConfig, digest: str) -> None:
    """Persist the pushed-config hash in the VM so the next warm probe can read it."""
    subprocess.run(
        ["colima", "ssh", "-p", config.profile_name, "--", "sudo", "tee", SQUID_HASH_MARKER],
        input=(digest + "\n").encode(),
        capture_output=True,
        check=True,
    )


def apply_firewall(config: VmConfig, paths: Paths, facts: dict) -> None:
    """Push Squid config + denylist (only when changed), then re-assert iptables.

    The iptables chain is rebuilt on every attach — the egress trust boundary
    must not depend on a warm-path shortcut. The Squid config push + daemon
    reload is hash-gated: on a warm reattach where the rendered squid.conf and
    denylist are byte-identical to what was last pushed and Squid is already
    running, the push and reload are skipped entirely.
    """
    assert config.bridge_ip and config.bridge_cidr and config.host_ip and config.research_net_cidr

    denylist_domains = compose_denylist(paths)
    acl_body = denylist_to_squid_acl(denylist_domains)
    conf_body = render_squid_conf(config.bridge_ip, SQUID_PORT)

    hl = facts.get("HASHLIMIT")
    if hl == "true":
        has_hashlimit = True
    elif hl == "false":
        has_hashlimit = False
    else:
        has_hashlimit = vm_has_hashlimit(config.profile_name)
    if not has_hashlimit:
        print("warning: xt_hashlimit not available in VM; using coarse '-m limit' fallback.", file=sys.stderr)

    fw_script = render_iptables_apply_script(
        bridge_ip=config.bridge_ip,
        bridge_cidr=config.bridge_cidr,
        research_net_cidr=config.research_net_cidr,
        host_ip=config.host_ip,
        proxy_port=SQUID_PORT,
        inference_port=config.inference_port,
        has_hashlimit=has_hashlimit,
    )

    new_hash = squid_config_hash(conf_body, acl_body)
    squid_active = facts.get("SQUID_ACTIVE") == "true"
    push_needed = new_hash != facts.get("SQUID_STORED_HASH", "") or not squid_active

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fw_file = tmp_path / "firewall-apply.sh"
        fw_file.write_text(fw_script)

        if push_needed:
            conf_file = tmp_path / "squid.conf"
            acl_file = tmp_path / "denylist.txt"
            conf_file.write_text(conf_body)
            acl_file.write_text(acl_body)

            vm_put_file(conf_file, "/etc/squid/squid.conf")
            vm_put_file(acl_file, "/etc/squid/denylist.txt")
            _reload_squid(config, cold=not squid_active)
            _write_squid_hash_marker(config, new_hash)
            print(f"==> Squid config pushed ({len(denylist_domains)} denylist entries)")
        else:
            print(f"==> Squid config unchanged; skipping push ({len(denylist_domains)} denylist entries)")

        fw_content = fw_file.read_bytes()
        subprocess.run(
            ["colima", "ssh", "-p", config.profile_name, "--", "sudo", "sh"],
            input=fw_content,
            capture_output=True,
            check=True,
        )

    print("==> Firewall applied")
    print(f"    proxy: http://{config.bridge_ip}:{SQUID_PORT}")


def seed_searxng_settings(paths: Paths, config: VmConfig) -> None:
    paths.searxng_dir.mkdir(parents=True, exist_ok=True)
    if not paths.searxng_settings.exists():
        secret = secrets.token_hex(32)
        assert config.bridge_ip
        paths.searxng_settings.write_text(
            render_searxng_settings(config.bridge_ip, SQUID_PORT, secret)
        )
        print(f"==> Seeded {paths.searxng_settings} (secret_key generated, proxy={config.bridge_ip}:{SQUID_PORT})")
    else:
        # Drift check: fix stale proxy address if it doesn't match current bridge IP.
        assert config.bridge_ip
        expected_proxy = f"http://{config.bridge_ip}:{SQUID_PORT}"
        content = paths.searxng_settings.read_text()
        m = re.search(r'all://:\s*"([^"]+)"', content)
        if m and m.group(1) != expected_proxy:
            print(f"==> SearXNG proxy drift: {m.group(1)} → {expected_proxy}")
            new_content = re.sub(
                r'(all://:\s*)"[^"]+"',
                f'\\1"{expected_proxy}"',
                content,
            )
            paths.searxng_settings.write_text(new_content)


def ensure_searxng_container(paths: Paths, config: VmConfig) -> bool:
    """Start or create the SearXNG container. Returns True if newly created."""
    print("==> Starting SearXNG container")

    def _create() -> None:
        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", CONTAINER_SEARXNG,
                "--network", RESEARCH_NET_NAME,
                "--restart", "unless-stopped",
                "-v", f"{paths.searxng_settings}:/etc/searxng/settings.yml:ro",
                "docker.io/searxng/searxng",
            ],
            capture_output=True,
            check=True,
        )
        print(f"    {CONTAINER_SEARXNG}: created")

    return start_or_recreate(CONTAINER_SEARXNG, _create)


def ensure_vane_searxng_url(paths: Paths) -> bool:
    """Ensure Vane's config.json points at the correct SearXNG container URL.

    Reads config.json, patches search.searxngURL if wrong, writes back.
    Returns True if the file was changed.
    Does nothing (returns False) if the file does not exist yet — correct-only-if-exists
    is the safe default for first-run installs where config.json isn't created until
    after the user completes UI setup.
    """
    import json as _json
    desired = f"http://{CONTAINER_SEARXNG}:8080"
    cfg_file = paths.vane_config_file
    if not cfg_file.exists():
        return False
    text = cfg_file.read_text()
    patched = patch_vane_searxng_url(text, desired)
    if patched is None:
        # Either already correct (silent) or parse error (warn).
        try:
            _json.loads(text)
        except _json.JSONDecodeError:
            print(
                f"warning: could not parse {cfg_file}; SearXNG URL may be wrong.\n"
                f"  Open http://localhost:3000 → Settings → SearXNG URL and set it to {desired}.",
                file=sys.stderr,
            )
        return False
    try:
        old_url = _json.loads(text).get("search", {}).get("searxngURL", "(unset)")
    except Exception:
        old_url = "(unset)"
    cfg_file.write_text(patched)
    print(f"==> Vane SearXNG URL corrected: {old_url} → {desired}")
    return True


def ensure_vane_container(paths: Paths, config: VmConfig) -> bool:
    """Start or create the Vane container. Returns True if newly created."""
    paths.vane_data_dir.mkdir(parents=True, exist_ok=True)
    print("==> Starting Vane container")

    def _create() -> None:
        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", CONTAINER_VANE,
                "--network", RESEARCH_NET_NAME,
                "--restart", "unless-stopped",
                # Point host.docker.internal at the VM's default-route gateway
                # (the macOS host), NOT Docker's host-gateway alias — under
                # Colima the latter resolves to the Linux VM's bridge gateway,
                # one hop short of where Ollama/omlx actually listens. This is
                # the same IP the firewall RETURN rule and probe_inference use.
                "--add-host", f"host.docker.internal:{config.host_ip}",
                "-p", f"{config.vane_port}:3000",
                "-e", f"HTTP_PROXY=http://{config.bridge_ip}:{SQUID_PORT}",
                "-e", f"HTTPS_PROXY=http://{config.bridge_ip}:{SQUID_PORT}",
                "-e", f"NO_PROXY={CONTAINER_SEARXNG},host.docker.internal,localhost,127.0.0.1",
                "-v", f"{paths.vane_data_dir}:/home/vane/data",
                "docker.io/itzcrazykns1337/vane:slim-latest",
            ],
            capture_output=True,
            check=True,
        )
        print(f"    {CONTAINER_VANE}: created (http://localhost:{config.vane_port})")
        print(f"    note: configure LLM at http://localhost:{config.vane_port} on first access")

    return start_or_recreate(CONTAINER_VANE, _create)


def probe_inference(config: VmConfig) -> Optional[str]:
    """Non-fatal probe; return a warning string if the backend is unreachable.

    The curl carries a 3s timeout, so this is run on a background thread (see
    main) and its returned warning is printed once the thread is joined — the
    timeout overlaps the container bring-up instead of blocking it.
    """
    assert config.host_ip
    if config.backend == "ollama":
        result = vm_sh(
            f"curl -sf --max-time 3 http://{config.host_ip}:{config.inference_port}/api/tags",
            check=False,
        )
        if result.returncode != 0:
            return (
                f"warning: Ollama not reachable at http://{config.host_ip}:{config.inference_port} "
                f"from inside the Colima VM.\n"
                f"Ensure Ollama is running on the macOS host and bound to 0.0.0.0.\n"
                f"On the host, run once:\n"
                f"    launchctl setenv OLLAMA_HOST 0.0.0.0:{config.inference_port}\n"
                f"and restart the Ollama app. Continuing without local inference."
            )
    else:
        omlx_key = os.environ.get("OMLX_API_KEY", "")
        auth_header = f'-H "Authorization: Bearer {omlx_key}"' if omlx_key else ""
        result = vm_sh(
            f"curl -sf --max-time 3 {auth_header} http://{config.host_ip}:{config.inference_port}/v1/models",
            check=False,
        )
        if result.returncode != 0:
            return (
                f"warning: omlx not reachable at http://{config.host_ip}:{config.inference_port} "
                f"from inside the Colima VM.\n"
                f"Ensure omlx is running on the host. Continuing without local inference."
            )
    return None


def start_inference_probe(config: VmConfig) -> "tuple[threading.Thread, list]":
    """Launch probe_inference on a background thread; return (thread, result_box).

    Pass the pair to join_inference_probe once the overlapping work completes.
    """
    print(f"==> Probing {config.inference_label} at http://{config.host_ip}:{config.inference_port} from inside VM")
    box: list = [None]

    def _run() -> None:
        box[0] = probe_inference(config)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, box


def join_inference_probe(probe: "tuple[threading.Thread, list]") -> None:
    """Join the probe thread started by start_inference_probe and print any warning."""
    thread, box = probe
    thread.join()
    if box[0]:
        print(box[0], file=sys.stderr)


def rebuild_teardown(config: VmConfig) -> None:
    """Remove containers (and optionally the Colima VM) before a rebuild."""
    for name in (CONTAINER_VANE, CONTAINER_SEARXNG):
        if docker_container_exists(name):
            print(f"==> --rebuild: removing container '{name}'")
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    print()
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
        print(f"==> Keeping Colima VM; only containers will be rebuilt.")


def reload_denylist_fast_path(paths: Paths, config: VmConfig) -> None:
    """Recompose denylist from local files, push ACL file, reconfigure Squid. No container restart."""
    assert config.bridge_ip and config.research_net_cidr

    removed = prune_orphan_cache_files(paths)
    for name in removed:
        print(f"==> Pruned orphan cache file: {name}")
    denylist_domains = compose_denylist(paths)
    acl_body = denylist_to_squid_acl(denylist_domains)

    with tempfile.TemporaryDirectory() as tmp:
        acl_file = Path(tmp) / "denylist.txt"
        acl_file.write_text(acl_body)
        vm_put_file(acl_file, "/etc/squid/denylist.txt")

    vm_sh(
        "sudo squid -k reconfigure 2>/dev/null || sudo systemctl restart squid"
    )

    # Keep the stored hash consistent with what's now on disk so the next full
    # bring-up doesn't needlessly re-push. squid.conf is unchanged by this path
    # (only the denylist moved), so re-derive it from the current bridge IP.
    conf_body = render_squid_conf(config.bridge_ip, SQUID_PORT)
    _write_squid_hash_marker(config, squid_config_hash(conf_body, acl_body))

    print(f"==> Denylist reloaded ({len(denylist_domains)} entries)")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    paths = Paths()
    _check_legacy_allowlist(paths)
    config = VmConfig(
        memory_gib=args.memory,
        cpus=args.cpus,
        backend=args.backend,
        vane_port=args.vane_port,
    )

    # --reseed-denylist: overwrite source/additions templates before any other op.
    if args.reseed_denylist:
        seed_denylist_files(paths, force=True)

    # --refresh-denylist / --reload-denylist: VM-bound fast path.
    if args.refresh_denylist or args.reload_denylist:
        seed_denylist_files(paths)
        if args.refresh_denylist:
            refresh_denylist_cache(paths)
        if not colima_profile_running(config.profile_name):
            print(f"error: Colima VM '{config.profile_name}' is not running. Start it first.", file=sys.stderr)
            sys.exit(1)
        ensure_docker_context(config.profile_name)
        facts = probe_vm(config)
        config = discover_network(config, facts)
        ensure_docker_network(config, facts)
        reload_denylist_fast_path(paths, config)
        return

    # --rebuild: tear down containers (and optionally VM) before bring-up.
    if args.rebuild:
        if colima_profile_running(config.profile_name):
            ensure_docker_context(config.profile_name)
            rebuild_teardown(config)
        else:
            print(f"==> VM '{config.profile_name}' not running; nothing to remove.")

    # ── Full bring-up ──────────────────────────────────────────────────────────
    seed_denylist_files(paths)

    # Bootstrap: if no upstream cache yet, fetch now. Abort on any failure so we
    # don't bring up a research VM with a partial denylist.
    cache_empty = not any(paths.denylist_cache_dir.glob("*.txt"))
    if cache_empty and _read_source_urls(paths.denylist_sources_file):
        print("==> First-run bootstrap: fetching upstream denylist feeds")
        refresh_denylist_cache(paths, abort_on_any_failure=True)

    ensure_colima_vm(config)
    ensure_docker_context(config.profile_name)

    facts = probe_vm(config)
    config = discover_network(config, facts)
    ensure_docker_network(config, facts)

    install_squid(config, facts)
    seed_searxng_settings(paths, config)
    apply_firewall(config, paths, facts)

    # Background the inference probe so its 3s curl timeout overlaps the
    # container bring-up below instead of blocking on the critical path.
    probe = start_inference_probe(config)

    ensure_searxng_container(paths, config)
    url_changed = ensure_vane_searxng_url(paths)
    vane_created = ensure_vane_container(paths, config)
    if url_changed and not vane_created:
        # Config was patched on an already-running container; restart so Vane re-reads it.
        subprocess.run(["docker", "restart", CONTAINER_VANE], capture_output=True, check=True)
        print(f"    {CONTAINER_VANE}: restarted to apply SearXNG URL")

    # Collect the backgrounded inference probe; print its warning if any.
    join_inference_probe(probe)

    print()
    print("==> Research environment ready")
    print(f"    Vane    : http://localhost:{config.vane_port}")
    print(f"    SearXNG : auto-wired to http://{CONTAINER_SEARXNG}:8080 (internal)")
    print(f"    LLM     : configure at http://localhost:{config.vane_port} → Settings → LLM")
    if config.backend == "ollama":
        print(f"              use http://host.docker.internal:{config.inference_port} (Ollama)")
    else:
        print(f"              use http://host.docker.internal:{config.inference_port}/v1 (omlx)")
    print(f"    proxy   : http://{config.bridge_ip}:{SQUID_PORT}  (denylist sources: {paths.denylist_sources_file})")


if __name__ == "__main__":
    main()
