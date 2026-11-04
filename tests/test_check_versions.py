"""Launch-time staleness warnings: check_versions.py and its launcher wiring
(plans/safe-tool-version-updates.md, Phase 3; ADR-055).

Fixture JSON and a local HTTP server only; nothing here touches the network.
tests/verify-version-check.sh runs the helper against the real registries.
"""
import ast
import gzip
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HELPER = REPO / "check_versions.py"
AGENT = REPO / "start-agent.sh"
CLAUDE = REPO / "start-claude.sh"

DAY = 86400
NOW = 1_800_000_000.0  # 2027-01-15


@pytest.fixture
def cv(monkeypatch):
    spec = importlib.util.spec_from_file_location("check_versions", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def packument(latest="1.20.0", times=None):
    times = times or {
        "1.19.5": "2026-10-28T06:17:11.100Z",
        "1.20.0": "2026-12-10T01:02:03.456Z",
    }
    return {
        "name": "opencode-ai",
        "dist-tags": {"latest": latest},
        "time": {"created": "2025-01-01T00:00:00.000Z",
                 "modified": "2026-12-10T01:02:04.000Z",
                 "0.0.0-dev-202612100102": "2026-12-10T01:02:03.000Z",
                 **times},
        "versions": {v: {"version": v} for v in times},
    }


def dockerhub_tag(updated="2026-12-08T05:06:07.891011Z", digest="sha256:" + "a" * 64):
    return {"name": "latest", "last_updated": updated, "tag_last_pushed": updated,
            "digest": digest, "images": [{"digest": "sha256:" + "b" * 64}]}


def state_with(cv, **entries):
    sources = {}
    for name, data in entries.items():
        sources[cv.source_key(cv.COMPONENTS[name]["source"])] = {"checked": NOW, **data}
    return {"schema": cv.STATE_SCHEMA, "sources": sources}


# ── interface ─────────────────────────────────────────────────────────────────

def test_helper_is_stdlib_only():
    tree = ast.parse(HELPER.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    non_stdlib = sorted(m for m in modules if m not in sys.stdlib_module_names)
    assert not non_stdlib, f"helper must be stdlib-only, imports: {non_stdlib}"


def test_helper_parses_as_python_3_9():
    ast.parse(HELPER.read_text(), feature_version=(3, 9))


def test_components_name_their_update_command(cv):
    assert set(cv.COMPONENTS) == {"searxng", "opencode", "pi", "sandbox-runtime"}
    assert "scripts/bump-searxng.sh" in cv.COMPONENTS["searxng"]["update"]
    for name in ("opencode", "pi"):
        assert cv.COMPONENTS[name]["update"] == "start-agent.sh --refresh-tools"
    sr = cv.COMPONENTS["sandbox-runtime"]["update"]
    assert "start-claude.sh --rebuild" in sr and "tests/verify-sandbox-runtime.sh" in sr
    assert cv.DEFAULT_LAG_DAYS == 30


def test_npm_packages_match_the_installs():
    dockerfile = (REPO / "dockerfiles" / "claude-agent.Dockerfile").read_text()
    spec = importlib.util.spec_from_file_location("cv_static", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.COMPONENTS["opencode"]["source"][1] + "@latest" in dockerfile
    assert mod.COMPONENTS["pi"]["source"][1] + "@latest" in dockerfile
    assert mod.COMPONENTS["sandbox-runtime"]["source"][1] + "@" in CLAUDE.read_text()


# ── pure helpers ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("2026-10-28T06:17:11.100Z", (2026, 10, 28, 6, 17, 11)),
    ("2026-12-08T05:06:07.891011Z", (2026, 12, 8, 5, 6, 7)),
    ("2026-11-03", (2026, 11, 3, 0, 0, 0)),
    ("2026-11-03T00:00:00", (2026, 11, 3, 0, 0, 0)),
])
def test_parse_timestamp(cv, value, expected):
    assert cv.parse_timestamp(value).timetuple()[:6] == expected


@pytest.mark.parametrize("value", ["", "yesterday", "2026-13-01", "2026-02-30", None, 17, "Nov 3"])
def test_parse_timestamp_rejects_garbage(cv, value):
    assert cv.parse_timestamp(value) is None


@pytest.mark.parametrize("value,expected", [
    ("1.19.5", "1.19.5"),
    ("1.19.5\n", "1.19.5"),
    ("opencode 1.19.5", "1.19.5"),
    ("v0.0.81", "0.0.81"),
    ("1.20.0-beta.1", "1.20.0-beta.1"),
    ("", None), ("unknown", None), (None, None),
])
def test_normalize_version(cv, value, expected):
    assert cv.normalize_version(value) == expected


def test_parse_npm_keeps_releases_and_drops_snapshots(cv):
    out = cv.parse_npm(packument())
    assert out["latest"] == "1.20.0"
    assert set(out["times"]) == {"1.19.5", "1.20.0"}
    assert out["times"]["1.20.0"] == "2026-12-10T01:02:03"


@pytest.mark.parametrize("doc", [
    [], "x", {},
    {"dist-tags": {"latest": "1.0.0"}},
    {"time": {"1.0.0": "2026-01-01T00:00:00Z"}},
    {"dist-tags": {"latest": "1.0.0"}, "time": {"0.9.0": "2026-01-01T00:00:00Z"}},
    {"dist-tags": {"latest": "1.0.0"}, "time": {"1.0.0": "not a date"}},
    {"dist-tags": {"latest": 1}, "time": {"1": "2026-01-01T00:00:00Z"}},
    {"dist-tags": [], "time": {}},
])
def test_parse_npm_rejects_malformed(cv, doc):
    with pytest.raises(ValueError):
        cv.parse_npm(doc)


def test_parse_dockerhub(cv):
    out = cv.parse_dockerhub(dockerhub_tag())
    assert out == {"updated": "2026-12-08T05:06:07", "digest": "sha256:" + "a" * 64}
    # tag_last_pushed is the fallback when last_updated is missing
    doc = dockerhub_tag()
    del doc["last_updated"]
    assert cv.parse_dockerhub(doc)["updated"] == "2026-12-08T05:06:07"


@pytest.mark.parametrize("doc", [[], {}, {"last_updated": None}, {"last_updated": "soon"},
                                 {"name": "latest", "digest": "sha256:" + "a" * 64}])
def test_parse_dockerhub_rejects_malformed(cv, doc):
    with pytest.raises(ValueError):
        cv.parse_dockerhub(doc)


# ── lag computation ───────────────────────────────────────────────────────────

def _npm_state(cv, name="opencode", **kw):
    return state_with(cv, **{name: cv.parse_npm(packument(**kw))})


def test_npm_lag_over_threshold_warns_and_names_the_command(cv):
    line = cv.evaluate("opencode", "1.19.5", _npm_state(cv), 30)
    assert line is not None
    assert "OpenCode 1.19.5" in line and "43 days" in line and "1.20.0" in line
    assert "2026-12-10" in line
    assert line.endswith("start-agent.sh --refresh-tools")
    assert "\n" not in line


def test_npm_lag_threshold_is_exclusive(cv):
    state = _npm_state(cv)  # 2026-10-28 -> 2026-12-10 is 43 days
    assert cv.evaluate("opencode", "1.19.5", state, 43) is None
    assert cv.evaluate("opencode", "1.19.5", state, 42) is not None
    assert cv.evaluate("opencode", "1.19.5", state, 0) is not None


def test_npm_lag_under_threshold_is_silent(cv):
    state = _npm_state(cv, times={"1.19.5": "2026-12-01T00:00:00Z", "1.20.0": "2026-12-10T00:00:00Z"})
    assert cv.evaluate("opencode", "1.19.5", state, 30) is None


def test_lag_counts_calendar_days(cv):
    # 23:59 to 00:01 the next day is one day, not zero.
    state = _npm_state(cv, times={"1.19.5": "2026-12-01T23:59:00Z", "1.20.0": "2026-12-02T00:01:00Z"})
    assert "1 days" in cv.evaluate("opencode", "1.19.5", state, 0)


@pytest.mark.parametrize("in_use", ["1.20.0", "opencode 1.20.0\n"])
def test_npm_in_use_latest_is_silent(cv, in_use):
    assert cv.evaluate("opencode", in_use, _npm_state(cv), 0) is None


@pytest.mark.parametrize("in_use", ["", "?", "1.18.0", "1.21.0", "1.20.0-beta.1"])
def test_npm_in_use_without_a_known_date_is_silent(cv, in_use):
    # Not recorded, unparsable, older than the cache knows, newer than the
    # cached latest (refreshed since the last fetch), or a prerelease.
    assert cv.evaluate("opencode", in_use, _npm_state(cv), 0) is None


def test_npm_sandbox_runtime_line(cv):
    state = _npm_state(cv, name="sandbox-runtime", latest="0.0.90",
                       times={"0.0.81": "2026-10-20T01:13:23Z", "0.0.90": "2026-12-20T00:00:00Z"})
    line = cv.evaluate("sandbox-runtime", "0.0.81", state, 30)
    assert "sandbox-runtime 0.0.81" in line and "61 days" in line
    assert "start-claude.sh --rebuild" in line and "tests/verify-sandbox-runtime.sh" in line


def test_dockerhub_lag(cv):
    state = state_with(cv, searxng=cv.parse_dockerhub(dockerhub_tag()))
    line = cv.evaluate("searxng", "2026-11-03", state, 30)
    assert "SearXNG" in line and "2026-11-03" in line and "35 days" in line
    assert "2026-12-08" in line and "scripts/bump-searxng.sh" in line
    assert cv.evaluate("searxng", "2026-11-03", state, 35) is None
    assert cv.evaluate("searxng", "2026-12-01", state, 30) is None


@pytest.mark.parametrize("in_use", ["", "sha256:abc", "not-a-date"])
def test_dockerhub_bad_pin_date_is_silent(cv, in_use):
    state = state_with(cv, searxng=cv.parse_dockerhub(dockerhub_tag()))
    assert cv.evaluate("searxng", in_use, state, 0) is None


def test_missing_or_partial_cache_entry_is_silent(cv):
    empty = {"schema": cv.STATE_SCHEMA, "sources": {}}
    assert cv.evaluate("opencode", "1.19.5", empty, 0) is None
    only_checked = state_with(cv, opencode={}, searxng={})
    assert cv.evaluate("opencode", "1.19.5", only_checked, 0) is None
    assert cv.evaluate("searxng", "2026-01-01", only_checked, 0) is None
    assert cv.evaluate("nonesuch", "1.0.0", only_checked, 0) is None


# ── cache freshness ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("checked,fresh", [
    (NOW, True),
    (NOW - DAY + 60, True),
    (NOW - DAY, False),
    (NOW - 10 * DAY, False),
    (NOW + 3600, False),   # clock moved back: refetch rather than wait it out
    (None, False), ("yesterday", False), (True, False),
])
def test_is_fresh(cv, checked, fresh):
    entry = {} if checked is None else {"checked": checked}
    assert cv.is_fresh(entry, NOW) is fresh


def _fake_fetch(cv, monkeypatch, result):
    calls = []

    def fake(source):
        calls.append(source)
        if isinstance(result, Exception):
            raise result
        return result(source) if callable(result) else result

    monkeypatch.setattr(cv, "fetch_source", fake)
    return calls


def test_fetch_at_most_once_a_day(cv, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    calls = _fake_fetch(cv, monkeypatch, cv.parse_npm(packument()))
    args = ["--state", str(state), "opencode=1.19.5"]
    assert cv.run(args, now=NOW) and len(calls) == 1
    assert cv.run(args, now=NOW + 3600) and len(calls) == 1, "second launch the same day must not fetch"
    assert cv.run(args, now=NOW + DAY + 1) and len(calls) == 2, "next day refetches"


def test_sources_are_shared_and_deduplicated(cv, monkeypatch, tmp_path):
    calls = _fake_fetch(cv, monkeypatch, cv.parse_npm(packument()))
    cv.run(["--state", str(tmp_path / "s.json"), "opencode", "opencode=1.0.0", "pi"], now=NOW)
    assert len(calls) == 2


def test_failed_fetch_keeps_old_data_and_waits_a_day(cv, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    old = state_with(cv, opencode=cv.parse_npm(packument()))
    old["sources"]["npm:opencode-ai"]["checked"] = NOW - 2 * DAY
    state.write_text(json.dumps(old))
    calls = _fake_fetch(cv, monkeypatch, OSError("offline"))
    lines = cv.run(["--state", str(state), "opencode=1.19.5"], now=NOW)
    assert len(calls) == 1
    assert lines and "1.20.0" in lines[0], "old data still reported"
    saved = json.loads(state.read_text())["sources"]["npm:opencode-ai"]
    assert saved["checked"] == NOW and saved["latest"] == "1.20.0"
    cv.run(["--state", str(state), "opencode=1.19.5"], now=NOW + 60)
    assert len(calls) == 1, "a failed fetch also counts as today's attempt"


def test_malformed_response_is_silent_and_keeps_old_data(cv, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    calls = _fake_fetch(cv, monkeypatch, lambda s: cv.parse_npm({"dist-tags": {}}))
    assert cv.run(["--state", str(state), "opencode=1.19.5"], now=NOW) == []
    assert len(calls) == 1
    assert json.loads(state.read_text())["sources"]["npm:opencode-ai"] == {"checked": NOW}


def test_cached_mode_never_fetches(cv, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    stale = state_with(cv, opencode=cv.parse_npm(packument()))
    stale["sources"]["npm:opencode-ai"]["checked"] = NOW - 30 * DAY
    state.write_text(json.dumps(stale))
    before = state.read_text()
    calls = _fake_fetch(cv, monkeypatch, cv.parse_npm(packument()))
    lines = cv.run(["--cached", "--state", str(state), "opencode=1.19.5"], now=NOW)
    assert calls == []
    assert lines and "OpenCode" in lines[0]
    assert state.read_text() == before


def test_fetch_only_prints_nothing_and_writes_state(cv, monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    _fake_fetch(cv, monkeypatch, cv.parse_npm(packument()))
    assert cv.run(["--fetch-only", "--state", str(state), "opencode=1.19.5"], now=NOW) == []
    assert json.loads(state.read_text())["sources"]["npm:opencode-ai"]["latest"] == "1.20.0"


def test_deadline_bounds_a_hung_fetch(cv, monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "FETCH_DEADLINE", 0.3)
    release = threading.Event()
    monkeypatch.setattr(cv, "fetch_source", lambda s: release.wait(10))
    t0 = time.monotonic()
    try:
        assert cv.run(["--state", str(tmp_path / "s.json"), "opencode=1.19.5"], now=NOW) == []
    finally:
        release.set()
    assert time.monotonic() - t0 < 2
    saved = json.loads((tmp_path / "s.json").read_text())["sources"]["npm:opencode-ai"]
    assert saved == {"checked": NOW}


@pytest.mark.parametrize("text", ["", "{", "[]", '{"schema": 99, "sources": {}}', '{"schema": 1, "sources": []}'])
def test_corrupt_state_file_is_treated_as_empty(cv, tmp_path, text):
    state = tmp_path / "state.json"
    state.write_text(text)
    assert cv.load_state(str(state)) == {"schema": cv.STATE_SCHEMA, "sources": {}}
    assert cv.run(["--cached", "--state", str(state), "opencode=1.19.5"], now=NOW) == []


# ── over real HTTP (local server) ─────────────────────────────────────────────

class _Server:
    def __init__(self, routes, delay=0.0):
        self.routes, self.delay, self.paths = routes, delay, []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                if outer.delay:
                    time.sleep(outer.delay)
                body = outer.routes.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
                gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
                if gz:
                    raw = gzip.compress(raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if gz:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(cv, monkeypatch):
    made = []
    # The helper honours *_PROXY like any urllib client; keep the local server direct.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    def make(routes, delay=0.0):
        s = _Server(routes, delay)
        made.append(s)
        monkeypatch.setattr(cv, "NPM_REGISTRY", s.url)
        monkeypatch.setattr(cv, "DOCKER_HUB", s.url)
        return s

    yield make
    for s in made:
        s.close()


def test_end_to_end_over_http(cv, server, tmp_path):
    srv = server({
        "/opencode-ai": packument(),
        "/@earendil-works%2Fpi-coding-agent": packument(
            latest="1.5.0", times={"1.5.0": "2026-11-02T11:05:09.564Z"}),
        "/v2/repositories/searxng/searxng/tags/latest": dockerhub_tag(),
    })
    state = tmp_path / "state.json"
    out = cv.run(["--state", str(state), "--lag-days", "30",
                  "searxng=2026-11-03", "opencode=1.19.5", "pi=1.5.0"], now=NOW)
    assert len(out) == 2
    assert out[0].startswith("warning: the SearXNG pin") and "OpenCode 1.19.5" in out[1]
    assert sorted(srv.paths) == sorted([
        "/opencode-ai", "/@earendil-works%2Fpi-coding-agent",
        "/v2/repositories/searxng/searxng/tags/latest"])
    saved = json.loads(state.read_text())
    assert set(saved["sources"]) == {"npm:opencode-ai", "npm:@earendil-works/pi-coding-agent",
                                     "dockerhub:searxng/searxng:latest"}


def test_http_errors_are_silent(cv, server, tmp_path, capsys):
    server({"/opencode-ai": b"<html>not json</html>"})
    state = tmp_path / "state.json"
    assert cv.main(["--state", str(state), "opencode=1.19.5", "pi=1.0.0", "searxng=2026-01-01"]) == 0
    assert capsys.readouterr() == ("", "")
    assert all(e == {"checked": e["checked"]} for e in json.loads(state.read_text())["sources"].values())


def test_timeout_is_silent(cv, server, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cv, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(cv, "FETCH_DEADLINE", 1.0)
    server({"/opencode-ai": packument()}, delay=3.0)
    t0 = time.monotonic()
    assert cv.main(["--state", str(tmp_path / "s.json"), "opencode=1.19.5"]) == 0
    assert time.monotonic() - t0 < 2.5
    assert capsys.readouterr() == ("", "")


def test_gzip_response_is_inflated_and_parsed(cv, server, tmp_path):
    srv = server({"/opencode-ai": packument()})
    state = tmp_path / "state.json"
    lines = cv.run(["--state", str(state), "opencode=1.19.5"], now=NOW)
    assert srv.paths == ["/opencode-ai"]
    assert lines and "OpenCode 1.19.5" in lines[0]
    assert json.loads(state.read_text())["sources"]["npm:opencode-ai"]["latest"] == "1.20.0"


def test_gzip_bomb_is_refused_silently_and_keeps_old_data(cv, server, tmp_path, capsys, monkeypatch):
    # The compressed body is tiny, well under the cap; only the inflated size
    # trips it. A small cap keeps the test cheap.
    monkeypatch.setattr(cv, "MAX_RESPONSE_BYTES", 64 << 10)
    bomb = packument(latest="9.9.9", times={"9.9.9": "2026-12-30T00:00:00.000Z"})
    bomb["padding"] = "x" * (4 << 20)
    srv = server({"/opencode-ai": bomb})
    state = tmp_path / "state.json"
    old = state_with(cv, opencode=cv.parse_npm(packument()))
    old["sources"]["npm:opencode-ai"]["checked"] = NOW - 2 * DAY
    state.write_text(json.dumps(old))
    assert cv.main(["--fetch-only", "--state", str(state), "opencode=1.19.5"]) == 0
    assert srv.paths == ["/opencode-ai"]
    assert capsys.readouterr() == ("", "")
    saved = json.loads(state.read_text())["sources"]["npm:opencode-ai"]
    assert saved["latest"] == "1.20.0", "the oversized response must not replace old data"


def test_gunzip_bounded(cv):
    raw = b"y" * 1000
    assert cv.gunzip_bounded(gzip.compress(raw), 1000) == raw
    with pytest.raises(ValueError):
        cv.gunzip_bounded(gzip.compress(raw), 999)
    with pytest.raises(ValueError):
        cv.gunzip_bounded(gzip.compress(raw)[:-10], 1000)


# ── CLI contract (subprocess) ────────────────────────────────────────────────

def _cli(*args):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("args", [
    [],
    ["opencode=1.0.0"],                                   # no --state
    ["--state", "x", "--lag-days", "soon", "opencode"],
    ["--state", "x", "--lag-days", "-1", "opencode"],
    ["--state", "x", "--fetch-only", "--cached", "opencode"],
    ["--state", "x", "--bogus", "opencode"],
])
def test_cli_bad_arguments_are_silent(args):
    proc = _cli(*args)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_cli_cached_reports_and_unwritable_state_is_silent(tmp_path, cv):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(state_with(cv, opencode=cv.parse_npm(packument()))))
    proc = _cli("--cached", "--state", str(state), "opencode=1.19.5", "unknown-thing=1")
    assert proc.returncode == 0 and proc.stderr == ""
    assert proc.stdout.count("\n") == 1 and "OpenCode 1.19.5" in proc.stdout
    missing_dir = tmp_path / "nope" / "state.json"
    proc = _cli("--cached", "--state", str(missing_dir), "opencode=1.19.5")
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


# ── launcher wiring ───────────────────────────────────────────────────────────

def _function_body(text: str, name: str) -> str:
    start = text.find(f"\n{name}() {{")
    assert start != -1, f"{name}() not found"
    brace_open = text.find("{", start)
    depth = 0
    for i, ch in enumerate(text[brace_open:], start=brace_open):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1: i + 1]
    raise AssertionError(f"{name}() not terminated")


FAKE_PY = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
"""


def _fake_python(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "python3").write_text(FAKE_PY)
    (bin_dir / "python3").chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    return bin_dir, calls


def _run_bash(script, env):
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], capture_output=True,
                          text=True, timeout=30, env={"PATH": os.environ["PATH"], **env})


def _agent_harness(tmp_path, extra=""):
    text = AGENT.read_text()
    funcs = "\n".join(_function_body(text, n) for n in (
        "version_check_enabled", "version_check_components", "searxng_pinned_on",
        "start_version_check", "report_version_check", "tool_version"))
    return (f'SCRIPT_DIR="{REPO}"\nVERSION_CHECK_STATE="{tmp_path}/version-check.json"\n'
            f'TOOL_VERSIONS_FILE="{tmp_path}/tool-versions"\n'
            # tool_version only trusts a record for the current image.
            'IMAGE_ID="sha256:x"\n'
            f'LOCAL_SEARCH_ENABLED="${{LOCAL_SEARCH_ENABLED:-true}}"\n{funcs}\n{extra}')


def test_agent_searxng_pinned_on_reads_the_pin_comment(tmp_path):
    out = _run_bash(_agent_harness(tmp_path, f'searxng_pinned_on "{AGENT}"'), {})
    lines = AGENT.read_text().splitlines()
    pin = next(i for i, l in enumerate(lines) if l.startswith('SEARXNG_IMAGE="'))
    comment = "\n".join(lines[pin - 3:pin])
    assert out.stdout.strip() and f"known good on {out.stdout.strip()}" in comment


def test_agent_pinned_on_survives_a_bump(tmp_path):
    copy = tmp_path / "start-agent.sh"
    copy.write_text(AGENT.read_text())
    proc = subprocess.run([str(REPO / "scripts" / "bump-searxng.sh"), "--digest", "sha256:" + "c" * 64],
                          env={**os.environ, "START_AGENT": str(copy)}, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    out = _run_bash(_agent_harness(tmp_path, f'searxng_pinned_on "{copy}"'), {})
    assert out.stdout.strip() == time.strftime("%Y-%m-%d")


def test_agent_start_runs_a_detached_fetch(tmp_path):
    bin_dir, calls = _fake_python(tmp_path)
    proc = _run_bash(_agent_harness(tmp_path, "start_version_check; wait; sleep 0.3"),
                     {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls)})
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    args = calls.read_text().strip()
    assert args.startswith(f"{REPO}/check_versions.py --fetch-only --state {tmp_path}/version-check.json")
    assert args.split()[-3:] == ["searxng", "opencode", "pi"]


def test_agent_skips_searxng_when_search_is_disabled(tmp_path):
    bin_dir, calls = _fake_python(tmp_path)
    _run_bash(_agent_harness(tmp_path, "start_version_check; sleep 0.3; report_version_check"),
              {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls),
               "LOCAL_SEARCH_ENABLED": "false"})
    fetch, report = calls.read_text().splitlines()
    assert fetch.split()[-2:] == ["opencode", "pi"] and "searxng" not in fetch.split()
    assert report.split()[-2:] == ["opencode=", "pi="] and not any(
        w.startswith("searxng") for w in report.split())


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_agent_disable_switch(tmp_path, value):
    bin_dir, calls = _fake_python(tmp_path)
    proc = _run_bash(_agent_harness(tmp_path, "start_version_check; report_version_check; sleep 0.3"),
                     {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls),
                      "CLAUDE_AGENT_DISABLE_VERSION_CHECK": value})
    assert proc.returncode == 0 and calls.read_text() == ""


def test_agent_report_prints_from_the_cache(tmp_path, cv):
    entry = cv.parse_npm(packument())
    state = state_with(cv, opencode=entry, pi=entry,
                       searxng=cv.parse_dockerhub(dockerhub_tag(updated="2030-01-01T00:00:00Z")))
    (tmp_path / "version-check.json").write_text(json.dumps(state))
    (tmp_path / "tool-versions").write_text("image_id=sha256:x\nopencode=1.19.5\npi=1.20.0\nrecorded=2026-11-03T00:00:00Z\n")
    proc = _run_bash(_agent_harness(tmp_path, "report_version_check"), {})
    assert proc.returncode == 0 and proc.stdout == ""
    lines = proc.stderr.splitlines()
    assert len(lines) == 2, proc.stderr
    assert lines[0].startswith("warning: the SearXNG pin") and "OpenCode 1.19.5" in lines[1]
    proc = _run_bash(_agent_harness(tmp_path, "report_version_check"),
                     {"CLAUDE_AGENT_VERSION_LAG_DAYS": "100000"})
    assert proc.stderr == ""


def test_agent_report_never_fails_the_launch(tmp_path):
    # No state, no tool-versions file, a garbage lag: silent, exit 0.
    proc = _run_bash(_agent_harness(tmp_path, "report_version_check; echo ok"),
                     {"CLAUDE_AGENT_VERSION_LAG_DAYS": "soon"})
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "ok\n", "")


def test_agent_wiring_order():
    text = AGENT.read_text()
    start = text.find("\nstart_version_check\n")
    assert start != -1 and start < text.find("# ── Colima VM bring-up"), "fetch must start before VM work"
    attach = _function_body(text, "attach_existing")
    assert attach.find("report_version_check") > attach.find("wait_inference_probe")
    assert attach.find("report_version_check") < attach.find("exec docker exec")
    tail = text[text.rfind("wait_inference_probe\n"):]
    assert tail.find("report_version_check") < tail.find("exec docker run")
    assert "CLAUDE_AGENT_VERSION_LAG_DAYS" in text[:text.find("\nUSAGE\n", text.find("usage() {"))]
    assert "CLAUDE_AGENT_DISABLE_VERSION_CHECK" in text[:text.find("\nUSAGE\n", text.find("usage() {"))]


def _claude_harness(tmp_path, extra=""):
    text = CLAUDE.read_text()
    funcs = "\n".join(_function_body(text, n) for n in (
        "version_check_enabled", "sandbox_runtime_pin", "start_version_check", "report_version_check"))
    return f'SCRIPT_DIR="{REPO}"\nVERSION_CHECK_STATE="{tmp_path}/cc/version-check.json"\n{funcs}\n{extra}'


def test_claude_pin_matches_the_install_line(tmp_path):
    import re
    pin = re.search(r"npm install -g .*@anthropic-ai/sandbox-runtime@(\d+\.\d+\.\d+)", CLAUDE.read_text()).group(1)
    out = _run_bash(_claude_harness(tmp_path, f'sandbox_runtime_pin "{CLAUDE}"'), {})
    assert out.stdout.strip() == pin


def test_claude_start_and_report(tmp_path, cv):
    bin_dir, calls = _fake_python(tmp_path)
    proc = _run_bash(_claude_harness(tmp_path, "start_version_check; sleep 0.3"),
                     {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls)})
    assert proc.returncode == 0 and proc.stderr == ""
    assert (tmp_path / "cc").is_dir(), "state dir is created for the first run"
    assert calls.read_text().strip().endswith("--fetch-only --state %s/cc/version-check.json sandbox-runtime" % tmp_path)

    pin = _run_bash(_claude_harness(tmp_path, f'sandbox_runtime_pin "{CLAUDE}"'), {}).stdout.strip()
    state = state_with(cv, **{"sandbox-runtime": cv.parse_npm(packument(
        latest="9.9.9", times={pin: "2026-10-20T00:00:00Z", "9.9.9": "2027-01-01T00:00:00Z"}))})
    (tmp_path / "cc" / "version-check.json").write_text(json.dumps(state))
    proc = _run_bash(_claude_harness(tmp_path, "report_version_check"), {})
    assert proc.stdout == "" and f"sandbox-runtime {pin} is 73 days behind" in proc.stderr
    proc = _run_bash(_claude_harness(tmp_path, "report_version_check"),
                     {"CLAUDE_CONTAINER_DISABLE_VERSION_CHECK": "1"})
    assert proc.stderr == ""
    proc = _run_bash(_claude_harness(tmp_path, "report_version_check"),
                     {"CLAUDE_CONTAINER_VERSION_LAG_DAYS": "90"})
    assert proc.stderr == ""


def test_claude_wiring_order():
    text = CLAUDE.read_text()
    start = text.find("\nstart_version_check\n")
    assert start != -1 and start < text.find("container system start")
    exec_at = text.find("container exec -it")
    assert text.rfind("report_version_check", 0, exec_at) > text.find("\nstart_version_check\n")
    run_at = text.rfind("container run \\")
    assert text.rfind("report_version_check", 0, run_at) > exec_at
    # Nothing added inside the single-quoted setup script.
    setup = text[text.find("bash -c '"):text.find("\n  '\n")]
    assert "version_check" not in setup
