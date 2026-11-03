"""Tests for the shared global-settings helper (plans/refresh-injected-claude-settings.md).

The helper merges the repo's preferences into ``~/.claude/settings.json``.
``start-claude.sh`` and ``start-agent.sh`` both call it, in place of their
former inline Python heredocs.

Helper interface (Phase 2 implements against this)
--------------------------------------------------

Path: ``merge_global_settings.py`` at the repo root, a sibling of
``templates/`` and ``research.py``. It must use only the standard library and
run under the macOS system ``python3`` (3.9), because both scripts run it on
the host.

CLI::

    python3 merge_global_settings.py SETTINGS_PATH TEMPLATE_PATH

- SETTINGS_PATH is missing: start from a copy of the template, apply the same
  rules as for an existing file, and write the result.
- SETTINGS_PATH exists: load it, apply the rules, and rewrite it only if
  something changed. A file that already conforms keeps its exact bytes.
- Writes use ``json.dumps(data, indent=2) + "\\n"``.
- Prints one ``==> <message>`` line to stdout per change. Prints nothing
  when there is nothing to do.
- Exits 0 on success. Exits non-zero, with a message on stderr, and leaves
  SETTINGS_PATH untouched (not created, not modified) when SETTINGS_PATH is
  not valid JSON or TEMPLATE_PATH is missing.

Python API (importable from pytest):

- ``apply_rules(data: dict, template: dict) -> list[str]``: mutates ``data``
  in place and returns the change messages, without the ``==> `` prefix. An
  empty list means nothing changed. It never mutates ``template``, and
  anything it copies from the template is a deep copy.
- ``merge_file(settings_path, template_path) -> list[str]``: the CLI's work
  without printing. It returns the messages, which include a "created" message
  when the file was missing. It raises on invalid settings JSON or a missing
  template, and writes nothing in either case.
- ``main(argv: list[str]) -> int``: the CLI entry point (argv excludes the
  program name).

Rules (forced = overwritten every run; merged = entries added if missing, never
removed; seeded = written only if absent):

- forced   ``showThinkingSummaries: true``
- removed  ``effortLevel``
- promoted ``theme`` from unset or ``"auto"`` to ``"dark-ansi"``; any other
  value is kept
- seeded   the template's whole ``permissions`` block, when ``permissions`` is
  absent
- forced   ``attribution: {"commit": "", "pr": ""}``, and ``coauthorTag``
  removed (Phase 3; ``sessionUrl`` dropped because CLIs up to at least 2.1.100
  skip a settings file that sets it)
- forced   ``prefersReducedMotion: true`` (Phase 4)
- merged   the template's ``permissions.deny`` entries into an existing
  ``permissions.deny``, in a union that leaves the user's allow, ask and deny
  entries untouched (Phase 5)

Phase gating
------------

Tests whose behavior lands in a later phase carry a ``PENDING_PHASE_<N>``
marker. The worker for phase N deletes that marker's definition below and
every ``@PENDING_PHASE_<N>`` line. Every test also skips while the helper file
is missing, so the suite stays green until Phase 2 creates it.
"""
import ast
import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HELPER = REPO / "merge_global_settings.py"
TEMPLATE = REPO / "templates" / "global-claude-settings.json"
SCRIPTS = [REPO / "start-claude.sh", REPO / "start-agent.sh"]

PENDING_PHASE_5 = pytest.mark.skip(
    reason="pending Phase 5 (merge push/publish deny list); Phase 5 removes this marker"
)

EXPECTED_ATTRIBUTION_STRINGS = {"commit": "", "pr": ""}


# ── fixtures and helpers ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def helper():
    if not HELPER.exists():
        pytest.skip(f"{HELPER.name} not written yet (Phase 2 creates it)")
    spec = importlib.util.spec_from_file_location("merge_global_settings", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def template():
    with TEMPLATE.open() as f:
        return json.load(f)


def _run_cli(settings: Path, template_path: Path = TEMPLATE):
    return subprocess.run(
        [sys.executable, str(HELPER), str(settings), str(template_path)],
        capture_output=True,
        text=True,
    )


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def _merged(helper, data: dict, template: dict) -> dict:
    data = copy.deepcopy(data)
    helper.apply_rules(data, template)
    return data


def _user_permissions() -> dict:
    return {
        "allow": ["Bash(my-tool*)", "Read"],
        "deny": ["Bash(rm -rf /*)", "Bash(git push*)"],
        "ask": ["Bash(kubectl*)"],
        "defaultMode": "acceptEdits",
        "additionalDirectories": ["/data"],
    }


# ── Phase 2: interface and parity with the old heredocs ──────────────────────

def test_helper_is_stdlib_only(helper):
    tree = ast.parse(HELPER.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    non_stdlib = sorted(m for m in modules if m not in sys.stdlib_module_names)
    assert not non_stdlib, f"helper must be stdlib-only, imports: {non_stdlib}"


def test_helper_parses_as_python_3_9(helper):
    # macOS ships python3 3.9 as /usr/bin/python3, and both scripts run the helper on the host.
    ast.parse(HELPER.read_text(), feature_version=(3, 9))


def test_cli_creates_missing_file_from_template_with_rules_applied(helper, template, tmp_path):
    settings = tmp_path / "settings.json"
    result = _run_cli(settings)
    assert result.returncode == 0, result.stderr
    assert settings.exists()
    assert json.loads(settings.read_text()) == _merged(helper, template, template)
    assert result.stdout.strip(), "creating the file should be reported"


def test_missing_file_result_has_core_settings(helper, template, tmp_path):
    settings = tmp_path / "settings.json"
    helper.merge_file(settings, TEMPLATE)
    data = json.loads(settings.read_text())
    assert data["showThinkingSummaries"] is True
    assert data["theme"] == "dark-ansi"
    assert "effortLevel" not in data
    assert data["permissions"]["allow"] == template["permissions"]["allow"]
    assert set(template["permissions"]["deny"]) <= set(data["permissions"]["deny"])


@pytest.mark.parametrize("initial", [{}, {"showThinkingSummaries": False}, {"showThinkingSummaries": "yes"}])
def test_show_thinking_summaries_forced_true(helper, template, initial):
    assert _merged(helper, initial, template)["showThinkingSummaries"] is True


def test_effort_level_removed(helper, template):
    assert "effortLevel" not in _merged(helper, {"effortLevel": "high"}, template)


@pytest.mark.parametrize("initial", [{}, {"theme": None}, {"theme": "auto"}])
def test_theme_promoted_to_dark_ansi(helper, template, initial):
    assert _merged(helper, initial, template)["theme"] == "dark-ansi"


@pytest.mark.parametrize("theme", ["dark", "light", "dark-daltonized", "light-ansi"])
def test_explicit_theme_preserved(helper, template, theme):
    assert _merged(helper, {"theme": theme}, template)["theme"] == theme


def test_permissions_seeded_when_absent(helper, template):
    data = _merged(helper, {}, template)
    assert data["permissions"]["allow"] == template["permissions"]["allow"]
    assert data["permissions"]["deny"] == template["permissions"]["deny"]


def test_seeded_permissions_are_not_aliased_to_template(helper, template):
    pristine = copy.deepcopy(template)
    data = {}
    helper.apply_rules(data, template)
    data["permissions"]["allow"].append("Bash(evil*)")
    data["permissions"]["deny"].clear()
    assert template == pristine


def test_apply_rules_does_not_mutate_template(helper, template):
    pristine = copy.deepcopy(template)
    helper.apply_rules({"theme": "auto", "effortLevel": "low"}, template)
    helper.apply_rules({"permissions": _user_permissions()}, template)
    assert template == pristine


def test_existing_permissions_allow_ask_and_other_keys_untouched(helper, template):
    user = _user_permissions()
    perms = _merged(helper, {"permissions": user}, template)["permissions"]
    for key in ("allow", "ask", "defaultMode", "additionalDirectories"):
        assert perms[key] == user[key], f"permissions.{key} must be left alone"
    assert "ask" not in _merged(helper, {"permissions": {"allow": ["Read"]}}, template)["permissions"]


def test_existing_user_deny_entries_preserved_in_order(helper, template):
    user = _user_permissions()
    deny = _merged(helper, {"permissions": user}, template)["permissions"]["deny"]
    assert [e for e in deny if e in user["deny"]] == user["deny"]


def test_unmanaged_keys_preserved(helper, template):
    initial = {
        "remoteControlAtStartup": True,
        "agentPushNotifEnabled": False,
        "autoCompactEnabled": True,
        "env": {"FOO": "bar"},
        "hooks": {"Stop": []},
    }
    data = _merged(helper, initial, template)
    for key, value in initial.items():
        assert data[key] == value


def test_apply_rules_reports_changes_and_none_when_conforming(helper, template):
    data = {"theme": "auto"}
    assert helper.apply_rules(data, template), "changes must be reported"
    assert helper.apply_rules(data, template) == [], "second pass must report no changes"


def test_conforming_file_is_not_rewritten(helper, template, tmp_path):
    settings = tmp_path / "settings.json"
    conforming = _merged(helper, {"theme": "light", "extra": 1}, template)
    compact = json.dumps(conforming, separators=(",", ":"))
    settings.write_text(compact)
    result = _run_cli(settings)
    assert result.returncode == 0, result.stderr
    assert settings.read_text() == compact, "a conforming file must keep its exact bytes"
    assert result.stdout == ""


@pytest.mark.parametrize("initial", [
    None,
    {},
    {"theme": "auto", "effortLevel": "high", "showThinkingSummaries": False},
    {"permissions": _user_permissions(), "coauthorTag": "none"},
    {"permissions": {"allow": ["Read"]}, "attribution": False, "prefersReducedMotion": False},
])
def test_cli_is_idempotent(helper, tmp_path, initial):
    settings = tmp_path / "settings.json"
    if initial is not None:
        _write(settings, initial)
    first = _run_cli(settings)
    assert first.returncode == 0, first.stderr
    after_first = settings.read_text()
    second = _run_cli(settings)
    assert second.returncode == 0, second.stderr
    assert second.stdout == "", f"second run must be a no-op, printed: {second.stdout!r}"
    assert settings.read_text() == after_first


def test_written_file_format(helper, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"theme":"auto"}')
    assert _run_cli(settings).returncode == 0
    text = settings.read_text()
    assert text == json.dumps(json.loads(text), indent=2) + "\n"


def test_cli_change_messages_are_prefixed(helper, tmp_path):
    settings = tmp_path / "settings.json"
    _write(settings, {"theme": "auto", "effortLevel": "high"})
    result = _run_cli(settings)
    lines = result.stdout.splitlines()
    assert lines, "changes must be reported"
    assert all(line.startswith("==> ") for line in lines), lines


def test_merge_file_returns_messages(helper, tmp_path):
    settings = tmp_path / "settings.json"
    assert helper.merge_file(settings, TEMPLATE), "creating the file must be reported"
    assert helper.merge_file(settings, TEMPLATE) == []


def test_main_entry_point(helper, tmp_path):
    settings = tmp_path / "settings.json"
    assert helper.main([str(settings), str(TEMPLATE)]) == 0
    assert settings.exists()


def test_invalid_json_fails_without_writing(helper, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    result = _run_cli(settings)
    assert result.returncode != 0
    assert result.stderr.strip()
    assert settings.read_text() == "{not json"
    with pytest.raises(Exception):
        helper.merge_file(settings, TEMPLATE)
    assert settings.read_text() == "{not json"


def test_missing_template_fails_without_writing(helper, tmp_path):
    missing_template = tmp_path / "nope.json"
    settings = tmp_path / "settings.json"
    result = _run_cli(settings, missing_template)
    assert result.returncode != 0
    assert not settings.exists()

    _write(settings, {"theme": "auto"})
    before = settings.read_text()
    result = _run_cli(settings, missing_template)
    assert result.returncode != 0
    assert settings.read_text() == before
    with pytest.raises(Exception):
        helper.merge_file(settings, missing_template)
    assert settings.read_text() == before


# ── Phase 2: both scripts call the helper ────────────────────────────────────

@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_calls_helper_without_inline_merge(helper, script):
    text = script.read_text()
    calls = [
        line for line in text.splitlines()
        if "merge_global_settings.py" in line and not line.lstrip().startswith("#")
    ]
    assert any("python3" in line for line in calls), (
        f"{script.name} must run merge_global_settings.py with python3"
    )
    assert "showThinkingSummaries" not in text, (
        f"{script.name} still carries the inline global-settings heredoc"
    )
    assert "coauthorTag" not in text, (
        f"{script.name} still hard-codes coauthorTag (heredoc or echo fallback)"
    )
    fallbacks = [
        line for line in text.splitlines()
        if re.search(r"echo\s+'\{", line) and "GLOBAL_SETTINGS_FILE" in line
    ]
    assert not fallbacks, (
        f"{script.name} still writes a hard-coded JSON settings fallback: {fallbacks}"
    )


# ── Phase 3: attribution replaces coauthorTag ────────────────────────────────

def _assert_attribution_off(data: dict) -> None:
    attribution = data.get("attribution")
    assert isinstance(attribution, dict), (
        f"attribution must be the object form (older CLIs reject false), got {attribution!r}"
    )
    for key, value in EXPECTED_ATTRIBUTION_STRINGS.items():
        assert attribution.get(key) == value, f"attribution.{key} must be {value!r}"
    # The plan's Unknowns may drop sessionUrl; if present it must be false.
    assert attribution.get("sessionUrl", False) is False


@pytest.mark.parametrize("initial", [
    {},
    {"attribution": False},
    {"attribution": {"commit": "Co-Authored-By: someone", "pr": "made by a bot"}},
    {"attribution": {"commit": "", "pr": "", "sessionUrl": True}},
    {"includeCoAuthoredBy": True},
])
def test_attribution_forced_off(helper, template, initial):
    _assert_attribution_off(_merged(helper, initial, template))


@pytest.mark.parametrize("value", ["none", "Claude", ""])
def test_coauthor_tag_removed(helper, template, value):
    assert "coauthorTag" not in _merged(helper, {"coauthorTag": value}, template)


def test_missing_file_gets_attribution_and_no_coauthor_tag(helper, tmp_path):
    settings = tmp_path / "settings.json"
    assert _run_cli(settings).returncode == 0
    data = json.loads(settings.read_text())
    _assert_attribution_off(data)
    assert "coauthorTag" not in data


# ── Phase 4: reduced motion ──────────────────────────────────────────────────

@pytest.mark.parametrize("initial", [{}, {"prefersReducedMotion": False}, {"prefersReducedMotion": "yes"}])
def test_prefers_reduced_motion_forced_true(helper, template, initial):
    assert _merged(helper, initial, template)["prefersReducedMotion"] is True


def test_missing_file_gets_reduced_motion(helper, tmp_path):
    settings = tmp_path / "settings.json"
    assert _run_cli(settings).returncode == 0
    assert json.loads(settings.read_text())["prefersReducedMotion"] is True


# ── Phase 5: push/publish deny list merged on every run ──────────────────────
#
# These tests pin behavior, not rule strings: Phase 5 picks the exact rules.
# _rule_matches approximates Claude Code's Bash rule matching. "*" matches any
# run of characters, a trailing " *" also matches nothing, and the legacy
# ":*" suffix is a prefix match.

def _rule_matches(rule: str, command: str) -> bool:
    m = re.fullmatch(r"Bash\((.*)\)", rule)
    if not m:
        return False
    pattern = m.group(1)
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + "*"
    optional_tail = pattern.endswith(" *")
    if optional_tail:
        pattern = pattern[:-2]
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    if optional_tail:
        regex += r"(?: .*)?"
    return re.fullmatch(regex, command, flags=re.DOTALL) is not None


def _denied(deny: list, command: str) -> bool:
    return any(_rule_matches(rule, command) for rule in deny)


DENIED_COMMANDS = [
    "git push origin main",
    "git push --force origin HEAD",
    "git -C . push origin main",
    "git -C /some/repo push",
    "git -c user.name=x push origin main",
    "git config alias.p push",
    "git config --global alias.p push",
    "git config --local alias.sync '!git push'",
    "cargo publish --dry-run",
    "uv publish dist/*",
    "docker push example/image:latest",
    "npm publish --access public",
    "yarn publish",
    "pnpm publish",
    "gh pr create --fill",
    "gh pr merge 12 --squash",
    "gh release create v1.0.0",
    "gh repo create me/new --public",
    "gh api repos/o/r/pulls -X POST",
]

ALLOWED_COMMANDS = [
    "git commit -m \"push fix\"",
    "git commit -m 'publish docs'",
    "git status",
    "git log --oneline",
    "git config user.email a@example.com",
    "git config --global user.name Someone",
    "cargo build --release",
    "uv run pytest tests/",
    "docker build -t x .",
    "npm install",
]


@PENDING_PHASE_5
@pytest.mark.parametrize("command", DENIED_COMMANDS)
def test_template_deny_covers_push_and_publish(template, command):
    assert _denied(template["permissions"]["deny"], command), (
        f"template deny list should block: {command}"
    )


@PENDING_PHASE_5
@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_template_deny_spares_ordinary_commands(template, command):
    hits = [r for r in template["permissions"]["deny"] if _rule_matches(r, command)]
    assert not hits, f"{command!r} should not be denied, matched by {hits}"


@PENDING_PHASE_5
def test_template_deny_entries_merged_into_existing_deny(helper, template):
    user = _user_permissions()
    deny = _merged(helper, {"permissions": user}, template)["permissions"]["deny"]
    missing = [e for e in template["permissions"]["deny"] if e not in deny]
    assert not missing, f"template deny entries not merged: {missing}"
    assert [e for e in deny if e in user["deny"]] == user["deny"]
    assert len(deny) == len(set(deny)), f"deny list has duplicates: {deny}"


@PENDING_PHASE_5
def test_deny_created_when_permissions_lacks_it(helper, template):
    perms = _merged(helper, {"permissions": {"allow": ["Read"]}}, template)["permissions"]
    assert perms["allow"] == ["Read"]
    assert set(template["permissions"]["deny"]) <= set(perms.get("deny", []))


@PENDING_PHASE_5
def test_deny_merge_is_idempotent(helper, template):
    data = {"permissions": _user_permissions()}
    assert helper.apply_rules(data, template), "merging new deny entries must be reported"
    once = copy.deepcopy(data)
    assert helper.apply_rules(data, template) == []
    assert data == once


@PENDING_PHASE_5
@pytest.mark.parametrize("command", DENIED_COMMANDS)
def test_merged_user_file_blocks_push_and_publish(helper, template, command):
    deny = _merged(helper, {"permissions": {"allow": ["Bash(git*)"], "deny": []}}, template)["permissions"]["deny"]
    assert _denied(deny, command)
