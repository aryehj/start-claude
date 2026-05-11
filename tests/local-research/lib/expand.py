"""
Query expansion via the EXPAND_MODEL.
Returns [original_query, *expansions]; callers pass include_seed=False to drop
the seed before searching (recommended for long natural-language questions where
the seed crowds out keyword expansions in top-N slots).

Prompt selection (in priority order):
  1. EXPAND_PROMPT env var — raw template string with {n} and {query} placeholders
  2. EXPAND_PROMPT_NAME env var — one of: keyword-distillation, generic, scholarly-tilt, anti-seo
  3. Default: keyword-distillation
"""
import os

from lib.config import EXPAND_MODEL
from lib import omlx

_PROMPTS = {
    "keyword-distillation": (
        "Output {n} short search-keyword strings (3-6 tokens each) suitable for a search "
        "engine, derived from the question. No natural-language phrasings; no quoting; no "
        "site: operators. Output exactly one keyword string per line with no numbering, "
        "bullets, or extra text.\n\nQuery: {query}"
    ),

    "generic": (
        "Generate {n} alternative phrasings of the following research query. "
        "Cover different angles: technical vs. lay terminology, narrow vs. broad scope, "
        "and alternative domain vocabulary. Output exactly one phrasing per line with no "
        "numbering, bullets, or extra text.\n\nQuery: {query}"
    ),

    "scholarly-tilt": (
        "Generate {n} alternative phrasings of the following research query, "
        "designed to surface peer-reviewed and authoritative sources. "
        "Include at least one phrasing as a quoted exact phrase, "
        "one phrasing with 'site:edu OR site:gov', "
        "one phrasing that appends a methodology term such as "
        "'meta-analysis', 'review article', 'longitudinal study', or 'RCT', "
        "and one phrasing in field-jargon register used by practitioners or researchers. "
        "Output exactly one phrasing per line with no numbering, bullets, or extra text.\n\n"
        "Query: {query}"
    ),

    "anti-seo": (
        "Generate {n} alternative phrasings of the following research query "
        "that avoid promotional and listicle-style language. "
        "Prohibit phrasings that begin with or contain 'best', 'top N', 'guide to', "
        "'how to', or 'X vs Y' comparisons. "
        "Require at least one phrasing in the form 'evidence on ...' or "
        "'what the research says about ...'. "
        "Output exactly one phrasing per line with no numbering, bullets, or extra text.\n\n"
        "Query: {query}"
    ),
}

_raw_env = os.environ.get("EXPAND_PROMPT") or ""
_name_env = os.environ.get("EXPAND_PROMPT_NAME") or "keyword-distillation"

if _raw_env:
    _ACTIVE_PROMPT = _raw_env
elif _name_env in _PROMPTS:
    _ACTIVE_PROMPT = _PROMPTS[_name_env]
else:
    _ACTIVE_PROMPT = _PROMPTS["generic"]


def expand(query: str, n: int = 4) -> list[str]:
    prompt = _ACTIVE_PROMPT.format(n=n, query=query)
    raw = omlx.chat(EXPAND_MODEL, [{"role": "user", "content": prompt}])
    expansions = [line.strip() for line in raw.splitlines() if line.strip()]
    return [query, *expansions]
