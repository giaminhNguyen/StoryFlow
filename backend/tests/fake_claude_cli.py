"""Fake ``claude`` CLI used by tests (never the real one). Emulates ``-p --output-format json``.

Behaviour is chosen by env FAKE_CLAUDE_MODE; a JSON record of argv/cwd/env-keys/stdin is written to
FAKE_CLAUDE_RECORD; FAKE_CLAUDE_PIDFILE receives this pid (and a grandchild pid in ``slow`` mode).
"""

import json
import os
import subprocess
import sys
import time

CANON = {
    "version": 1, "central_conflict": "a rivalry over a secret",
    "characters": [{"id": "c1", "name": "Mira", "function": "protagonist"},
                   {"id": "c2", "name": "Oren", "function": "antagonist"}],
    "relationships": [{"from": "c1", "to": "c2", "type": "rivalry"}],
    "events": [{"id": "e1", "summary": "the secret is found"}, {"id": "e2", "summary": "it spreads", "causes": ["e1"]}],
    "leverage_points": [{"id": "l1", "description": "who finds the secret first"}],
}
STORY = "# The Other Door\n\nMira opens the other door and the rivalry with Oren changes for good. " * 3


def envelope(result, **extra):
    body = {"type": "result", "subtype": "success", "is_error": False, "result": result,
            "duration_ms": 12, "num_turns": 1, "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 10, "output_tokens": 20}}
    body.update(extra)
    return json.dumps(body)


def main():
    mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
    raw = sys.stdin.buffer.read()
    stdin = raw.decode("utf-8", errors="replace")
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "w", encoding="utf-8") as f:
            json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "cwd_entries": os.listdir("."),
                       "env_keys": sorted(os.environ), "stdin": stdin}, f)
    pidfile = os.environ.get("FAKE_CLAUDE_PIDFILE")
    is_canon = "TASK: analyse" in stdin
    out = sys.stdout

    if mode == "slow":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        if pidfile:
            with open(pidfile, "w") as f:
                f.write(f"{os.getpid()} {child.pid}")
        time.sleep(120)
        return 0
    if mode == "success":
        out.write(envelope(json.dumps(CANON) if is_canon else STORY))
    elif mode == "fenced":
        out.write(envelope("Here you go:\n```json\n" + json.dumps(CANON) + "\n```" if is_canon
                           else "```markdown\n" + STORY + "\n```"))
    elif mode == "rate_limit":
        out.write(envelope("API Error: 429 rate limit exceeded, retry after 30 seconds", is_error=True,
                           subtype="error_during_execution"))
    elif mode == "overloaded_stderr":
        sys.stderr.write("Error: 529 overloaded_error\n")
        return 1
    elif mode == "quota":
        out.write(envelope("Claude AI usage limit reached|1893456000", is_error=True))
    elif mode == "quota_credit":
        out.write(envelope("Credit balance is too low", is_error=True))
    elif mode == "auth":
        out.write(envelope("Invalid API key - Please run /login", is_error=True))
    elif mode == "auth_stderr":
        sys.stderr.write("Not logged in. Run claude login.\n")
        return 1
    elif mode == "crash":
        sys.stderr.write("boom at C:\\Users\\secret\\thing sk-abcdefghijklmnop\n")
        return 3
    elif mode == "giant":
        out.write(envelope("x" * (9 * 1024 * 1024)))
    elif mode == "invalid_json":
        out.write("this is not json {")
    elif mode == "empty":
        out.write(envelope("   "))
    elif mode == "non_utf8":
        sys.stdout.buffer.write(b'{"result": "\xff\xfe\xfa"}')
    elif mode == "bad_story":
        out.write(envelope("too short" if not is_canon else "{}"))
    elif mode == "canon_no_json":
        out.write(envelope("I cannot do that."))
    else:
        sys.stderr.write("unknown mode\n")
        return 2
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
