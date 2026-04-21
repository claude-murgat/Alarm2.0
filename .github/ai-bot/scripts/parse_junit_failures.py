#!/usr/bin/env python3
"""Extract failed test IDs from pytest JUnit XML reports.

Usage :
    python parse_junit_failures.py <path> [<path> ...]

Paths can be either directories (recursively scanned for *.xml) or individual
XML files. The script prints a semicolon-separated list of stable identifiers
("classname::name") for every <testcase> that contains <failure> or <error>.

Used by .github/workflows/ai-bot.yml (phase 3D loop detection) to compare
the failing tests of two consecutive CI runs and decide whether the bot is
stuck in a loop (same tests failing 3 times in a row).

Stdlib only (no external deps) — runs on any Python 3.8+.
"""
from __future__ import annotations

import pathlib
import sys
import xml.etree.ElementTree as ET


def _failures_from_file(xml_path: pathlib.Path) -> set[str]:
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError):
        # Corrupted or unreadable XML : ignore (caller falls back to "no loop
        # detection" rather than blocking the retry).
        return set()

    failed: set[str] = set()
    for testcase in tree.iter("testcase"):
        if testcase.find("failure") is None and testcase.find("error") is None:
            continue
        classname = (testcase.get("classname") or "").strip()
        name = (testcase.get("name") or "").strip()
        if not classname and not name:
            continue
        failed.add(f"{classname}::{name}")
    return failed


def collect_failures(paths: list[str]) -> list[str]:
    xml_files: list[pathlib.Path] = []
    for raw in paths:
        p = pathlib.Path(raw)
        if p.is_dir():
            xml_files.extend(sorted(p.rglob("*.xml")))
        elif p.is_file():
            xml_files.append(p)
        # Silently skip non-existent paths : missing artifacts = "no data",
        # handled as "no detection" upstream.

    failures: set[str] = set()
    for xml in xml_files:
        failures |= _failures_from_file(xml)
    return sorted(failures)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: parse_junit_failures.py <path> [<path> ...]", file=sys.stderr)
        return 2
    failures = collect_failures(sys.argv[1:])
    print(";".join(failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
