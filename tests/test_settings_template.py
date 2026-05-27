"""Tests for templates/global-claude-settings.json."""
import json
from pathlib import Path

TEMPLATE = Path(__file__).parent.parent / "templates" / "global-claude-settings.json"


def _load():
    assert TEMPLATE.exists(), f"Template file not found: {TEMPLATE}"
    with TEMPLATE.open() as f:
        return json.load(f)


def test_template_is_valid_json():
    data = _load()
    assert isinstance(data, dict)


def test_required_top_level_keys():
    data = _load()
    assert data.get("showThinkingSummaries") is True
    assert data.get("coauthorTag") == "none"
    assert data.get("theme") == "dark-ansi"


def test_permissions_structure():
    data = _load()
    perms = data.get("permissions")
    assert isinstance(perms, dict), "permissions must be a dict"
    assert isinstance(perms.get("allow"), list), "permissions.allow must be a list"
    assert isinstance(perms.get("deny"), list), "permissions.deny must be a list"
    assert len(perms["allow"]) > 0, "permissions.allow must not be empty"
    assert len(perms["deny"]) > 0, "permissions.deny must not be empty"


def test_git_push_not_in_allow():
    data = _load()
    allow = data["permissions"]["allow"]
    push_allows = [e for e in allow if "push" in e.lower()]
    assert not push_allows, (
        f"git push must not appear in allow list, found: {push_allows}"
    )


def test_git_push_in_deny():
    data = _load()
    deny = data["permissions"]["deny"]
    # At minimum, bare "git push" must be covered
    assert any("push" in e for e in deny), (
        f"git push must appear in deny list, got: {deny}"
    )


def test_all_deny_patterns_are_strings():
    data = _load()
    for entry in data["permissions"]["deny"]:
        assert isinstance(entry, str), f"deny entry must be a string: {entry!r}"


def test_all_allow_patterns_are_strings():
    data = _load()
    for entry in data["permissions"]["allow"]:
        assert isinstance(entry, str), f"allow entry must be a string: {entry!r}"


def test_no_effortlevel_in_template():
    # effortLevel is intentionally unpinned (ADR-017); must not appear in the template
    data = _load()
    assert "effortLevel" not in data, "effortLevel must not be seeded in the template"
