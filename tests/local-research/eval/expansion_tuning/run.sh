#!/usr/bin/env bash
# run.sh — convenience wrapper for the Phase 6.5 iteration harness.
#
# Runs from inside the claude-agent container against the local `searxng`
# container (which carries Phase 6's winning config) and omlx on the macOS
# host. Mirrors the env-var conventions of bootstrap.sh.
#
# Usage:
#   ./tests/local-research/eval/expansion_tuning/run.sh \
#       --axis-touched A_prompt --mutation-summary "iter-0 baseline"
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../../.." && pwd)"

: "${OMLX_HOST:?OMLX_HOST is not set in this shell}"
: "${OMLX_API_KEY:?OMLX_API_KEY is required}"

export OMLX_BASE_URL="${OMLX_BASE_URL:-${OMLX_HOST}/v1}"
export SEARXNG_URL="${SEARXNG_URL:-http://searxng:8080}"
# Default EXPAND_MODEL in lib/config.py points at a model ID that does not
# exist on the omlx server we tested (`gemma-4-E4B-it-MLX-8bit`). Pin to the
# E4B variant that is actually on /v1/models.
export EXPAND_MODEL="${EXPAND_MODEL:-gemma-4-e4b-it-6bit}"
export PYTHONPATH="${REPO_ROOT}/tests/local-research"

cd "$REPO_ROOT"
exec uv run --with requests python3 \
    "tests/local-research/eval/expansion_tuning/iterate.py" "$@"
