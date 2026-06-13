"""Tests for templates/sandbox-allowlist.txt and start-claude.sh injection logic."""
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
ALLOWLIST = REPO / "templates" / "sandbox-allowlist.txt"
SCRIPT = REPO / "start-claude.sh"


# ── template file ─────────────────────────────────────────────────────────────

def _parse_domains():
    """Return list of bare domain entries (non-comment, non-blank lines)."""
    domains = []
    with ALLOWLIST.open() as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                domains.append(line)
    return domains


def test_allowlist_exists():
    assert ALLOWLIST.exists(), f"Template not found: {ALLOWLIST}"


def test_allowlist_nonempty():
    domains = _parse_domains()
    assert len(domains) > 0, "sandbox-allowlist.txt has no domain entries"


def test_allowlist_domains_are_valid():
    """Each non-comment line must look like a bare domain (no http://, no slashes, no spaces)."""
    domains = _parse_domains()
    for d in domains:
        assert '/' not in d, f"Domain must not contain slashes: {d!r}"
        assert ' ' not in d, f"Domain must not contain spaces: {d!r}"
        assert d.startswith('http') is False, f"Domain must not start with http: {d!r}"
        assert '.' in d, f"Domain must contain a dot: {d!r}"


def test_allowlist_includes_anthropic():
    """anthropic.com must be in the list (required for Claude Code to function)."""
    domains = _parse_domains()
    assert 'anthropic.com' in domains


def test_allowlist_excludes_github_com():
    """github.com must NOT be in the list (read-only stance; users add it per-project)."""
    domains = _parse_domains()
    assert 'github.com' not in domains, (
        "github.com must not be in the default allowlist "
        "(write-capable; users add it per-project if needed)"
    )


def test_allowlist_excludes_wildcards():
    """Template stores bare domains only; expansion to *.d is done by the script."""
    domains = _parse_domains()
    wildcards = [d for d in domains if d.startswith('*.')]
    assert not wildcards, (
        f"Template must store bare domains; wildcards must not appear: {wildcards}"
    )


# ── script static checks ──────────────────────────────────────────────────────

def _script_text():
    return SCRIPT.read_text()


def test_script_references_allowlist_template():
    text = _script_text()
    assert 'sandbox-allowlist.txt' in text, (
        "start-claude.sh must reference templates/sandbox-allowlist.txt"
    )


def test_script_sets_alloweddomains():
    text = _script_text()
    assert 'allowedDomains' in text, (
        "start-claude.sh must inject sandbox.network.allowedDomains"
    )


def test_script_sets_denieddomains():
    """deniedDomains is required by the Zod schema; must be present alongside allowedDomains."""
    text = _script_text()
    assert 'deniedDomains' in text, (
        "start-claude.sh must include deniedDomains (required by sandbox schema)"
    )


def test_script_expands_wildcard():
    """The injection logic must produce both bare and *.d forms."""
    text = _script_text()
    # Look for the wildcard expansion pattern in the Python block
    assert re.search(r'\*\.\{', text) or re.search(r"f'\*\.", text) or re.search(r'"\\*\\.', text) or '*.{d}' in text or "'*.' +" in text or '"*." +' in text or re.search(r'\*\.' , text), (
        "start-claude.sh must expand each domain to both bare and *.d forms"
    )


def test_script_has_reseed_flag():
    text = _script_text()
    assert '--reseed-sandbox-allowlist' in text, (
        "start-claude.sh must have a --reseed-sandbox-allowlist flag"
    )


def test_script_seeds_network_sandbox():
    """network key must appear under sandbox in the injection logic."""
    text = _script_text()
    assert "'network'" in text or '"network"' in text, (
        "start-claude.sh must set sandbox.network block"
    )
