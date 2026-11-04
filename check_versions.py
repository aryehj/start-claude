#!/usr/bin/env python3
"""Launch-time staleness check for the version pins the launchers own.

Shared by start-agent.sh (SearXNG, OpenCode, Pi) and start-claude.sh
(sandbox-runtime), which run it on the macOS host with its python3:

    python3 check_versions.py --state FILE [--lag-days N]
                              [--fetch-only | --cached] NAME[=IN_USE]...

NAME is a key of COMPONENTS. IN_USE is a version for npm components and the
date the pin was last current (YYYY-MM-DD) for Docker Hub ones.

It prints one line per component whose newest upstream release came out more
than N days (default 30) after the one in use, naming the command that
updates it. Upstream data is cached in FILE; each source is fetched at most
once a day, whether or not the fetch worked. Modes:

- default:      fetch stale sources, then report
- --fetch-only: fetch stale sources, print nothing (the launchers run this
                detached at startup, so a slow network never delays a launch)
- --cached:     no network; report from FILE (the launchers run this just
                before attaching)

Any error, a timeout, a malformed response or a bad argument prints nothing
and exits 0: this must never break or slow a launch.

Stdlib-only, and must stay parseable by Python 3.9 (macOS system python3).
tests/test_check_versions.py pins the interface. See ADR-055.
"""
import argparse
import datetime
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zlib

NPM_REGISTRY = "https://registry.npmjs.org"
DOCKER_HUB = "https://hub.docker.com"

DEFAULT_LAG_DAYS = 30
CHECK_INTERVAL = 86400        # seconds between fetches of one source
REQUEST_TIMEOUT = 10.0        # per socket operation
FETCH_DEADLINE = 30.0         # all sources together
MAX_RESPONSE_BYTES = 64 << 20  # opencode-ai's packument is ~4 MB gzipped, ~25 MB raw

STATE_SCHEMA = 1

COMPONENTS = {
    "searxng": {
        "label": "SearXNG",
        "source": ("dockerhub", "searxng/searxng", "latest"),
        "update": "scripts/bump-searxng.sh, then tests/verify-tool-refresh.sh",
    },
    "opencode": {
        "label": "OpenCode",
        "source": ("npm", "opencode-ai"),
        "update": "start-agent.sh --refresh-tools",
    },
    "pi": {
        "label": "Pi",
        "source": ("npm", "@earendil-works/pi-coding-agent"),
        "update": "start-agent.sh --refresh-tools",
    },
    "sandbox-runtime": {
        "label": "sandbox-runtime",
        "source": ("npm", "@anthropic-ai/sandbox-runtime"),
        "update": "bump the pin in start-claude.sh, run start-claude.sh --rebuild,"
                  " then tests/verify-sandbox-runtime.sh",
    },
}

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")
_TIMESTAMP_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}):(\d{2}))?")


class _Quiet(Exception):
    """Any condition that should end the run silently."""


# ── pure helpers ─────────────────────────────────────────────────────────────

def source_key(source):
    return ":".join(source)


def parse_timestamp(value):
    """'2026-10-28T06:17:11.100Z' or '2026-10-28' -> naive UTC datetime, or None.

    Hand-rolled: Python 3.9's fromisoformat rejects the 'Z' suffix and
    fractions that are not 3 or 6 digits. Both sources report UTC.
    """
    if not isinstance(value, str):
        return None
    m = _TIMESTAMP_RE.match(value.strip())
    if not m:
        return None
    parts = [int(p) if p else 0 for p in m.groups()]
    try:
        return datetime.datetime(*parts)
    except ValueError:
        return None


def normalize_version(value):
    """Pull X.Y.Z[-pre] out of `opencode --version`-style output."""
    if not isinstance(value, str):
        return None
    m = _VERSION_RE.search(value)
    return m.group(0) if m else None


def lag_days(newer, older):
    """Whole calendar days from older to newer (datetimes)."""
    return (newer.date() - older.date()).days


def is_fresh(entry, now):
    checked = entry.get("checked") if isinstance(entry, dict) else None
    if not isinstance(checked, (int, float)) or isinstance(checked, bool):
        return False
    # A timestamp from the future (clock moved back) counts as stale.
    return 0 <= now - checked < CHECK_INTERVAL


def parse_npm(doc):
    """Packument -> {'latest': version, 'times': {release version: timestamp}}.

    Prereleases are dropped to keep the state file small (opencode-ai lists
    ~12k snapshot versions); a prerelease in use is simply not checked.
    """
    if not isinstance(doc, dict):
        raise ValueError("packument is not an object")
    tags, times = doc.get("dist-tags"), doc.get("time")
    if not isinstance(tags, dict) or not isinstance(times, dict):
        raise ValueError("packument lacks dist-tags or time")
    latest = tags.get("latest")
    if not isinstance(latest, str) or parse_timestamp(times.get(latest)) is None:
        raise ValueError("no publish time for dist-tags.latest")
    kept = {}
    for version, stamp in times.items():
        if version in ("created", "modified") or not isinstance(stamp, str):
            continue
        if "-" in version and version != latest:
            continue
        if parse_timestamp(stamp) is not None:
            kept[version] = stamp[:19]
    return {"latest": latest, "times": kept}


def parse_dockerhub(doc):
    """Tags-API entry -> {'updated': timestamp, 'digest': digest or None}."""
    if not isinstance(doc, dict):
        raise ValueError("tag is not an object")
    stamp = doc.get("last_updated") or doc.get("tag_last_pushed")
    if parse_timestamp(stamp) is None:
        raise ValueError("tag lacks last_updated")
    digest = doc.get("digest")
    return {"updated": stamp[:19], "digest": digest if isinstance(digest, str) else None}


def evaluate(name, in_use, state, lag_limit):
    """One warning line for NAME, or None."""
    comp = COMPONENTS.get(name)
    if comp is None or not in_use:
        return None
    entry = state.get("sources", {}).get(source_key(comp["source"]))
    if not isinstance(entry, dict):
        return None
    kind = comp["source"][0]
    if kind == "npm":
        version = normalize_version(in_use)
        latest = entry.get("latest")
        times = entry.get("times")
        if not version or not isinstance(latest, str) or not isinstance(times, dict):
            return None
        if version == latest:
            return None
        newer, older = parse_timestamp(times.get(latest)), parse_timestamp(times.get(version))
        if newer is None or older is None:
            return None
        days = lag_days(newer, older)
        if days <= lag_limit:
            return None
        return ("warning: {label} {version} is {days} days behind npm latest {latest}"
                " (released {date}). Update: {update}").format(
            label=comp["label"], version=version, days=days, latest=latest,
            date=newer.date().isoformat(), update=comp["update"])
    if kind == "dockerhub":
        pinned = parse_timestamp(in_use)
        newer = parse_timestamp(entry.get("updated"))
        if pinned is None or newer is None:
            return None
        days = lag_days(newer, pinned)
        if days <= lag_limit:
            return None
        return ("warning: the {label} pin (current on {pinned}) is {days} days behind"
                " Docker Hub {tag} (pushed {date}). Update: {update}").format(
            label=comp["label"], pinned=pinned.date().isoformat(), days=days,
            tag=comp["source"][2], date=newer.date().isoformat(), update=comp["update"])
    return None


# ── state file ───────────────────────────────────────────────────────────────

def load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"schema": STATE_SCHEMA, "sources": {}}
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA \
            or not isinstance(data.get("sources"), dict):
        return {"schema": STATE_SCHEMA, "sources": {}}
    return data


def save_state(path, state):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".version-check.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── network ──────────────────────────────────────────────────────────────────

def get_json(url, timeout):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "start-claude-check-versions",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_RESPONSE_BYTES + 1)
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    if encoding == "gzip":
        body = gunzip_bounded(body, MAX_RESPONSE_BYTES)
    return json.loads(body.decode("utf-8"))


def gunzip_bounded(data, limit):
    """Inflate a gzip body, refusing more than limit bytes of output.

    MAX_RESPONSE_BYTES caps the compressed read; without an output cap a small
    gzip bomb could inflate to gigabytes in the detached fetcher.
    """
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise ValueError("decompressed response too large")
    if not d.eof:
        raise ValueError("truncated gzip response")
    return out


def source_url(source):
    kind = source[0]
    if kind == "npm":
        return "{}/{}".format(NPM_REGISTRY, urllib.parse.quote(source[1], safe="@"))
    if kind == "dockerhub":
        return "{}/v2/repositories/{}/tags/{}".format(DOCKER_HUB, source[1], source[2])
    raise ValueError("unknown source kind")


def fetch_source(source):
    doc = get_json(source_url(source), REQUEST_TIMEOUT)
    if source[0] == "npm":
        return parse_npm(doc)
    return parse_dockerhub(doc)


def refresh(state, sources, now):
    """Fetch every stale source in parallel, within FETCH_DEADLINE.

    A source that fails or times out keeps its old data but is still marked
    checked, so an offline host retries the next day, not on every launch.
    Returns True if the state changed.
    """
    stale = [s for s in sources
             if not is_fresh(state["sources"].get(source_key(s)), now)]
    if not stale:
        return False
    results = {}
    lock = threading.Lock()

    def work(src):
        try:
            entry = fetch_source(src)
        except Exception:
            return
        with lock:
            results[source_key(src)] = entry

    threads = [threading.Thread(target=work, args=(s,), daemon=True) for s in stale]
    for t in threads:
        t.start()
    deadline = time.monotonic() + FETCH_DEADLINE
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    with lock:  # threads still running past the deadline are ignored
        done = dict(results)
    for src in stale:
        key = source_key(src)
        if key in done:
            entry = dict(done[key])
        else:
            old = state["sources"].get(key)
            entry = dict(old) if isinstance(old, dict) else {}
        entry["checked"] = now
        state["sources"][key] = entry
    return True


# ── CLI ──────────────────────────────────────────────────────────────────────

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Quiet(message)


def parse_args(argv):
    p = _Parser(add_help=False)
    p.add_argument("--state", required=True)
    p.add_argument("--lag-days", default=str(DEFAULT_LAG_DAYS))
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--fetch-only", action="store_true")
    mode.add_argument("--cached", action="store_true")
    p.add_argument("components", nargs="+")
    args = p.parse_args(argv)
    if not re.fullmatch(r"\d+", args.lag_days.strip()):
        raise _Quiet("bad --lag-days")
    args.lag_days = int(args.lag_days.strip())
    specs = []
    for item in args.components:
        name, _, in_use = item.partition("=")
        if name in COMPONENTS:
            specs.append((name, in_use.strip()))
    args.specs = specs
    return args


def run(argv, now=None):
    """Return the warning lines (empty on any error)."""
    args = parse_args(argv)
    now = time.time() if now is None else now
    state = load_state(args.state)
    if not args.cached:
        sources = []
        for name, _ in args.specs:
            src = COMPONENTS[name]["source"]
            if src not in sources:
                sources.append(src)
        if refresh(state, sources, now):
            try:
                save_state(args.state, state)
            except OSError:
                pass
    if args.fetch_only:
        return []
    lines = []
    for name, in_use in args.specs:
        line = evaluate(name, in_use, state, args.lag_days)
        if line:
            lines.append(line)
    return lines


def main(argv=None):
    try:
        lines = run(sys.argv[1:] if argv is None else argv)
    except Exception:
        return 0
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
