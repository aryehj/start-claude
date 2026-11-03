"""Static checks for the Claude Code skills in skills/ and their small-model ports."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SKILLS = ROOT / "skills"
SKILLS_AGENTS = ROOT / "skills-agents"

SKILL_FILES = sorted(SKILLS.glob("*/SKILL.md"))
SMALL_MODEL_FILES = sorted(SKILLS_AGENTS.rglob("*.md"))
TASK_TOOL_RE = re.compile(r"\bTask(Create|List|Update|Get)\b")


def _frontmatter(text: str) -> list[str]:
    """Return the frontmatter lines, or fail if the block is malformed.

    Claude Code only recognises frontmatter that opens with `---` on line 1 and
    closes with `---` at column 0. An indented closing fence silently turns the
    whole block into body text, so `disable-model-invocation` and `model` are
    ignored.
    """
    lines = text.splitlines()
    assert lines and lines[0] == "---", "line 1 must be exactly '---'"
    try:
        end = lines.index("---", 1)
    except ValueError:
        pytest.fail("no closing '---' at column 0")
    return lines[1:end]


# ── Task tools ───────────────────────────────────────────────────────────────
# Claude Code's task tools are deferred: they exist, but only after ToolSearch
# loads them. A skill that requires them without loading them breaks.

@pytest.mark.parametrize("path", SMALL_MODEL_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_small_model_skills_have_no_task_tools(path):
    hit = TASK_TOOL_RE.search(path.read_text())
    assert not hit, (
        f"{path.relative_to(ROOT)} references {hit.group(0)}, a Claude Code-only tool; "
        "use a TODO file instead"
    )


@pytest.mark.parametrize(
    "path", sorted(SKILLS.rglob("*.md")), ids=lambda p: str(p.relative_to(ROOT))
)
def test_task_tools_are_loaded_and_optional(path):
    text = path.read_text()
    if not TASK_TOOL_RE.search(text):
        return
    assert "ToolSearch" in text, (
        f"{path.relative_to(ROOT)} uses the task tools but never says to load them "
        "with ToolSearch; they are deferred"
    )
    assert re.search(r"unavailable|not available|if .*can't be loaded", text, re.IGNORECASE), (
        f"{path.relative_to(ROOT)} must say what to do when the task tools are unavailable"
    )


def test_build_uses_task_tools():
    text = (SKILLS / "build" / "SKILL.md").read_text()
    assert "TaskCreate" in text and "TaskUpdate" in text


# ── Frontmatter ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_frontmatter_well_formed(path):
    fm = _frontmatter(path.read_text())
    names = [l for l in fm if l.startswith("name:")]
    assert names, "frontmatter needs a top-level 'name:' field (column 0)"
    assert names[0].split(":", 1)[1].strip() == path.parent.name, (
        "frontmatter name must match the skill directory name"
    )


# ── /build orchestration contract ────────────────────────────────────────────

BUILD = SKILLS / "build" / "SKILL.md"
PHASE = SKILLS / "build" / "phase.md"
SCOPE = SKILLS / "scope" / "SKILL.md"


def test_build_references_phase_file():
    assert "${CLAUDE_SKILL_DIR}/phase.md" in BUILD.read_text(), (
        "/build must point workers at its phase procedure via ${CLAUDE_SKILL_DIR}/phase.md"
    )


def test_phase_file_exists():
    assert PHASE.exists(), f"Missing {PHASE.relative_to(ROOT)}"


def test_build_supports_one_flag():
    assert "--one" in BUILD.read_text()


def test_human_gate_marker_shared():
    marker = "(human gate:"
    assert marker in SCOPE.read_text(), "/scope must define the human-gate marker"
    assert marker in BUILD.read_text(), "/build must recognise the human-gate marker"


def test_phase_requires_scripted_human_checks():
    text = PHASE.read_text()
    assert re.search(r"script", text, re.IGNORECASE)
    assert re.search(r"log", text, re.IGNORECASE), (
        "human-check scripts must write a log the agent reads back"
    )
