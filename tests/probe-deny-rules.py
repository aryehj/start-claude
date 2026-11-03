#!/usr/bin/env python3
"""Check the push/publish deny rules against a real Claude Code CLI, with no account.

    tests/probe-deny-rules.py [--claude PATH]

tests/test_global_settings_helper.py checks the deny list with a small matcher
that follows the permissions docs. This probe asks the CLI itself. Each case
starts `claude -p` against a mock Messages API on 127.0.0.1 (ANTHROPIC_BASE_URL)
that answers with one Bash tool_use for the case's command. The tool_result
Claude Code sends back says what its permission engine decided:

- denied:   "Permission to use Bash with command ... has been denied."
- ran:      the command ran (the output of a PATH shim, below)
- approval: "This command requires approval" (no allow or deny rule matched)

Safe by construction: git, cargo, uv, docker, npm, yarn, pnpm and gh are PATH
shims that only echo their arguments, every run uses a scratch HOME and
CLAUDE_CONFIG_DIR, and no proxy is set, so nothing leaves the machine.

The settings come from merge_global_settings.py: a fresh file built from the
template, and a user file with broad allow rules and an empty deny list. The
command lists are read from the test file. Output goes to stdout and to
.verify/probe-deny-rules.log; the exit status is non-zero on any FAIL.
"""
import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "merge_global_settings.py"
TEMPLATE = REPO / "templates" / "global-claude-settings.json"
TEST_FILE = REPO / "tests" / "test_global_settings_helper.py"
LOG = REPO / ".verify" / "probe-deny-rules.log"
SHIMMED = ("git", "cargo", "uv", "docker", "npm", "yarn", "pnpm", "gh")

# Forms the matcher doesn't model: compound commands, wrappers, env prefixes,
# and the accepted false positive from the plan (a -C commit mentioning push).
EXTRA_DENIED = [
    "echo start && git push origin main",
    "git status; git push origin main",
    "FOO=1 git push origin main",
    "timeout 30 git push origin main",
    "git -C . -c k=v push",
    "git -c alias.p=push p",
    "git -C . config alias.p push",
    "git config set alias.p push",
    'git -C sub commit -m "fix push"',
]

USER_PERMISSIONS = {
    "allow": ["Bash(%s*)" % p for p in SHIMMED],
    "deny": [],
}


def _list_from_tests(name):
    tree = ast.parse(TEST_FILE.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise SystemExit("probe-deny-rules: {} not found in {}".format(name, TEST_FILE))


def _mock_handler(command, results):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, payload):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):
            self._json({})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length", 0)))
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            if not self.path.startswith("/v1/messages") or "count_tokens" in self.path:
                self._json({"input_tokens": 1})
                return
            seen = [
                block
                for message in body.get("messages", [])
                if message.get("role") == "user" and isinstance(message.get("content"), list)
                for block in message["content"]
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            results.extend(seen)
            tools = [t.get("name") for t in body.get("tools", []) if isinstance(t, dict)]
            if "Bash" in tools and not seen:
                block = {"type": "tool_use", "id": "toolu_probe", "name": "Bash", "input": {}}
                delta = {"type": "input_json_delta",
                         "partial_json": json.dumps({"command": command, "description": "probe"})}
                stop = "tool_use"
            else:
                block = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": "done"}
                stop = "end_turn"
            message = {"id": "msg_probe", "type": "message", "role": "assistant",
                       "model": body.get("model", "mock"), "content": [], "stop_reason": None,
                       "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
            if not body.get("stream"):
                done = dict(block)
                if stop == "tool_use":
                    done["input"] = json.loads(delta["partial_json"])
                else:
                    done["text"] = delta["text"]
                message.update(content=[done], stop_reason=stop)
                self._json(message)
                return
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            events = [
                {"type": "message_start", "message": message},
                {"type": "content_block_start", "index": 0, "content_block": block},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                 "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ]
            for event in events:
                self.wfile.write("event: {}\ndata: {}\n\n".format(event["type"], json.dumps(event)).encode())

    return Handler


def _verdict(results):
    if not results:
        return "no-result"
    content = results[-1].get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    content = str(content)
    if "has been denied" in content:
        return "denied"
    if "SHIM-RAN" in content:
        return "ran"
    if "requires approval" in content:
        return "approval"
    return "other: " + content[:120]


def run_case(claude, settings, command):
    work = Path(tempfile.mkdtemp(prefix="probe-deny-"))
    try:
        cfg, home, shims, repo = (work / d for d in ("cfg", "home", "shims", "repo"))
        for d in (cfg, home, shims, repo / "sub"):
            d.mkdir(parents=True)
        (cfg / "settings.json").write_text(json.dumps(settings, indent=2))
        for prog in SHIMMED:
            shim = shims / prog
            shim.write_text('#!/bin/sh\necho "SHIM-RAN: {} $*"\n'.format(prog))
            shim.chmod(0o755)
        results = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _mock_handler(command, results))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {
            "HOME": str(home),
            "PATH": "{}:/usr/local/bin:/usr/bin:/bin".format(shims),
            "TMPDIR": str(work),
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:{}".format(server.server_address[1]),
            "ANTHROPIC_API_KEY": "sk-ant-api03-probe-not-a-real-key",
            "NO_PROXY": "127.0.0.1,localhost",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CONFIG_DIR": str(cfg),
        }
        try:
            subprocess.run([claude, "-p", "run it", "--max-turns", "3"], cwd=repo, env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            pass
        finally:
            server.shutdown()
            server.server_close()
        return _verdict(results)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_settings(initial):
    work = Path(tempfile.mkdtemp(prefix="probe-deny-settings-"))
    try:
        path = work / "settings.json"
        if initial is not None:
            path.write_text(json.dumps(initial))
        subprocess.run([sys.executable, str(HELPER), str(path), str(TEMPLATE)],
                       check=True, capture_output=True)
        return json.loads(path.read_text())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude", default=shutil.which("claude"), help="CLI to test (default: claude on PATH)")
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()
    if not args.claude:
        raise SystemExit("probe-deny-rules: no claude CLI on PATH; pass --claude PATH")

    denied = _list_from_tests("DENIED_COMMANDS") + EXTRA_DENIED
    allowed = _list_from_tests("ALLOWED_COMMANDS")
    fresh = build_settings(None)
    user = build_settings({"permissions": USER_PERMISSIONS})
    cases = (
        [("fresh", fresh, c, {"denied"}) for c in denied]
        + [("fresh", fresh, c, {"ran"}) for c in allowed]
        + [("user", user, c, {"denied"}) for c in denied]
    )

    LOG.parent.mkdir(exist_ok=True)
    version = subprocess.run([args.claude, "--version"], capture_output=True, text=True).stdout.strip()
    failures = 0
    with LOG.open("w") as log, ThreadPoolExecutor(args.jobs) as pool:
        def out(line):
            print(line, flush=True)
            log.write(line + "\n")

        out("claude: {} ({})".format(args.claude, version))
        verdicts = pool.map(lambda case: run_case(args.claude, case[1], case[2]), cases)
        for (scenario, _, command, expected), verdict in zip(cases, verdicts):
            ok = verdict in expected
            failures += not ok
            out("{} [{}] {} -> {} (expected {})".format(
                "PASS" if ok else "FAIL", scenario, command, verdict, "/".join(sorted(expected))))
        out("{}: {} of {} checks passed".format(
            "PASS" if not failures else "FAIL", len(cases) - failures, len(cases)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
