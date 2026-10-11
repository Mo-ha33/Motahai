#!/usr/bin/env python3
"""qtest.py - run a targeted pytest selection and print only a compact summary.

Keeps chat/agent context small: no full tracebacks, just the result line and
one line per failure (test id + the assertion/error message).

Examples:
  python scripts/dev/qtest.py tests/test_capi_cod.py
  python scripts/dev/qtest.py -k phone
  python scripts/dev/qtest.py tests/test_webhook_routes.py -k replay --max-fail 5
  python scripts/dev/qtest.py --changed          # tests matching files changed vs origin/main
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def changed_tests(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.split()
    out += subprocess.run(["git", "diff", "--name-only"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    tests = set()
    for f in out:
        p = Path(f)
        if p.suffix != ".py":
            continue
        if p.name.startswith("test_") and (ROOT / p).exists():
            tests.add(str(p))
            continue
        for cand in ROOT.glob(f"tests/**/test_{p.stem}*.py"):
            tests.add(str(cand.relative_to(ROOT)))
    return sorted(tests)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="test files/dirs/node ids")
    ap.add_argument("-k", help="pytest -k expression")
    ap.add_argument("--changed", action="store_true", help="select tests for files changed vs --base")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--max-fail", type=int, default=10, help="failure lines to print (default 10)")
    a = ap.parse_args()

    paths = list(a.paths)
    if a.changed:
        paths += changed_tests(a.base)
        if not paths:
            print("qtest: no matching tests for changed files")
            return 0
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line", "-rfE", *paths]
    if a.k:
        cmd += ["-k", a.k]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    lines = (proc.stdout + proc.stderr).splitlines()

    fails = [l for l in lines if l.startswith(("FAILED ", "ERROR "))]
    summary = next((l for l in reversed(lines) if re.search(r"\b(passed|failed|error|deselected|no tests ran)\b", l)), "")
    print("cmd:", " ".join(cmd[3:]))
    for l in fails[: a.max_fail]:
        print(l[:240])
    if len(fails) > a.max_fail:
        print(f"... {len(fails) - a.max_fail} more failures")
    if proc.returncode not in (0, 1, 5) and not fails:
        # collection/usage error: show the tail so the cause is visible
        print("\n".join(l[:240] for l in lines[-15:]))
    print(summary.strip("= ") or f"exit {proc.returncode}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
