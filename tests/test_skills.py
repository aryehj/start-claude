"""Static checks for the Claude Code skills in skills/ and their small-model ports."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SKILLS = ROOT / "skills"
SKILLS_AGENTS = ROOT / "skills-agents"

SKILL_FILES = sorted(SKILLS.glob("*/SKILL.md"))
ALL_SKILL_TEXT_FILES = sorted(
    p for base in (SKILLS, SKILLS_AGENTS) for p in base.rglob("*.md")
)


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


# ── Task tools no longer exist ───────────────────────────────────────────────

@pytest.mark.parametrize("path", ALL_SKILL_TEXT_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_task_tools(path):
    hits = re.findall(r"\bTask(Create|List|Update|Get)\b", path.read_text())
    assert not hits, (
        f"{path.relative_to(ROOT)} references Task{hits[0]}; those tools no longer "
        "exist in Claude Code — track progress in the plan file instead"
    )


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
