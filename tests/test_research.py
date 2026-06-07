"""Unit tests for research.py pure helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from research import (
    _prune_subdomains,
    compose_denylist,
    denylist_to_squid_acl,
    patch_vane_searxng_url,
    prune_orphan_cache_files,
    render_iptables_apply_script,
    render_searxng_settings,
    Paths,
)


# ── denylist_to_squid_acl ────────────────────────────────────────────────────

def test_denylist_squid_acl_basic_domain():
    result = denylist_to_squid_acl(["wikipedia.org"])
    assert ".wikipedia.org" in result


def test_denylist_squid_acl_dotted_prefix():
    result = denylist_to_squid_acl(["example.com"])
    assert result.strip() == ".example.com"


def test_denylist_squid_acl_multiple_domains():
    result = denylist_to_squid_acl(["alpha.com", "beta.com"])
    lines = result.strip().splitlines()
    assert ".alpha.com" in lines
    assert ".beta.com" in lines


def test_denylist_squid_acl_skips_empty_strings():
    result = denylist_to_squid_acl(["", "google.com", ""])
    lines = [l for l in result.splitlines() if l.strip()]
    assert len(lines) == 1


def test_denylist_squid_acl_single_entry_has_newline():
    result = denylist_to_squid_acl(["google.com"])
    assert result.endswith("\n")


def test_denylist_squid_acl_no_regex_escaping():
    result = denylist_to_squid_acl(["api.example-corp.com"])
    assert ".api.example-corp.com" in result
    assert "\\" not in result


def test_denylist_squid_acl_empty_input():
    assert denylist_to_squid_acl([]) == ""
    assert denylist_to_squid_acl(["", ""]) == ""


# ── _prune_subdomains ────────────────────────────────────────────────────────

def test_prune_subdomains_drops_covered_entry():
    result = _prune_subdomains(["sub.example.com", "example.com"])
    assert "sub.example.com" not in result
    assert "example.com" in result


def test_prune_subdomains_keeps_disjoint_entries():
    result = _prune_subdomains(["alpha.com", "beta.com"])
    assert set(result) == {"alpha.com", "beta.com"}


def test_prune_subdomains_deep_nesting():
    # only the shallowest ancestor is kept
    result = _prune_subdomains(["a.b.example.com", "b.example.com", "example.com"])
    assert result == ["example.com"]


def test_prune_subdomains_empty():
    assert _prune_subdomains([]) == []


def test_denylist_squid_acl_no_subdomain_redundancy():
    # Squid 6 rejects a file where both a domain and its subdomain appear.
    result = denylist_to_squid_acl(["sub.example.com", "example.com"])
    lines = result.strip().splitlines()
    assert ".example.com" in lines
    assert ".sub.example.com" not in lines


# ── render_searxng_settings ───────────────────────────────────────────────────

def test_searxng_settings_contains_proxy():
    result = render_searxng_settings("172.17.0.1", 8888, "deadbeef")
    assert "http://172.17.0.1:8888" in result


def test_searxng_settings_contains_secret():
    result = render_searxng_settings("172.17.0.1", 8888, "mysecretkey")
    assert "mysecretkey" in result


def test_searxng_settings_contains_engines():
    result = render_searxng_settings("172.17.0.1", 8888, "x")
    for engine in ("google", "bing", "duckduckgo", "wikipedia", "arxiv"):
        assert engine in result


def test_searxng_settings_uses_research_container_url():
    result = render_searxng_settings("172.17.0.1", 8888, "x")
    assert "research-searxng:8080" in result


def test_searxng_settings_is_valid_yaml_structure():
    result = render_searxng_settings("10.0.0.1", 8888, "abc123")
    # Must start with a top-level key, not empty
    assert result.startswith("use_default_settings:")
    # Must contain outgoing proxies section
    assert "outgoing:" in result
    assert "proxies:" in result
    assert 'all://:' in result


# ── render_iptables_apply_script ──────────────────────────────────────────────

def test_iptables_no_uninterpolated_vars():
    script = render_iptables_apply_script(
        bridge_ip="172.17.0.1",
        bridge_cidr="172.17.0.0/16",
        research_net_cidr="172.20.0.0/24",
        host_ip="192.168.5.1",
        proxy_port=8888,
        inference_port=11434,
    )
    # No shell variable references should remain (all interpolated at render time)
    assert "${" not in script
    assert "$BRIDGE" not in script
    assert "$HOST" not in script
    assert "$TINYPROXY" not in script


def test_iptables_contains_research_chain():
    script = render_iptables_apply_script(
        bridge_ip="172.17.0.1",
        bridge_cidr="172.17.0.0/16",
        research_net_cidr="172.20.0.0/24",
        host_ip="192.168.5.1",
        proxy_port=8888,
        inference_port=11434,
    )
    assert "RESEARCH" in script
    assert "DOCKER-USER" in script


def test_iptables_interpolates_ips():
    script = render_iptables_apply_script(
        bridge_ip="10.1.2.3",
        bridge_cidr="10.1.2.0/24",
        research_net_cidr="10.5.6.0/24",
        host_ip="192.168.99.1",
        proxy_port=9999,
        inference_port=8000,
    )
    assert "10.1.2.3" in script
    assert "10.1.2.0/24" in script
    assert "192.168.99.1" in script
    assert "9999" in script
    assert "8000" in script


def test_iptables_ends_with_reject():
    script = render_iptables_apply_script(
        bridge_ip="172.17.0.1",
        bridge_cidr="172.17.0.0/16",
        research_net_cidr="172.20.0.0/24",
        host_ip="192.168.5.1",
        proxy_port=8888,
        inference_port=11434,
    )
    assert "REJECT" in script


def test_iptables_allows_intra_net_8080():
    script = render_iptables_apply_script(
        bridge_ip="172.17.0.1",
        bridge_cidr="172.17.0.0/16",
        research_net_cidr="172.20.0.0/24",
        host_ip="192.168.5.1",
        proxy_port=8888,
        inference_port=11434,
    )
    assert "--dport 8080" in script
    assert "172.20.0.0/24" in script


# ── compose_denylist ─────────────────────────────────────────────────────────

def _make_paths(tmp_path) -> Paths:
    p = Paths(base=tmp_path)
    p.denylist_cache_dir.mkdir(parents=True, exist_ok=True)
    return p


def test_compose_denylist_union_cache_and_additions(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "feed.txt").write_text("evil.com\n")
    p.denylist_additions_file.write_text("pastebin.com\n")
    result = compose_denylist(p)
    assert "evil.com" in result
    assert "pastebin.com" in result


def test_compose_denylist_overrides_subtract(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "feed.txt").write_text("evil.com\nfalsepositiv.com\n")
    p.denylist_additions_file.write_text("pastebin.com\n")
    p.denylist_overrides_file.write_text("falsepositiv.com\n")
    result = compose_denylist(p)
    assert "evil.com" in result
    assert "pastebin.com" in result
    assert "falsepositiv.com" not in result


def test_compose_denylist_overrides_can_remove_addition(tmp_path):
    p = _make_paths(tmp_path)
    p.denylist_additions_file.write_text("pastebin.com\n")
    p.denylist_overrides_file.write_text("pastebin.com\n")
    result = compose_denylist(p)
    assert "pastebin.com" not in result


def test_compose_denylist_multiple_cache_files_merged(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "pro.txt").write_text("alpha.com\n")
    (p.denylist_cache_dir / "fake.txt").write_text("beta.com\n")
    result = compose_denylist(p)
    assert "alpha.com" in result
    assert "beta.com" in result


def test_compose_denylist_sorted_and_deduped(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "feed.txt").write_text("z.com\na.com\nz.com\n")
    p.denylist_additions_file.write_text("m.com\na.com\n")
    result = compose_denylist(p)
    assert result == sorted(set(result))
    assert result.count("a.com") == 1
    assert result.count("z.com") == 1


def test_compose_denylist_missing_additions_tolerated(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "feed.txt").write_text("evil.com\n")
    # additions file does not exist
    result = compose_denylist(p)
    assert "evil.com" in result


def test_compose_denylist_missing_overrides_tolerated(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "feed.txt").write_text("evil.com\n")
    # overrides file does not exist — should not crash
    result = compose_denylist(p)
    assert "evil.com" in result


def test_compose_denylist_empty_cache_dir(tmp_path):
    p = _make_paths(tmp_path)
    p.denylist_additions_file.write_text("pastebin.com\n")
    result = compose_denylist(p)
    assert result == ["pastebin.com"]


def test_compose_denylist_strips_comments_and_blank_lines(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "feed.txt").write_text(
        "# a comment\n\nevil.com\n  # another\nspam.com\n"
    )
    result = compose_denylist(p)
    assert "evil.com" in result
    assert "spam.com" in result
    assert len([d for d in result if d.startswith("#")]) == 0


def test_compose_denylist_strips_hosts_format(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "feed.txt").write_text("0.0.0.0 evil.com\n127.0.0.1 spam.com\n")
    result = compose_denylist(p)
    assert "evil.com" in result
    assert "spam.com" in result
    assert any("0.0.0.0" in d for d in result) is False


def test_compose_denylist_strips_wildcard_prefix(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "feed.txt").write_text("*.evil.com\n")
    result = compose_denylist(p)
    assert "evil.com" in result
    assert "*.evil.com" not in result


def test_compose_denylist_set_algebra_invariant(tmp_path):
    # Final = (cache ∪ additions) − overrides
    p = _make_paths(tmp_path)
    cache = {"a.com", "b.com", "c.com"}
    additions = {"b.com", "d.com"}
    overrides = {"c.com", "d.com"}
    (p.denylist_cache_dir / "feed.txt").write_text("\n".join(cache) + "\n")
    p.denylist_additions_file.write_text("\n".join(additions) + "\n")
    p.denylist_overrides_file.write_text("\n".join(overrides) + "\n")
    result = set(compose_denylist(p))
    expected = (cache | additions) - overrides
    assert result == expected


# ── patch_vane_searxng_url ────────────────────────────────────────────────────

DESIRED_URL = "http://research-searxng:8080"

def test_patch_vane_searxng_url_already_correct_returns_none():
    config = '{"search": {"searxngURL": "http://research-searxng:8080"}, "version": 1}'
    assert patch_vane_searxng_url(config, DESIRED_URL) is None


def test_patch_vane_searxng_url_wrong_host_corrected():
    config = '{"search": {"searxngURL": "http://host.docker.internal:8080"}, "version": 1}'
    result = patch_vane_searxng_url(config, DESIRED_URL)
    assert result is not None
    import json
    parsed = json.loads(result)
    assert parsed["search"]["searxngURL"] == DESIRED_URL
    assert parsed["version"] == 1  # other keys preserved


def test_patch_vane_searxng_url_preserves_other_keys():
    config = '{"search": {"searxngURL": "http://wrong:8080", "otherSetting": true}, "modelProviders": ["openai"]}'
    result = patch_vane_searxng_url(config, DESIRED_URL)
    assert result is not None
    import json
    parsed = json.loads(result)
    assert parsed["search"]["searxngURL"] == DESIRED_URL
    assert parsed["search"]["otherSetting"] is True
    assert parsed["modelProviders"] == ["openai"]


def test_patch_vane_searxng_url_missing_search_key_adds_it():
    config = '{"version": 1, "modelProviders": []}'
    result = patch_vane_searxng_url(config, DESIRED_URL)
    assert result is not None
    import json
    parsed = json.loads(result)
    assert parsed["search"]["searxngURL"] == DESIRED_URL
    assert parsed["version"] == 1


def test_patch_vane_searxng_url_malformed_json_returns_none():
    assert patch_vane_searxng_url("{not valid json", DESIRED_URL) is None


def test_patch_vane_searxng_url_empty_string_returns_none():
    assert patch_vane_searxng_url("", DESIRED_URL) is None


def test_patch_vane_searxng_url_missing_searxng_url_key_in_search():
    # search key exists but no searxngURL inside it
    config = '{"search": {"someOtherKey": "val"}, "version": 2}'
    result = patch_vane_searxng_url(config, DESIRED_URL)
    assert result is not None
    import json
    parsed = json.loads(result)
    assert parsed["search"]["searxngURL"] == DESIRED_URL
    assert parsed["search"]["someOtherKey"] == "val"


# ── prune_orphan_cache_files ─────────────────────────────────────────────────

def test_prune_orphan_cache_files_removes_stale(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "pro.txt").write_text("old.example\n")
    (p.denylist_cache_dir / "pro-onlydomains.txt").write_text("new.example\n")
    p.denylist_sources_file.write_text(
        "https://example.test/wildcard/pro-onlydomains.txt\n"
    )
    removed = prune_orphan_cache_files(p)
    assert removed == ["pro.txt"]
    assert not (p.denylist_cache_dir / "pro.txt").exists()
    assert (p.denylist_cache_dir / "pro-onlydomains.txt").exists()


def test_prune_orphan_cache_files_keeps_all_when_sources_match(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "a.txt").write_text("a.example\n")
    (p.denylist_cache_dir / "b.txt").write_text("b.example\n")
    p.denylist_sources_file.write_text(
        "https://example.test/a.txt\nhttps://example.test/b.txt\n"
    )
    assert prune_orphan_cache_files(p) == []
    assert (p.denylist_cache_dir / "a.txt").exists()
    assert (p.denylist_cache_dir / "b.txt").exists()


def test_prune_orphan_cache_files_no_sources_removes_everything(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "stale.txt").write_text("x.example\n")
    p.denylist_sources_file.write_text("# all commented\n")
    removed = prune_orphan_cache_files(p)
    assert removed == ["stale.txt"]


def test_prune_orphan_cache_files_handles_missing_cache_dir(tmp_path):
    p = Paths(base=tmp_path)  # no cache dir created
    p.denylist_sources_file.write_text("https://example.test/feed.txt\n")
    assert prune_orphan_cache_files(p) == []


def test_prune_orphan_cache_files_ignores_comments_in_sources(tmp_path):
    p = _make_paths(tmp_path)
    (p.denylist_cache_dir / "active.txt").write_text("ok.example\n")
    (p.denylist_cache_dir / "disabled.txt").write_text("stale.example\n")
    p.denylist_sources_file.write_text(
        "https://example.test/active.txt\n# https://example.test/disabled.txt\n"
    )
    removed = prune_orphan_cache_files(p)
    assert removed == ["disabled.txt"]


if __name__ == "__main__":
    import unittest
    # Run all test_ functions manually for environments without pytest
    import inspect
    module = sys.modules[__name__]
    passed = failed = 0
    for name, fn in inspect.getmembers(module, inspect.isfunction):
        if name.startswith("test_"):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as exc:
                print(f"  FAIL  {name}: {exc}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
