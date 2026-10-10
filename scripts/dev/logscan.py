#!/usr/bin/env python3
"""logscan.py - summarize service logs instead of dumping them.

Reads a log file, a systemd unit (via journalctl) or stdin, keeps lines at or
above --level (and/or matching --grep), collapses repeats, and prints a short
digest: counts per level, the top repeated messages, and the last N matches.

Examples:
  python scripts/dev/logscan.py --unit motahai-scheduler --since "1 hour ago"
  python scripts/dev/logscan.py /var/log/motahai/core.log --grep capi --tail 20
  journalctl -u ameen-workforce -n 2000 | python scripts/dev/logscan.py -
"""
import argparse
import re
import subprocess
import sys
from collections import Counter

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|Traceback)\b")
NOISE_RE = re.compile(r"\d{4}-\d\d-\d\d[T ][\d:.,]+Z?|\b[0-9a-f]{8,}\b|\b\d+\b")


def level_of(line: str) -> str:
    m = LEVEL_RE.search(line)
    if not m:
        return "INFO"
    lv = m.group(1)
    return {"WARN": "WARNING", "Traceback": "ERROR"}.get(lv, lv)


def read_lines(a) -> list[str]:
    if a.unit:
        cmd = ["journalctl", "-u", a.unit, "--no-pager", "-o", "short-iso", "-n", str(a.max_lines)]
        if a.since:
            cmd += ["--since", a.since]
        return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()
    if a.source in (None, "-"):
        return sys.stdin.read().splitlines()[-a.max_lines:]
    with open(a.source, encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()[-a.max_lines:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="log file path, or - for stdin")
    ap.add_argument("--unit", help="systemd unit to read with journalctl")
    ap.add_argument("--since", help='journalctl --since, e.g. "30 min ago"')
    ap.add_argument("--level", default="WARNING", choices=LEVELS, help="minimum level (default WARNING)")
    ap.add_argument("--grep", help="case-insensitive regex the line must match")
    ap.add_argument("--tail", type=int, default=10, help="last matching lines to show (default 10)")
    ap.add_argument("--top", type=int, default=5, help="top repeated messages (default 5)")
    ap.add_argument("--max-lines", type=int, default=20000, help="lines to read at most")
    a = ap.parse_args()

    lines = read_lines(a)
    min_idx = LEVELS.index(a.level)
    pat = re.compile(a.grep, re.I) if a.grep else None
    per_level = Counter(level_of(l) for l in lines)
    hits = [l for l in lines if LEVELS.index(level_of(l)) >= min_idx and (not pat or pat.search(l))]
    shapes = Counter(NOISE_RE.sub("#", l)[:160] for l in hits)

    print(f"read {len(lines)} lines | " + " ".join(f"{k}={per_level[k]}" for k in LEVELS if per_level[k]))
    print(f"matched {len(hits)} (level>={a.level}{', grep=' + a.grep if a.grep else ''})")
    if shapes:
        print("top messages:")
        for shape, n in shapes.most_common(a.top):
            print(f"  {n:>5}x {shape}")
    if hits:
        print(f"last {min(a.tail, len(hits))}:")
        for l in hits[-a.tail:]:
            print("  " + l[:240])
    return 1 if any(level_of(l) in ("ERROR", "CRITICAL") for l in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
