"""Static checks for skills-agents/scope/SKILL.md (small-model adaptation)."""
import re
from pathlib import Path

SKILL_FILE = Path(__file__).parent.parent / "skills-agents" / "scope" / "SKILL.md"
_TEXT = SKILL_FILE.read_text() if SKILL_FILE.exists() else ""
_LINES = _TEXT.splitlines()

# ── Frontmatter ──────────────────────────────────────────────────────────────

def test_file_exists():
    assert SKILL_FILE.exists(), f"Missing {SKILL_FILE}"


def test_has_name_frontmatter():
    assert re.search(r"^name:\s*scope", _TEXT, re.MULTILINE), (
        "Frontmatter must include 'name: scope'"
    )


def test_has_description_frontmatter():
    assert re.search(r"^description:", _TEXT, re.MULTILINE), (
        "Frontmatter must include a 'description:' field (required by Agent Skills standard)"
    )


def test_no_argument_hint_frontmatter():
    assert "argument-hint:" not in _TEXT, (
        "Claude-Code-only 'argument-hint:' field should be removed; "
        "Pi passes arguments as a user message instead"
    )


def test_no_model_frontmatter():
    assert not re.search(r"^model:", _TEXT, re.MULTILINE), (
        "Claude-Code-only 'model:' frontmatter field should be removed; Pi ignores it"
    )

# ── No Claude-only tool references ───────────────────────────────────────────

def test_no_ask_user_question():
    assert "AskUserQuestion" not in _TEXT, (
        "AskUserQuestion is a Claude Code-only tool; remove it from the small-model skill"
    )


def test_no_agent_tool():
    # 'Agent' alone could appear in prose; look for the tool invocation pattern
    assert not re.search(r"\bAgent\b.*tool|\btool.*\bAgent\b|use.*\bAgent\b", _TEXT, re.IGNORECASE), (
        "Agent (subagent dispatch) is a Claude Code-only tool; remove it from the small-model skill"
    )


def test_no_task_create():
    assert "TaskCreate" not in _TEXT, (
        "TaskCreate is a Claude Code-only tool; use a TODO file instead"
    )


def test_no_task_update():
    assert "TaskUpdate" not in _TEXT, (
        "TaskUpdate is a Claude Code-only tool; use a TODO file instead"
    )


def test_no_arguments_placeholder():
    assert "$ARGUMENTS" not in _TEXT, (
        "$ARGUMENTS is a Claude Code placeholder; Pi passes arguments as a user message — "
        "the skill should reference 'the user's request' instead"
    )

# ── Size ─────────────────────────────────────────────────────────────────────

def test_line_count_within_target():
    # Plan targets 50–80 lines; allow ±20% tolerance for a useful non-trivial skill
    assert len(_LINES) <= 100, (
        f"Skill is {len(_LINES)} lines — target is 50–80. "
        "Strip decorative sections to keep it tractable for small models."
    )


def test_line_count_not_trivial():
    assert len(_LINES) >= 30, (
        f"Skill is only {len(_LINES)} lines — too thin to be useful. "
        "Include the key process steps."
    )

# ── Structure ────────────────────────────────────────────────────────────────

def test_mentions_plans_directory():
    assert "plans/" in _TEXT, (
        "Skill must instruct the model to write the plan to plans/ directory"
    )


def test_mentions_todo_or_checklist():
    assert re.search(r"todo|checklist|\.todo\.md", _TEXT, re.IGNORECASE), (
        "Skill should use a TODO file or checklist for tracking (replaces TaskCreate)"
    )
