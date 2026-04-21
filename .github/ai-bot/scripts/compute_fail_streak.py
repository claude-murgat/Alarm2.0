#!/usr/bin/env python3
"""Compare the failing-test sets of two consecutive CI runs and emit the
updated fail-streak counter (phase 3D loop detection).

Usage :
    python compute_fail_streak.py --current <tests> --last <tests> --prev-streak <N>

Arguments :
  --current      semicolon-separated failing tests of the latest CI run
                 (output of parse_junit_failures.py)
  --last         semicolon-separated failing tests of the previous CI run
                 (stored in PR body metadata as `last-fail-tests:`)
  --prev-streak  integer value of the previous `fail-streak:` metadata field

Rule (from docs/AI_STRATEGY.md + phase 3D spec) :
  - If current == last  OR  current is a strict (proper) subset of last
      -> streak = prev_streak + 1
  - Else
      -> streak = 1  (current failure still counts as a fresh failure)

Output (to stdout, one key=value per line, GitHub Actions-friendly) :
    fail_streak=<int>
    is_loop=<true|false>            (true when streak >= 3)
    same_or_subset=<true|false>     (for diagnostics)

Stdlib only. No deps.
"""
from __future__ import annotations

import argparse
import sys


def parse_tests(raw: str) -> set[str]:
    if not raw:
        return set()
    return {t.strip() for t in raw.split(";") if t.strip()}


def compute(current: set[str], last: set[str], prev_streak: int) -> tuple[int, bool, bool]:
    # Empty current (no failing tests parsed) : should not happen for retry-ci
    # (the event itself is "CI failed") but handle defensively -> do not increment.
    if not current:
        return (max(prev_streak, 0), False, False)

    same_or_subset = current <= last  # subset-equal : covers "identique" and "strict subset"
    if same_or_subset:
        new_streak = prev_streak + 1
    else:
        new_streak = 1
    return (new_streak, new_streak >= 3, same_or_subset)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", default="", help="failing tests of current CI run (;-separated)")
    parser.add_argument("--last", default="", help="failing tests of previous CI run (;-separated)")
    parser.add_argument("--prev-streak", type=int, default=0, help="previous fail-streak counter")
    args = parser.parse_args()

    current = parse_tests(args.current)
    last = parse_tests(args.last)
    streak, is_loop, same_or_subset = compute(current, last, args.prev_streak)

    print(f"fail_streak={streak}")
    print(f"is_loop={'true' if is_loop else 'false'}")
    print(f"same_or_subset={'true' if same_or_subset else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
