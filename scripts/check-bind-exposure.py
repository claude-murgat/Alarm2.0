#!/usr/bin/env python3
"""Refuse le bring-up si etcd/PG/Patroni seraient publies sur Internet (INV-125).

Appele par `scripts/start-prod-node.sh` avant tout `docker compose up`. Codes :
  0 = bind sur, 1 = exposition detectee (bring-up a refuser), 2 = indeterminable.

Le cas 2 est volontairement bloquant (fail closed) : si on n'arrive pas a lister
les adresses de la machine, on ne peut pas affirmer que le bind est sur.

Usage :
  check-bind-exposure.py --env-file .env.prod.node3
  check-bind-exposure.py --advertise-host 10.99.0.1          # valeur explicite
  check-bind-exposure.py --advertise-host 0.0.0.0 \\
                         --address 51.210.105.102            # dry-run hors machine

Stdlib uniquement : tourne sur l'hote avec le python3 systeme, sans le venv du
backend.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.app.logic.bind_exposure import evaluate_bind_exposure  # noqa: E402


def read_advertise_host_from_env_file(path: str) -> str | None:
    """Derniere valeur de `ADVERTISE_HOST=` du fichier, ou None si absente."""
    value = None
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("ADVERTISE_HOST="):
            value = stripped.split("=", 1)[1].strip().strip("\"'")
    return value


def detect_ipv4_addresses() -> list[str]:
    """Adresses IPv4 de la machine, forme CIDR.

    IPv4 seulement : le defaut `0.0.0.0` du compose ne publie que sur IPv4, donc
    une IPv6 publique n'est pas une exposition (et ne doit pas bloquer un nœud
    onsite dont le FAI delegue un prefixe v6).
    """
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    addresses = []
    for line in out.splitlines():
        fields = line.split()
        if "inet" in fields:
            addresses.append(fields[fields.index("inet") + 1])
    return addresses


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="fichier .env d'ou lire ADVERTISE_HOST")
    parser.add_argument("--advertise-host", help="valeur explicite (prioritaire)")
    parser.add_argument("--address", action="append", default=[],
                        help="adresse de la machine (repetable) ; defaut = auto-detect")
    args = parser.parse_args(argv)

    if args.advertise_host is not None:
        advertise_host = args.advertise_host
    elif args.env_file:
        try:
            from_file = read_advertise_host_from_env_file(args.env_file)
        except OSError as exc:
            print(f"ERREUR : {args.env_file} illisible ({exc}).", file=sys.stderr)
            return 2
        # Precedence de docker compose : une variable du shell l'emporte sur
        # celle du --env-file. On resout dans le meme ordre, sinon le check
        # validerait le fichier pendant que compose binde la valeur du shell
        # (`ADVERTISE_HOST=0.0.0.0 ./start-prod-node.sh 3`).
        from_shell = os.environ.get("ADVERTISE_HOST")
        advertise_host = from_shell if from_shell is not None else from_file
    else:
        advertise_host = os.environ.get("ADVERTISE_HOST")

    addresses = args.address or detect_ipv4_addresses()
    if not addresses:
        print(
            "ERREUR : impossible de lister les adresses IPv4 de la machine "
            "(`ip -o -4 addr show` indisponible ou vide). Impossible d'affirmer "
            "que le bind etcd/PG/Patroni est sur — bring-up refuse. Passer "
            "--address <ip> pour forcer la verification.",
            file=sys.stderr,
        )
        return 2

    verdict = evaluate_bind_exposure(advertise_host, addresses)

    if verdict.safe:
        print(f"[bind-exposure] OK ({verdict.reason}) — {verdict.message}")
        return 0

    print(
        "\n".join([
            "",
            "=" * 72,
            "  REFUS DE BRING-UP — EXPOSITION RESEAU DETECTEE (INV-125)",
            "=" * 72,
            f"  Motif          : {verdict.reason}",
            f"  ADVERTISE_HOST : {verdict.advertise_host!r}",
            f"  IP publiques   : {', '.join(verdict.public_addresses)}",
            "",
            f"  {verdict.message}",
            "",
            "  Rappel : UFW n'y change rien — les regles iptables de Docker",
            "  court-circuitent UFW, un port publie reste joignable alors que",
            "  `ufw status` l'affiche ferme.",
            "=" * 72,
            "",
        ]),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
