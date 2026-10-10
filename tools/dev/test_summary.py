#!/usr/bin/env python3
"""
test_summary.py — run pytest on a target and print only what matters: the totals line and each failure's id and
first error line. Keeps agent and CI chat context small (Token-Saving Protocol).

  python tools/dev/test_summary.py                         # whole suite
  python tools/dev/test_summary.py tests/test_signal_integrity.py
  python tools/dev/test_summary.py -k phone                # any extra pytest args pass through

Exit code is pytest's.
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAX_FAILURES_SHOWN = 20


def main(argv):
    cmd = [sys.executable, "-m", "pytest", "-q", "-rf", "--no-header", "-p", "no:warnings", "--tb=line", *argv]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    lines = (proc.stdout + proc.stderr).splitlines()
    failures = [ln[len("FAILED "):] for ln in lines if ln.startswith("FAILED ")]
    errors = [ln for ln in lines if ln.startswith("ERROR ")]
    totals = next((ln for ln in reversed(lines) if re.search(r"\b(passed|failed|error|no tests ran)\b", ln)), "")
    for item in failures[:MAX_FAILURES_SHOWN]:
        print(f"FAIL {item}")
    for item in errors[:MAX_FAILURES_SHOWN]:
        print(item)
    hidden = max(0, len(failures) - MAX_FAILURES_SHOWN) + max(0, len(errors) - MAX_FAILURES_SHOWN)
    if hidden > 0:
        print(f"... {hidden} more")
    print(totals.strip("= ") or f"pytest exited {proc.returncode} with no summary line")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
