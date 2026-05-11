"""Unit tests for pure helpers in the Phase 6.5 expansion-tuning harness."""
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).parent
ITERATE_PATH = (
    HERE.parent / "tests" / "local-research" / "eval" / "expansion_tuning" / "iterate.py"
)

_spec = importlib.util.spec_from_file_location("expansion_tuning_iterate", ITERATE_PATH)
iterate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iterate)


def test_config_sha_is_12_hex_chars():
    sha = iterate.config_sha('{"prompt_template": "foo"}')
    assert len(sha) == 12
    assert all(c in "0123456789abcdef" for c in sha)


def test_config_sha_changes_with_content():
    a = iterate.config_sha('{"n_expansions": 4}')
    b = iterate.config_sha('{"n_expansions": 6}')
    assert a != b


def test_next_iter_missing_file(tmp_path):
    assert iterate.next_iter(tmp_path / "absent.jsonl") == 0


def test_next_iter_increments(tmp_path):
    p = tmp_path / "iterations.jsonl"
    p.write_text(
        json.dumps({"iter": 0}) + "\n" + json.dumps({"iter": 1}) + "\n"
    )
    assert iterate.next_iter(p) == 2


def test_next_iter_skips_blank_and_malformed(tmp_path):
    p = tmp_path / "iterations.jsonl"
    p.write_text(
        json.dumps({"iter": 0}) + "\n\nnot-json\n" + json.dumps({"iter": 3}) + "\n"
    )
    assert iterate.next_iter(p) == 4


def test_routing_for_within_bounds():
    cfg = [
        {"categories": None, "engines": None},
        {"categories": "science", "engines": None},
    ]
    assert iterate.routing_for(cfg, 1) == {"categories": "science", "engines": None}


def test_routing_for_out_of_bounds_defaults():
    cfg = [{"categories": "science", "engines": None}]
    assert iterate.routing_for(cfg, 5) == {"categories": None, "engines": None}


def test_routing_for_empty_list_defaults():
    assert iterate.routing_for([], 0) == {"categories": None, "engines": None}


def test_expansions_for_search_keeps_seed():
    out = iterate.expansions_for_search(["seed", "alt1", "alt2"], include_seed=True)
    assert out == ["seed", "alt1", "alt2"]


def test_expansions_for_search_drops_seed():
    out = iterate.expansions_for_search(["seed", "alt1", "alt2"], include_seed=False)
    assert out == ["alt1", "alt2"]


def test_expansions_for_search_drops_seed_handles_single():
    out = iterate.expansions_for_search(["seed"], include_seed=False)
    assert out == []
