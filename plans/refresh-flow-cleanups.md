# Refresh-flow and version-check cleanups (stub)

Findings left out of earlier fixes, from two reviews of `dependency-update-trixie`:

- Findings 1–2: the 2026-11-04 `/code-review high` (see `plans/implemented - safe-tool-version-updates.md` and ADR-054).
- Findings 3–12: the 2026-11-04 `/code-review medium` of `main...HEAD`, run after the trixie plan was wrapped. None were in the trixie or `docpython` changes. Only finding 4 was checked against the code. The rest are as the reviewer reported them, so confirm each before fixing.

None of these is high severity. Scope with `/scope` before building. Findings 4, 5, 7, 10 and 11 touch the same code as finding 2 (when and against which image the versions are recorded), so scope them together.

## Status

- [ ] SearXNG container is recreated by `--refresh-tools` and `--restore-previous-image`
- [ ] `record_tool_versions` call sites consolidated
- [ ] sandbox-runtime update check reads the pin, not the installed version
- [ ] warm reattach never records tool versions, so older images get no OpenCode/Pi warnings
- [ ] version record compared against `latest`, not the container's image
- [ ] restored 1.x OpenCode looked up in `@opencode/cli`'s release list
- [ ] plain build can reuse a stale npm layer kept by `:previous`
- [ ] bing migration matches `- bing` anywhere in `settings.yml`
- [ ] `bump-searxng.sh` rewrites the whole comment block and stamps "known good" before verification
- [ ] duplicate `docker image inspect` in `record_tool_versions`
- [ ] version probes run one after the other (up to 60 s on launch)
- [ ] version-check shell helpers copied between the two launchers

## Findings

### 1. SearXNG recreated needlessly

`start-agent.sh:1497` and `:1509` call `remove_containers` (defined at `:617`), which removes both the agent container and the `searxng` container. Neither flag changes SearXNG's image or `settings.yml`, so this only adds startup time and a short window with no search.

Direction: remove only `$CONTAINER_NAME` on these two paths. The SearXNG lifecycle already recreates the container when its pin or settings change (ADR-048, ADR-054). Check `tests/test_tool_refresh.py` and the CLAUDE.md/README text that says "recreates both containers".

### 2. `record_tool_versions` called from many places

`record_tool_versions` (`start-agent.sh:1416`) is called at `:1480`, `:1499`, `:1507`, `:1516`, `:1523`, and again unconditionally on the fresh-container path at `:2179`. It is a no-op when the record already matches the image, so the extra calls cost one `docker image inspect` plus a grep each. The real cost is that the logic for when versions are recorded is spread across six call sites.

Direction: keep the snapshots taken *before* an image moves (refresh and restore need the old versions for `print_tool_versions_change`). Record the new image once, in one place after the image lifecycle. Make sure `tests/test_tool_refresh.py` still covers "refresh summary shows `?` when recording fails".

### 3. sandbox-runtime check reads the pin, not the installed version

`start-claude.sh:670`: the launch-time check compares the sandbox-runtime pin written in the script against npm. A container built on an older pin (e.g. 0.0.73, before the bump to 0.0.81) that is reattached without `--rebuild` never warns, though it keeps running the old version. ADR-055 chose the pin as "in use", but this is the staleness the check exists for.

Direction: read the version installed in the image or container, and fall back to the pin. Or warn separately when the image predates the pin.

### 4. Warm reattach never records tool versions (checked)

`start-agent.sh:2195`: `record_tool_versions` runs only on the fresh-container path. `attach_existing` (`:2135`) goes straight to `report_version_check`. A user who upgrades with an existing container and image has no `tool-versions` file, so `check_versions.py` skips OpenCode and Pi on every launch until a `--reset-container`. The comment at `:2193` ("Images built before the version record existed get one here") reads as if it always covers them. ADR-054 says "never on a warm reattach", which was meant to avoid a container start per launch, not to leave old images without a record.

Direction: on reattach, record only when the file is missing or its `image_id` doesn't match. That costs one throwaway container once per image. Or fix the comment and document the gap.

### 5. Version record compared against `latest`, not the container's image

`start-agent.sh:459`: `tool_version` checks the record against `IMAGE_ID`, which is whatever `claude-agent:latest` points at. If a refresh moves `latest` and then dies before `remove_containers` (e.g. the `previous` tag at `:504` fails under `set -e`), or the user builds `latest` by hand, the surviving container runs the old binaries while the check reports on the new image.

Direction: on reattach, use the container's `.Image` rather than `latest`'s ID.

### 6. Restored 1.x OpenCode looked up in `@opencode/cli`

`start-agent.sh:1752`: after `--restore-previous-image` to an `opencode-ai` 1.x image, the recorded version (e.g. 1.14.x) is looked up in `@opencode/cli`'s npm time map. It has no entry there, so `evaluate()` returns None and the old OpenCode is never reported stale. ADR-056 already notes the silence before the first 2.x refresh. This is the same gap after a restore.

Direction: record the package name with the version, or pick the package by major version.

### 7. Plain build can reuse a stale npm layer

`dockerfiles/claude-agent-tools.Dockerfile:34`: a build with `latest` missing passes `TOOLS_CACHE_BUST=0`. If a still-tagged `:previous` was built with bust 0 on the same base, the legacy builder can match its npm layer and silently recreate the older OpenCode/Pi. This needs `latest` deleted by hand while `previous` stays.

Direction: always pass a fresh bust value when building `latest`, or remove `previous` when building `latest` from scratch.

### 8. Bing migration matches `- bing` anywhere

`start-agent.sh:361`: the check before appending the top-level `engines: - name: bing` entry matches `- bing` anywhere in `settings.yml`, not only in `use_default_settings.engines.keep_only`. If bing is dropped from `keep_only` but `- bing` appears in another list, the entry is appended and the container recreated. An entry for an engine that `keep_only` filters out breaks SearXNG at startup (ADR-054, first amendment).

Direction: match only inside the `keep_only` block. Add a test with `- bing` in another list.

### 9. `bump-searxng.sh` comment rewrite and "known good" date

`scripts/bump-searxng.sh:101`: the rewriter deletes every contiguous `#` line above `SEARXNG_IMAGE`, so a section header placed directly above the pin would be lost. It also stamps "known good on <today>" before `tests/verify-tool-refresh.sh` runs. `searxng_pinned_on` feeds that date to the staleness check, so a failed bump that is left uncommitted still resets the 30-day warning clock.

Direction: rewrite only the lines the script owns (match their prefix). Consider wording the stamp as the pin date, or reading the date from git.

### 10. Duplicate `docker image inspect` in `record_tool_versions`

`start-agent.sh:428`: `record_tool_versions` inspects `latest` itself although `load_image_id` has already set `IMAGE_ID`, leaving two sources for the current image ID. Fold into finding 2.

### 11. Version probes run sequentially

`start-agent.sh:433`: the throwaway container runs `opencode --version` and then `pi --version`, each with a 30 s timeout. Without network, OpenCode 2.x may stall reaching models.dev, so a fresh-container launch or refresh can block for up to 60 s, and a refresh can pay that twice. Unverified that 2.x actually stalls; measure first.

Direction: run both in the background in parallel, or shorten the timeouts.

### 12. Version-check helpers duplicated

`start-claude.sh:647`: `version_check_enabled`, `start_version_check` and `report_version_check` are near-copies of the ones in `start-agent.sh`. Changes to env-var parsing, the detach idiom or error silencing must be made twice.

Direction: a shared snippet, or a `check_versions.py` mode that detaches itself. Low priority; the two scripts deliberately share little shell code.

## Notes

- Run `uv run --no-project --with pytest pytest -q tests` (661 passed, 75 skipped as of 5a46ed9).
- Findings 1, 2, 4, 5, 7, 10 and 11 touch the refresh/restore or attach paths, so re-run `.temp/phase1-refresh-run.sh` (or `start-agent.sh --refresh-tools` + `tests/verify-tool-refresh.sh`) on the host afterwards. Findings 3, 4 and 6 change launch-time warnings; `tests/verify-version-check.sh` is the live check.
