# Refresh-flow cleanups (stub)

Two findings from the 2026-11-04 `/code-review high` of `dependency-update-trixie` that were left out of the fixes (see `plans/implemented - safe-tool-version-updates.md` and ADR-054). Neither is a correctness bug. Scope with `/scope` before building.

## Status

- [ ] SearXNG container is recreated by `--refresh-tools` and `--restore-previous-image`
- [ ] `record_tool_versions` call sites consolidated

## Findings

### 1. SearXNG recreated needlessly

`start-agent.sh:1497` and `:1509` call `remove_containers` (defined at `:617`), which removes both the agent container and the `searxng` container. Neither flag changes SearXNG's image or `settings.yml`, so this only adds startup time and a short window with no search.

Direction: remove only `$CONTAINER_NAME` on these two paths. The SearXNG lifecycle already recreates the container when its pin or settings change (ADR-048, ADR-054). Check `tests/test_tool_refresh.py` and the CLAUDE.md/README text that says "recreates both containers".

### 2. `record_tool_versions` called from many places

`record_tool_versions` (`start-agent.sh:1416`) is called at `:1480`, `:1499`, `:1507`, `:1516`, `:1523`, and again unconditionally on the fresh-container path at `:2179`. It is a no-op when the record already matches the image, so the extra calls cost one `docker image inspect` plus a grep each. The real cost is that the logic for when versions are recorded is spread across six call sites.

Direction: keep the snapshots taken *before* an image moves (refresh and restore need the old versions for `print_tool_versions_change`). Record the new image once, in one place after the image lifecycle. Make sure `tests/test_tool_refresh.py` still covers "refresh summary shows `?` when recording fails".

## Notes

- Run `uv run --no-project --with pytest pytest -q tests` (658 passed, 75 skipped as of 8586478).
- Both changes touch the refresh/restore branches, so re-run `.temp/phase1-refresh-run.sh` (or `start-agent.sh --refresh-tools` + `tests/verify-tool-refresh.sh`) on the host afterwards.
