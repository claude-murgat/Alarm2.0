#!/usr/bin/env python3
"""
Smoke test GPIO contact sec — SIM7600E-H — Alarm 2.0

Vérifie qu'une GPIO du module SIM7600 (par défaut pin 1) est accessible
en lecture via les AT commands CGDRT/CGGETV, puis monitore les transitions
pour valider que le câblage physique est propre (pas de rebond mécanique
fugitif).

⚠ INV-120 V2 (issue #112) : la gateway en prod ne fait plus de debounce
côté logiciel — elle poll toutes les ~5s et envoie l'état courant au
backend (POST /internal/alarms/report-state, level-based reconciliation).
La recommandation de debounce affichée par ce script reste utile comme
indicateur "qualité du câblage" : si on observe des rebonds <50ms, le
contact ou son montage est sale et peut générer une fausse alarme si
l'on tombe pile dessus au moment du poll. À corriger physiquement avant
déploiement (resserrer les vis, remplacer le contact, ajouter un
snubber RC, etc.).

Câblage retenu sur HAT Waveshare SIM7600X (cf config.py) :
  3V3 ─── [contact NC] ─── IO43 (= GPIO 43 module)
  - Repos NC fermé → IO43 = 3V3 → CGGETV=43 lit 1 = NORMAL_VALUE → "closed"
  - Alarme NC ouvert → IO43 floating → CGGETV=43 lit 0 → "open"

Usage :
    # depuis le dossier gateway/, sur le nœud de prod, en SSH
    python3 probe_dry_contact.py                          # auto-détection port + pin 1, 30s
    python3 probe_dry_contact.py --port /dev/sim7600_at   # port explicite
    python3 probe_dry_contact.py --pin 2 --duration 60    # autre pin, autre durée
    python3 probe_dry_contact.py --probe-all              # teste pins 1..5, pas de monitoring

⚠ Prérequis sur le nœud :
  - Stopper la gateway pour libérer le port AT :
      sudo systemctl stop alarm-gateway     (ou docker stop <gateway-container>)
  - L'utilisateur courant doit être dans le groupe 'dialout' (ou lancer en sudo).
  - pip install pyserial  (déjà dans gateway/requirements.txt)
"""
import argparse
import sys
import time

from modem_detect import detect_modem_port, send_at_command


def configure_as_input(ser, pin: int) -> tuple[bool, str]:
    """AT+CGDRT=<pin>,0 — passe la pin en entrée. Retourne (success, raw)."""
    resp = send_at_command(ser, f"AT+CGDRT={pin},0", timeout=2.0)
    return ("OK" in resp), resp.strip()


def read_pin(ser, pin: int) -> tuple[int | None, str]:
    """AT+CGGETV=<pin> — lit la valeur. Retourne (value, raw).
    Format attendu : '+CGGETV: <pin>,<0|1>'."""
    resp = send_at_command(ser, f"AT+CGGETV={pin}", timeout=1.0)
    for line in resp.split("\n"):
        line = line.strip()
        if line.startswith("+CGGETV:"):
            try:
                payload = line.split(":", 1)[1].strip()
                parts = [p.strip() for p in payload.split(",")]
                return int(parts[1]), resp
            except (ValueError, IndexError):
                pass
    return None, resp


def probe_all(ser) -> None:
    """Teste les pins 1..5 : addressable en input ? valeur lue au repos ?"""
    print()
    print("=" * 62)
    print("  Probe pins 1..5  (AT+CGDRT=<n>,0 puis AT+CGGETV=<n>)")
    print("=" * 62)
    for pin in range(1, 6):
        ok, raw = configure_as_input(ser, pin)
        if ok:
            val, _ = read_pin(ser, pin)
            val_str = "?" if val is None else str(val)
            print(f"  pin {pin} : OK  CGDRT accepté — valeur lue actuelle = {val_str}")
        else:
            short = raw.replace("\n", " | ")
            print(f"  pin {pin} : KO  CGDRT refusé → {short or 'pas de réponse'}")
    print("=" * 62)
    print("  Pins KO = réservées en interne par le firmware (STATUS, NETLIGHT, ...)")
    print("  Pins OK = candidates pour câbler le contact sec.")
    print("=" * 62)
    print()


def monitor(ser, pin: int, duration_s: float) -> int:
    """Monitor en boucle ASAP. Log chaque transition.
    Retourne le nombre de transitions observées."""
    print()
    print("=" * 62)
    print(f"  Monitoring pin {pin}  —  durée {duration_s:.0f}s  —  polling ASAP")
    print("=" * 62)
    print("  Provoque l'OUVERTURE et la FERMETURE du contact NC à la main")
    print("  plusieurs fois pour mesurer le rebond. Ctrl+C pour arrêter.")
    print()

    last_val: int | None = None
    transitions: list[tuple[int, int, int]] = []  # (t_ms, from, to)
    samples = 0
    invalid_reads = 0
    start = time.monotonic()

    try:
        while time.monotonic() - start < duration_s:
            val, _ = read_pin(ser, pin)
            samples += 1
            now = time.monotonic()
            t_ms = int((now - start) * 1000)

            if val is None:
                invalid_reads += 1
                continue

            if last_val is None:
                print(f"  [t={t_ms:>6}ms] état initial = {val}")
            elif val != last_val:
                arrow = "↑" if val > last_val else "↓"
                print(f"  [t={t_ms:>6}ms] transition {last_val} → {val}  {arrow}")
                transitions.append((t_ms, last_val, val))
            last_val = val
    except KeyboardInterrupt:
        print("\n  (interrompu)")

    elapsed = time.monotonic() - start
    rate_hz = samples / elapsed if elapsed > 0 else 0
    period_ms = 1000 / rate_hz if rate_hz > 0 else 0

    print()
    print("=" * 62)
    print(f"  Lectures totales  : {samples}  (dont invalides : {invalid_reads})")
    print(f"  Fréquence réelle  : {rate_hz:.1f} Hz  →  période ~{period_ms:.0f} ms")
    print(f"  Transitions       : {len(transitions)}")
    print("=" * 62)

    if len(transitions) >= 2:
        gaps = [transitions[i + 1][0] - transitions[i][0] for i in range(len(transitions) - 1)]
        print(f"  Écart inter-transition min/max : {min(gaps)} ms / {max(gaps)} ms")
        # Rebond probable = transitions très rapprochées (<50ms) qui s'annulent
        rebonds = [g for g in gaps if g < 50]
        if rebonds:
            print(f"  ⚠ {len(rebonds)} transition(s) à <50ms = rebond mécanique probable")
            print(f"    (max rebond observé {max(rebonds)}ms)")
            print(f"  → INV-120 V2 ne fait PAS de debounce côté gateway. Si on tombe")
            print(f"    pile sur un rebond ouvert lors d'un poll (~5s), on déclenche")
            print(f"    une fausse alarme. Corriger physiquement avant déploiement :")
            print(f"    resserrer les vis, remplacer le contact, snubber RC, etc.")
        else:
            print("  Aucun rebond <50ms observé. → Câblage clean, OK pour la prod.")
    elif len(transitions) == 0:
        print()
        print("  Aucune transition observée. Pistes :")
        print("   - Le contact n'a pas été manipulé pendant le test ?")
        print("   - Pin incorrect ? Essaie --probe-all pour lister les pins valides.")
        print("   - Câblage : le contact NC est-il bien entre GPIO et GND ?")
        print("   - Pull-up : si la pin flotte (sans pull-up interne), la valeur reste")
        print("     indéterminée. Ajouter R=10k entre GPIO et 3V3.")
    print("=" * 62)
    print()

    return len(transitions)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke test GPIO contact sec SIM7600E-H",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--port",
        default=None,
        help="Port AT (ex: /dev/sim7600_at, /dev/ttyUSB2). Auto-détection si absent.",
    )
    parser.add_argument(
        "--pin",
        type=int,
        default=1,
        help="Numéro de GPIO module SIM7600 à tester (défaut: 1).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="Durée du monitoring en secondes (défaut: 30).",
    )
    parser.add_argument(
        "--probe-all",
        action="store_true",
        help="Teste les pins 1..5 (addressabilité + valeur au repos), pas de monitoring.",
    )
    args = parser.parse_args()

    port, ser = detect_modem_port(manual_port=args.port)
    if port is None or ser is None:
        print("ERREUR : modem SIM7600 non détecté. Préciser --port /dev/...", file=sys.stderr)
        return 1

    print(f"\nModem détecté sur {port}\n")

    try:
        if args.probe_all:
            probe_all(ser)
            return 0

        # Configurer la pin en input
        ok, raw = configure_as_input(ser, args.pin)
        if not ok:
            print(f"ERREUR : AT+CGDRT={args.pin},0 refusé par le modem.", file=sys.stderr)
            print(f"  Réponse brute : {raw}", file=sys.stderr)
            print(f"  → La pin {args.pin} est probablement réservée en interne.", file=sys.stderr)
            print(f"  → Lance : python3 {sys.argv[0]} --probe-all", file=sys.stderr)
            return 2
        print(f"Pin {args.pin} configurée en entrée (AT+CGDRT={args.pin},0 → OK)")

        # Lecture initiale
        val, raw = read_pin(ser, args.pin)
        if val is None:
            print(f"ERREUR : AT+CGGETV={args.pin} pas de valeur dans la réponse.", file=sys.stderr)
            print(f"  Réponse brute : {raw}", file=sys.stderr)
            return 3
        print(f"Lecture initiale : pin {args.pin} = {val}")
        print(f"  (rappel câblage NC : 0 = contact fermé au repos, 1 = contact ouvert = alarme)")

        # Monitoring
        n_trans = monitor(ser, args.pin, args.duration)
        return 0 if n_trans > 0 or args.duration < 5 else 4

    finally:
        ser.close()


if __name__ == "__main__":
    sys.exit(main())
