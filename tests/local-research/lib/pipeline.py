"""
Per-round search + rerank pipeline, plus fetch + extract + notes.
"""
import json
import pathlib
import sys
import time

from lib import bundle as _bundle_mod
from lib import expand as _expand_mod
from lib import extract as _extract_mod
from lib import fetch as _fetch_mod
from lib import notes as _notes_mod
from lib import rerank as _rerank_mod
from lib import search as _search_mod
from lib import source_priors as _priors_mod


def gather_sources(
    query: str,
    exclude_urls: set[str] | None = None,
    n_expansions: int = 4,
    n_per_query: int = 20,
    top_k: int = 15,
    include_seed: bool = False,
) -> dict:
    """
    expand → search-each → flatten → dedupe → apply priors → rerank

    Per-expansion routing is read from the EXPAND_ROUTING env var: a JSON list
    of {categories, engines} dicts indexed by expansion position (post-seed-
    drop). Missing slots default to {categories: null, engines: null}.
    Example:
        EXPAND_ROUTING='[{"categories": null, "engines": null},
                         {"categories": "science", "engines": null}]'

    include_seed controls whether the original query (index 0 of the expand
    output) is included as the first search call. False (the default) drops the
    seed; this is strongly recommended for long natural-language questions where
    the seed crowds out short keyword expansions in the top-N result slots.

    Returns:
        {
            query: str,
            expansions: list[str],   # includes original at index 0
            raw_results: list[dict], # deduped union before rerank
            ranked: list[dict],      # top_k after rerank
            timings: dict,
        }
    """
    import os

    _routing_raw = os.environ.get("EXPAND_ROUTING") or "[]"
    try:
        per_expansion_routing: list[dict] = json.loads(_routing_raw)
    except (json.JSONDecodeError, TypeError):
        per_expansion_routing = []

    exclude_urls = exclude_urls or set()
    t0 = time.monotonic()

    expansions = _expand_mod.expand(query, n=n_expansions)
    t_expand = time.monotonic()

    to_search = expansions if include_seed else expansions[1:]

    # Search each expansion, flatten, dedupe by URL.
    seen_urls: set[str] = set(exclude_urls)
    raw: list[dict] = []
    for i, q in enumerate(to_search):
        slot = per_expansion_routing[i] if i < len(per_expansion_routing) else {}
        categories = (slot or {}).get("categories")
        engines = (slot or {}).get("engines")
        for r in _search_mod.search(q, n=n_per_query, categories=categories, engines=engines):
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                raw.append(r)
    t_search = time.monotonic()

    # Apply domain-quality priors before rerank so the embedder sees them.
    raw_with_priors = _priors_mod.apply_priors(raw)

    ranked = _rerank_mod.rerank(query, raw_with_priors, top_k=top_k, exclude_urls=exclude_urls)
    t_rerank = time.monotonic()

    return {
        "query": query,
        "expansions": expansions,
        "raw_results": raw_with_priors,
        "ranked": ranked,
        "timings": {
            "expand_s": round(t_expand - t0, 2),
            "search_s": round(t_search - t_expand, 2),
            "rerank_s": round(t_rerank - t_search, 2),
            "total_s": round(t_rerank - t0, 2),
        },
    }


def fetch_and_note(
    ranked: list[dict],
    round_dir: pathlib.Path,
    round_idx: int,
    branch_label: str,
    query: str = "",
) -> list[dict]:
    """
    For each ranked source: fetch → extract → note → write_source.

    Per-source failures are caught and recorded; the batch continues.
    Returns a list of source-meta dicts (one per input), always the same length
    as ranked.  Failed entries have error != None.
    """
    from lib.config import NOTES_MODEL

    metas: list[dict] = []
    for idx, source in enumerate(ranked, start=1):
        url = source.get("url", "")
        title = source.get("title", "")
        engine = source.get("engine", "")
        rerank_score = source.get("rerank_score", 0.0)

        base_meta = {
            "url": url,
            "title": title,
            "round_idx": round_idx,
            "branch_label": branch_label,
            "engine": engine,
            "rerank_score": rerank_score,
            "error": None,
        }

        print(f"  [{idx}/{len(ranked)}] {url}", file=sys.stderr)

        try:
            t_fetch_start = time.monotonic()
            body, fetch_meta = _fetch_mod.fetch(url)
            t_fetch_end = time.monotonic()

            extracted = _extract_mod.extract(body, url)
            t_extract_end = time.monotonic()

            note = _notes_mod.note_for_source(
                query=query,
                source_text=extracted,
                meta={"url": url, "title": title},
            )
            t_note_end = time.monotonic()

            timings = {
                "fetch_s": round(t_fetch_end - t_fetch_start, 3),
                "extract_s": round(t_extract_end - t_fetch_end, 3),
                "note_s": round(t_note_end - t_extract_end, 3),
            }

            file_path = _bundle_mod.write_source(
                round_dir=round_dir,
                idx=idx,
                url=url,
                title=title,
                round_idx=round_idx,
                branch_label=branch_label,
                engine=engine,
                rerank_score=rerank_score,
                note=note,
                extracted_text=extracted,
                timings=timings,
                model=NOTES_MODEL,
            )

            metas.append({
                **base_meta,
                "file_path": str(file_path),
                "fetch_meta": fetch_meta,
                "timings": timings,
            })

        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED: {exc}", file=sys.stderr)
            metas.append({**base_meta, "error": str(exc)})

    return metas
