#!/usr/bin/env python3
"""Smoke tests for StoryFlow bootstrap. No frameworks; run directly.

    python scripts/test_bootstrap.py

Uses a real temp root and real upstream URLs. Requires git on PATH.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOT = ROOT / "bootstrap.py"
LOCK = ROOT / "sources.lock.json"
LOCK_DATA = json.loads(LOCK.read_text(encoding="utf-8"))

FAILED = []


def run(args, expect):
    proc = subprocess.run([sys.executable, str(BOOT)] + args, capture_output=True, text=True)
    if proc.returncode != expect:
        raise AssertionError(
            "exit %d, expected %d\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (proc.returncode, expect, proc.stdout, proc.stderr)
        )
    return proc


def bash(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


def snapshot(root):
    out = {}
    for base in ("external", "skills"):
        d = root / base
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                key = str(p.relative_to(root))
                out[key] = p.stat().st_mtime_ns // 1000
    return out


def test_fresh(tmp):
    run(["--root", str(tmp), "--lock", str(LOCK)], expect=0)
    sub = tmp / "external" / "subtitle_suppervip"
    assert (sub / ".git").exists(), "subtitle checkout missing"
    head = bash(["git", "rev-parse", "HEAD"], cwd=sub)
    assert head == LOCK_DATA["subtitle_suppervip"]["revision"], "subtitle HEAD != pinned"
    for name in LOCK_DATA["skills"]["projects"]:
        d = tmp / "skills" / name
        assert (d / "SKILL.md").is_file(), "%s SKILL.md missing" % name
    assert (tmp / "runtime" / ".bootstrap-revs.json").is_file(), "state marker missing"


def test_rerun_noop(tmp):
    run(["--root", str(tmp), "--lock", str(LOCK)], expect=0)
    snap1 = snapshot(tmp)
    run(["--root", str(tmp), "--lock", str(LOCK)], expect=0)
    snap2 = snapshot(tmp)
    assert snap1 == snap2, "rerun modified dependency files"


def test_deps_present_check_ok(tmp):
    run(["--root", str(tmp), "--lock", str(LOCK)], expect=0)
    proc = run(["--root", str(tmp), "--lock", str(LOCK), "--check"], expect=0)
    assert "ok" in proc.stdout


def test_missing_git(tmp):
    proc = run(
        ["--root", str(tmp), "--lock", str(LOCK), "--git", "definitely-not-git-xyz"], expect=1
    )
    assert "Git not found" in proc.stderr


def test_bad_revision_fails(tmp):
    bad = tmp / "lock-bad.json"
    data = dict(LOCK_DATA)
    data["subtitle_suppervip"] = dict(data["subtitle_suppervip"], revision="0" * 40)
    bad.write_text(json.dumps(data), encoding="utf-8")
    proc = run(["--root", str(tmp / "bad"), "--lock", str(bad)], expect=1)
    assert "failed" in proc.stderr, "bad revision did not fail loudly"
    run(["--root", str(tmp / "bad"), "--lock", str(bad), "--check"], expect=1)


def main():
    cases = [test_fresh, test_rerun_noop, test_deps_present_check_ok, test_missing_git, test_bad_revision_fails]
    for test in cases:
        name = test.__name__
        with tempfile.TemporaryDirectory(prefix="storyflow-test-") as td:
            try:
                test(Path(td))
                print("PASS %s" % name)
            except Exception as e:
                FAILED.append(name)
                print("FAIL %s: %s" % (name, e))
    print("=" * 50)
    if FAILED:
        print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("all %d smoke tests passed" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())