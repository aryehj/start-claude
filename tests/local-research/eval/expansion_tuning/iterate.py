#!/usr/bin/env python3
"""
Phase 6.5 iteration harness — agent-as-loop query-expansion tuning.

Throwaway. Reads `iter_config.json` in this directory, calls the actual local
expand model (omlx) via `lib.expand.expand`, fans out per-expansion to SearXNG
via `lib.search.search`, and appends a row to `iterations.jsonl`. The agent
then Edits the row in-place to set `rationale` and `kept_or_reverted`.

The agent IS the judge. No score column, no LLM rubric.

Stdlib + project-local imports only. `lib.omlx` brings `requests` as a
transitive dep — install via `uv run --with requests` or set
`PYTHONPATH=tests/local-research` inside an env that already has requests.

Usage (from project root):
    PYTHONPATH=tests/local-research \\
    OMLX_BASE_URL=$OMLX_HOST/v1 OMLX_API_KEY=$OMLX_API_KEY \\
    SEARXNG_URL=http://searxng:8080 \\
    EXPAND_MODEL=gemma-4-e4b-it-6bit \\
    uv run --with requests python3 \\
        tests/local-research/eval/expansion_tuning/iterate.py \\
        --top-n 15 --axis-touched A_prompt \\
        --mutation-summary "keyword-distillation prompt"

The default `EXPAND_MODEL` in `lib/config.py` (`gemma-4-E4B-it-MLX-8bit`) is
NOT present on the omlx server we tested against; the harness leaves model
selection to the caller via the env var.
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import sys
import time


HERE = pathlib.Path(__file__).resolve().parent
ITER_CONFIG_PATH = HERE / "iter_config.json"
ITERATIONS_PATH = HERE / "iterations.jsonl"

QUERIES = {
    # carried over from Phase 6
    "q3": (
        "A cyclist develops stubborn medial knee pain that comes on during long rides "
        "and lingers for days afterward. What are the most likely diagnoses, how do "
        "bike-fit and biomechanical factors contribute to each, and what clinical features "
        "would help distinguish between them?"
    ),
    "creatine": "is creatine safe to take long term",
    "finance-team": (
        "is it unusual for a 60-person software consulting company to have a 4-person "
        "finance team"
    ),
    # new for Phase 6.5: recent-news factual lookup (verifiable number)
    "recent-news": (
        "2024 united states presidential election popular vote margin"
    ),
    # new for Phase 6.5: long natural-language non-medical-but-clinical-shaped question
    "long-form-runner": (
        "a runner who has never had heel pain before suddenly develops stiffness on the "
        "bottom of the foot in the morning that loosens up after walking — what are the "
        "most likely causes and how would a clinician distinguish between them?"
    ),
}


# ── pure helpers (covered by tests/test_expansion_tuning.py) ─────────────────

def config_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def next_iter(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    last = -1
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = max(last, int(json.loads(line).get("iter", -1)))
        except (json.JSONDecodeError, ValueError):
            continue
    return last + 1


def routing_for(per_expansion_routing: list[dict], i: int) -> dict:
    if 0 <= i < len(per_expansion_routing):
        slot = per_expansion_routing[i] or {}
        return {
            "categories": slot.get("categories"),
            "engines": slot.get("engines"),
        }
    return {"categories": None, "engines": None}


def expansions_for_search(expansions: list[str], include_seed: bool) -> list[str]:
    """`lib.expand.expand` always prepends the seed at position 0; drop it if
    the iteration config sets include_seed=False."""
    if include_seed:
        return list(expansions)
    return list(expansions[1:])


# ── harness ──────────────────────────────────────────────────────────────────

def _load_iter_config() -> dict:
    text = ITER_CONFIG_PATH.read_text()
    cfg = json.loads(text)
    cfg.setdefault("prompt_template", None)
    cfg.setdefault("n_expansions", 4)
    cfg.setdefault("include_seed", True)
    cfg.setdefault("per_expansion_routing", [])
    cfg["_sha"] = config_sha(text)
    return cfg


def _install_temperature_zero_chat() -> None:
    """Wrap lib.omlx.chat so every expansion call passes temperature=0.
    Determinism check (Unknown #5) confirmed temperature=0 produces identical
    output across repeated calls for gemma-4-e4b-it-6bit."""
    from lib import omlx
    _orig = omlx.chat

    def _det_chat(model, messages, **kw):
        kw.setdefault("temperature", 0)
        return _orig(model, messages, **kw)

    omlx.chat = _det_chat  # type: ignore[assignment]


def _set_active_prompt_env(prompt_template: str | None) -> None:
    """`lib.expand` reads EXPAND_PROMPT at import time. Set it before importing."""
    if prompt_template:
        os.environ["EXPAND_PROMPT"] = prompt_template


def main() -> None:
    p = argparse.ArgumentParser(description="Phase 6.5 expansion-tuning harness")
    p.add_argument("--top-n", type=int, default=15)
    p.add_argument("--n-per-query", type=int, default=20,
                   help="SearXNG results per expansion before dedupe")
    p.add_argument("--axis-touched", default="",
                   help="A_prompt | B_n | C_routing | D_seed | stacking")
    p.add_argument("--mutation-summary", default="", help="one-line description of the change")
    p.add_argument("--queries", default=",".join(QUERIES),
                   help="comma-separated subset of fixture slugs")
    p.add_argument("--search-delay", type=float, default=2.0,
                   help="seconds to sleep between each SearXNG call (avoids engine rate-limits)")
    args = p.parse_args()

    slugs = [s.strip() for s in args.queries.split(",") if s.strip()]
    unknown = [s for s in slugs if s not in QUERIES]
    if unknown:
        print(f"unknown query slugs: {unknown}; valid: {list(QUERIES)}", file=sys.stderr)
        sys.exit(2)

    cfg = _load_iter_config()
    _set_active_prompt_env(cfg["prompt_template"])

    # Import after env is set so lib.expand picks up EXPAND_PROMPT.
    from lib import expand as _expand_mod
    from lib import search as _search_mod
    _install_temperature_zero_chat()

    expansions_per_query: dict[str, list[str]] = {}
    top_per: dict[str, list[dict]] = {}
    expansion_counts: dict[str, int] = {}
    result_counts: dict[str, int] = {}

    for slug in slugs:
        query = QUERIES[slug]
        print(f"  [{slug}] expanding ...", file=sys.stderr)
        expansions = _expand_mod.expand(query, n=cfg["n_expansions"])
        expansions_per_query[slug] = expansions
        to_search = expansions_for_search(expansions, cfg["include_seed"])
        expansion_counts[slug] = len(to_search)

        seen_urls: set[str] = set()
        flat: list[dict] = []
        for i, exp in enumerate(to_search):
            r = routing_for(cfg["per_expansion_routing"], i)
            print(f"    [{slug}] search pos={i} cats={r['categories']} eng={r['engines']}: {exp[:60]!r}",
                  file=sys.stderr)
            if i > 0 and args.search_delay > 0:
                time.sleep(args.search_delay)
            results = _search_mod.search(
                exp,
                n=args.n_per_query,
                categories=r["categories"],
                engines=r["engines"],
            )
            for res in results:
                url = res.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    flat.append({
                        "url": url,
                        "title": res.get("title", ""),
                        "content": (res.get("content") or "")[:400],
                        "engines": res.get("engines") or (
                            [res.get("engine")] if res.get("engine") else []
                        ),
                        "from_position": i,
                    })
        top_per[slug] = flat[: args.top_n]
        result_counts[slug] = len(top_per[slug])

    iter_n = next_iter(ITERATIONS_PATH)
    row = {
        "iter": iter_n,
        "ts": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "config_sha": cfg["_sha"],
        "top_n": args.top_n,
        "axis_touched": args.axis_touched,
        "mutation_summary": args.mutation_summary,
        "rationale": "",
        "kept_or_reverted": "",
        "expansions_per_query": expansions_per_query,
        "top_ranked_per_query": top_per,
    }

    ITERATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ITERATIONS_PATH.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "iter": iter_n,
        "config_sha": cfg["_sha"],
        "queries": slugs,
        "expansion_counts": expansion_counts,
        "result_counts": result_counts,
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
