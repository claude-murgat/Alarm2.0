#!/usr/bin/env python3
"""Compute mutation testing score from mutmut's JUnit XML output.

Usage :
    mutation_score.py <junit_xml> [--threshold N] [--summary-file PATH] [--output-file PATH]

Semantique mutmut -> junit :
  - <testcase> avec <failure>  = mutation SURVIVED (test n'a pas detecte) -> BAD
  - <testcase> sans failure    = mutation KILLED (test a detecte)         -> GOOD
  - <testcase> avec <skipped>  = mutation skipped (ignoree)               -> neutre

Score = killed / (killed + survived). Timeouts et suspicious sont comptes
comme killed (ils ont casse le test, juste de maniere inhabituelle).

Emet :
  - stdout : score, counts
  - GITHUB_STEP_SUMMARY (si --summary-file donne) : markdown lisible
  - GITHUB_OUTPUT (si --output-file donne) : score=..., killed=..., survived=...
  - ::warning:: GitHub Actions si score < threshold (pas ::error:: : G1 non-bloquant)

Exit code 0 sauf erreur infra (pas de xml, xml corrompu).

Stdlib only. No deps.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import xml.etree.ElementTree as ET


def classify(junit_path: pathlib.Path) -> tuple[int, int, int, int]:
    """Return (killed, survived, skipped, errors) counts from a mutmut junit XML."""
    try:
        tree = ET.parse(junit_path)
    except (ET.ParseError, FileNotFoundError) as exc:
        print(f"::error::Cannot parse junit XML {junit_path}: {exc}", file=sys.stderr)
        return (0, 0, 0, 0)

    killed = survived = skipped = errors = 0
    for tc in tree.iter("testcase"):
        if tc.find("failure") is not None:
            survived += 1
        elif tc.find("skipped") is not None:
            skipped += 1
        elif tc.find("error") is not None:
            # Mutmut "error" = infra issue during mutation ; ne compte ni
            # comme killed ni survived. Log-le pour diagnostic humain.
            errors += 1
        else:
            killed += 1
    return killed, survived, skipped, errors


def pct(killed: int, survived: int) -> float | None:
    denom = killed + survived
    if denom == 0:
        return None
    return 100.0 * killed / denom


def write_step_summary(path: pathlib.Path, killed: int, survived: int,
                       skipped: int, errors: int, score: float | None,
                       threshold: float) -> None:
    lines: list[str] = []
    lines.append("# 🧬 Mutation testing report\n")
    lines.append(f"- **Killed** (test caught mutation) : `{killed}`")
    lines.append(f"- **Survived** (test did NOT catch mutation) : `{survived}`")
    lines.append(f"- **Skipped** : `{skipped}`")
    lines.append(f"- **Errors (infra)** : `{errors}`\n")

    if score is None:
        lines.append("**Score** : n/a (pas de mutation evaluee)")
    else:
        verdict = "✅" if score >= threshold else "⚠️"
        lines.append(f"**Score** : `{score:.1f}%` {verdict} (seuil : {threshold}%)")

    if survived > 0:
        lines.append("")
        lines.append(f"### Action : {survived} mutations survived")
        lines.append("Chaque mutation survivante = un test faible ou manquant.")
        lines.append("Voir l'artifact `mutation-reports-<run_id>` (sous-dossier `html/`)")
        lines.append("pour la liste detaillee par fichier + le code mute.")

    try:
        # Append to existing summary if any (workflow may have multiple writers).
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as exc:
        print(f"::warning::Cannot write step summary: {exc}", file=sys.stderr)


def write_outputs(path: pathlib.Path, score: float | None, killed: int,
                  survived: int, skipped: int, errors: int) -> None:
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"score={'unknown' if score is None else f'{score:.1f}'}\n")
            f.write(f"killed={killed}\n")
            f.write(f"survived={survived}\n")
            f.write(f"skipped={skipped}\n")
            f.write(f"errors={errors}\n")
    except OSError as exc:
        print(f"::warning::Cannot write outputs: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit_xml", help="path to mutmut's junit XML report")
    parser.add_argument("--threshold", type=float, default=80.0,
                        help="score %% below which a ::warning:: is emitted (default: 80)")
    parser.add_argument("--summary-file", type=pathlib.Path, default=None,
                        help="path to GITHUB_STEP_SUMMARY to append human-readable report")
    parser.add_argument("--output-file", type=pathlib.Path, default=None,
                        help="path to GITHUB_OUTPUT to write key=value pairs")
    args = parser.parse_args()

    killed, survived, skipped, errors = classify(pathlib.Path(args.junit_xml))
    score = pct(killed, survived)

    # Log stdout
    print(f"killed={killed} survived={survived} skipped={skipped} errors={errors}")
    if score is None:
        print("score=unknown (no mutations evaluated)")
    else:
        print(f"score={score:.1f}%  threshold={args.threshold}%")

    # GitHub Actions annotations
    if score is not None and score < args.threshold:
        print(f"::warning::Mutation score {score:.1f}% < threshold {args.threshold}%. "
              f"{survived} mutations survived — tests need strengthening.")

    if args.summary_file is not None:
        write_step_summary(args.summary_file, killed, survived, skipped, errors,
                           score, args.threshold)

    if args.output_file is not None:
        write_outputs(args.output_file, score, killed, survived, skipped, errors)

    return 0


if __name__ == "__main__":
    sys.exit(main())
