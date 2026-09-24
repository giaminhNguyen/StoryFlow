#!/usr/bin/env python3
"""StoryFlow Phase 0 bootstrap.

Fetches pinned upstream dependencies into external/ and skills/.
Reads sources.lock.json. Idempotent; fails loudly on any error.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCK_FILE = ROOT / "sources.lock.json"
STATE_FILE = "runtime/.bootstrap-revs.json"


class BootstrapError(Exception):
    pass


def _run(git, args, cwd=None):
    try:
        proc = subprocess.run(
            [git] + args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        raise BootstrapError("could not run git binary %r: %s" % (git, e))
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise BootstrapError(
            "git %s failed (exit %d): %s" % (" ".join(args), proc.returncode, msg or "no output")
        )
    return proc.stdout.strip()


def _fail(msg):
    print("ERROR: " + msg, file=sys.stderr)
    raise SystemExit(1)


def check_git(git="git"):
    if shutil.which(git) is None:
        raise BootstrapError(
            "Git not found (%r). Install Git for Windows (https://git-scm.com) and retry." % git
        )


def ensure_repo(base, dep, git="git"):
    """Clone (or update) a full repo checkout so HEAD == pinned revision."""
    name = dep["name"]
    target = base / dep["install_to"]
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(git, ["clone", dep["repo"], str(target)])
        print("[bootstrap] cloned %s" % name)
    elif not (target / ".git").exists():
        raise BootstrapError("%s (%s) exists but is not a git checkout" % (name, target))
    rev = _run(git, ["rev-parse", "HEAD"], cwd=target)
    if rev != dep["revision"]:
        print("[bootstrap] %s at %s, updating to pinned %s" % (name, rev[:12], dep["revision"][:12]))
        _run(git, ["fetch", "origin", dep["revision"]], cwd=target)
        _run(git, ["reset", "--hard", dep["revision"]], cwd=target)
        _run(git, ["clean", "-fdx"], cwd=target)
    rev = _run(git, ["rev-parse", "HEAD"], cwd=target)
    if rev != dep["revision"]:
        raise BootstrapError("%s: wanted %s, got %s" % (name, dep["revision"], rev))


def _check_files(name, target, required):
    missing = [f for f in required if not (target / f).is_file()]
    if missing:
        raise BootstrapError("%s: missing required files after install: %s" % (name, ", ".join(missing)))


def install_skills(base, skills, git="git"):
    """Mirror Skills-Import once, then copy each pinned skill subtree into skills/."""
    mirror = base / skills["mirror_to"]
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        _run(git, ["clone", skills["repo"], str(mirror)])
        print("[bootstrap] cloned skills mirror")
    result = {}
    installed = load_state(base)
    for name, spec in skills["projects"].items():
        target = base / spec["install_to"]
        files_ok = all((target / f).is_file() for f in spec["required_files"])
        if files_ok and installed.get(name) == spec["revision"]:
            result[name] = spec["revision"]
            print("[bootstrap] %s already at %s; skipping" % (name, spec["revision"][:12]))
            continue
        rev = _run(git, ["rev-parse", "HEAD"], cwd=mirror)
        if rev != spec["revision"]:
            _run(git, ["fetch", "origin", spec["revision"]], cwd=mirror)
            _run(git, ["reset", "--hard", spec["revision"]], cwd=mirror)
            _run(git, ["clean", "-fdx"], cwd=mirror)
        src = mirror / spec["src"]
        if not src.exists():
            raise BootstrapError(
                "%s: source path %r missing at revision %s" % (name, spec["src"], spec["revision"][:12])
            )
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, target)
        _check_files(name, target, spec["required_files"])
        result[name] = spec["revision"]
        print("[bootstrap] installed %s @ %s" % (name, spec["revision"][:12]))
    return result


def state_path(base):
    return base / STATE_FILE


def load_state(base):
    p = state_path(base)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(base, revs):
    p = state_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(revs, indent=2), encoding="utf-8")


def state(base, lock, git="git"):
    """Report (name, status, pinned) for every dependency. No downloads."""
    check_git(git)
    report = []
    sub = lock["subtitle_suppervip"]
    target = base / sub["install_to"]
    if not target.exists():
        report.append((sub["name"], "missing", sub["revision"][:12]))
    elif not (target / ".git").exists():
        report.append((sub["name"], "not a git checkout", sub["revision"][:12]))
    else:
        rev = _run(git, ["rev-parse", "HEAD"], cwd=target)
        ok = rev == sub["revision"]
        report.append((sub["name"], "ok" if ok else "stale (%s)" % rev[:12], sub["revision"][:12]))
    saved = load_state(base)
    for name, spec in lock["skills"]["projects"].items():
        target = base / spec["install_to"]
        if not target.exists():
            report.append((name, "missing", spec["revision"][:12]))
        else:
            files_ok = all((target / f).is_file() for f in spec["required_files"])
            rev_ok = saved.get(name) == spec["revision"]
            report.append((name, "ok" if (files_ok and rev_ok) else "stale or incomplete", spec["revision"][:12]))
    return report


def bootstrap(base, lock, git="git"):
    check_git(git)
    ensure_repo(base, lock["subtitle_suppervip"], git)
    revs = install_skills(base, lock["skills"], git)
    save_state(base, revs)
    print("[bootstrap] done.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bootstrap", description="StoryFlow dependency bootstrap (Phase 0).")
    parser.add_argument("--lock", default=str(LOCK_FILE))
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--git", default="git")
    parser.add_argument("--check", action="store_true", help="verify state only, no downloads; exit 1 if out of sync")
    args = parser.parse_args(argv)
    base = Path(args.root).resolve()
    try:
        with open(args.lock, encoding="utf-8") as fh:
            lock = json.load(fh)
    except OSError as e:
        _fail("cannot read lock file %s: %s" % (args.lock, e))
    try:
        if args.check:
            report = state(base, lock, args.git)
            all_ok = all(st == "ok" for _, st, _ in report)
            print("[check] %-22s %-26s pinned" % ("dependency", "status"))
            for name, status, pinned in report:
                print("[check] %-22s %-30s %s" % (name, status, pinned))
            if not all_ok:
                print("[check] out of sync; run bootstrap")
                return 1
            return 0
        bootstrap(base, lock, args.git)
        return 0
    except BootstrapError as e:
        _fail(str(e))


if __name__ == "__main__":
    sys.exit(main())