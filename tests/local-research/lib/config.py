"""
Central constants for the local-research harness.
All model IDs and key URLs are env-var overridable so `bootstrap.sh` can
pass the right values without touching the source.
"""
import os

# Use `or` rather than os.environ.get(..., default) so that empty-string env
# vars (which bootstrap.sh forwards when the host var is unset) fall back to
# the default.
SEARXNG_URL = os.environ.get("SEARXNG_URL") or "http://research-searxng:8080"

OMLX_BASE_URL = os.environ.get("OMLX_BASE_URL") or "http://host.docker.internal:8000/v1"
OMLX_API_KEY = os.environ.get("OMLX_API_KEY", "")

# Model IDs — confirm against /v1/models on first run; override via env vars.
EMBED_MODEL = os.environ.get("EMBED_MODEL") or "nomicai-modernbert-embed-base-bf16"
EXPAND_MODEL = os.environ.get("EXPAND_MODEL") or "gemma-4-E4B-it-MLX-8bit"
NOTES_MODEL = os.environ.get("NOTES_MODEL") or "gemma-4-26b-a4b-it-8bit"
SYNTH_MODEL = os.environ.get("SYNTH_MODEL") or "gemma-4-26b-a4b-it-8bit"

SESSION_ROOT = os.environ.get("SESSION_ROOT") or "/sessions"

# Per-expansion routing (Phase 6.5).
# JSON list of {categories, engines} dicts; read by pipeline.gather_sources via EXPAND_ROUTING.

# Batch-mode termination budgets (Phase 8).
MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS") or "4")
MAX_SOURCES = int(os.environ.get("MAX_SOURCES") or "80")
