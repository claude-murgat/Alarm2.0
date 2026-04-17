"""
Script de diagnostic de la suite de tests.
Lance tous les tests avec mesure individuelle des temps,
et ecrit un rapport JSON + console.

Usage:
    python tests/audit_tests.py

Prerequis: cluster Docker lance (dev ou 3 noeuds)
"""

import subprocess
import json
import sys
import time
from pathlib import Path

RESULTS_FILE = Path(__file__).parent / "audit_results.json"


def run_pytest_with_durations():
    """Lance pytest avec --durations=0 et sortie JSON via pytest-json-report si dispo, sinon parse stdout."""

    # Essai 1 : pytest-json-report (plus fiable)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pytest-json-report", "-q"],
            capture_output=True, timeout=30
        )

        json_tmp = Path(__file__).parent / "_pytest_report.json"
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest", "tests/",
                "-v",
                "--skip-failover",
                f"--json-report-file={json_tmp}",
                "--json-report-indent=2",
                "--tb=line",
                "-q",
            ],
            capture_output=True, text=True, timeout=1800,  # 30 min max
            cwd=Path(__file__).parent.parent
        )

        if json_tmp.exists():
            with open(json_tmp) as f:
                raw = json.load(f)

            report = analyze_json_report(raw)
            report["raw_stdout"] = result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout
            report["raw_stderr"] = result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr
            report["exit_code"] = result.returncode

            with open(RESULTS_FILE, "w") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)

            print_report(report)
            json_tmp.unlink(missing_ok=True)
            return

    except Exception as e:
        print(f"[audit] pytest-json-report non disponible ({e}), fallback durations")

    # Essai 2 : fallback --durations=0
    start = time.time()
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "tests/",
            "-v",
            "--skip-failover",
            "--durations=0",
            "--tb=line",
        ],
        capture_output=True, text=True, timeout=1800,
        cwd=Path(__file__).parent.parent
    )
    total_time = time.time() - start

    report = {
        "total_seconds": round(total_time, 2),
        "exit_code": result.returncode,
        "raw_stdout": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
        "raw_stderr": result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr,
        "method": "durations_fallback"
    }

    with open(RESULTS_FILE, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"TOTAL: {total_time:.1f}s | Exit code: {result.returncode}")
    print(f"Resultats ecrits dans: {RESULTS_FILE}")
    print(f"{'='*60}")
    print(result.stdout[-2000:])


def analyze_json_report(raw):
    """Analyse le rapport JSON de pytest-json-report."""

    tests = raw.get("tests", [])

    # Par fichier
    by_file = {}
    for t in tests:
        nodeid = t.get("nodeid", "")
        filepath = nodeid.split("::")[0]
        duration = t.get("duration", 0)
        outcome = t.get("outcome", "unknown")

        if filepath not in by_file:
            by_file[filepath] = {"tests": [], "total_duration": 0, "passed": 0, "failed": 0, "error": 0}

        by_file[filepath]["tests"].append({
            "name": nodeid,
            "duration": round(duration, 3),
            "outcome": outcome
        })
        by_file[filepath]["total_duration"] = round(by_file[filepath]["total_duration"] + duration, 3)
        by_file[filepath][outcome] = by_file[filepath].get(outcome, 0) + 1

    # Top 20 tests les plus lents
    all_tests = [{"name": t["nodeid"], "duration": round(t.get("duration", 0), 3), "outcome": t.get("outcome", "")} for t in tests]
    slowest = sorted(all_tests, key=lambda x: x["duration"], reverse=True)[:20]

    # Tests avec sleep potentiel (> 10s)
    sleep_suspects = [t for t in all_tests if t["duration"] > 10]

    # Resume par fichier, trie par duree
    file_summary = []
    for filepath, data in sorted(by_file.items(), key=lambda x: x[1]["total_duration"], reverse=True):
        file_summary.append({
            "file": filepath,
            "total_seconds": data["total_duration"],
            "test_count": len(data["tests"]),
            "passed": data["passed"],
            "failed": data["failed"],
            "error": data.get("error", 0),
            "avg_seconds": round(data["total_duration"] / max(len(data["tests"]), 1), 3)
        })

    summary = raw.get("summary", {})

    return {
        "method": "json_report",
        "total_seconds": round(raw.get("duration", 0), 2),
        "total_tests": len(tests),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "error": summary.get("error", 0),
        "skipped": summary.get("skipped", 0),
        "by_file": file_summary,
        "top20_slowest": slowest,
        "sleep_suspects_gt10s": sleep_suspects,
        "parallelization_potential": {
            "independent_fast_tests": len([t for t in all_tests if t["duration"] < 2]),
            "medium_tests_2_10s": len([t for t in all_tests if 2 <= t["duration"] < 10]),
            "slow_tests_gt10s": len([t for t in all_tests if t["duration"] >= 10]),
        }
    }


def print_report(report):
    """Affiche un resume console lisible."""
    print(f"\n{'='*60}")
    print(f"AUDIT TESTS — ALARM 2.0")
    print(f"{'='*60}")
    print(f"Total: {report['total_tests']} tests en {report['total_seconds']:.1f}s")
    print(f"Passed: {report['passed']} | Failed: {report['failed']} | Error: {report.get('error', 0)} | Skipped: {report.get('skipped', 0)}")

    print(f"\n--- TEMPS PAR FICHIER (trie par duree) ---")
    for f in report["by_file"]:
        status = "OK" if f["failed"] == 0 and f.get("error", 0) == 0 else "FAIL"
        print(f"  {f['total_seconds']:>7.1f}s  {f['test_count']:>3} tests  [{status}]  {f['file']}")

    print(f"\n--- TOP 10 TESTS LES PLUS LENTS ---")
    for t in report["top20_slowest"][:10]:
        print(f"  {t['duration']:>7.1f}s  [{t['outcome']}]  {t['name']}")

    pot = report["parallelization_potential"]
    print(f"\n--- POTENTIEL PARALLELISATION ---")
    print(f"  Rapides (<2s)  : {pot['independent_fast_tests']} tests")
    print(f"  Moyens (2-10s) : {pot['medium_tests_2_10s']} tests")
    print(f"  Lents (>10s)   : {pot['slow_tests_gt10s']} tests")

    if report["sleep_suspects_gt10s"]:
        print(f"\n--- SUSPECTS SLEEP (>10s) ---")
        for t in report["sleep_suspects_gt10s"]:
            print(f"  {t['duration']:>7.1f}s  {t['name']}")

    print(f"\n Resultats complets: {RESULTS_FILE}")
    print(f"{'='*60}")


if __name__ == "__main__":
    run_pytest_with_durations()
