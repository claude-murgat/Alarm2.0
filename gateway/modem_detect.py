#!/usr/bin/env python3
"""
Detection et diagnostic du modem Waveshare SIM7600E-H — Alarm 2.0

Role : detecter automatiquement le modem SIM7600E-H branche en USB,
ouvrir le port AT commands, et verifier l'etat du modem, de la SIM,
du signal et de l'operateur.

Prerequis hardware : Waveshare SIM7600E-H branche en USB
Prerequis logiciel : pip install pyserial

Usage :
    python modem_detect.py              # auto-detection
    python modem_detect.py --port COM7  # port manuel
"""
import argparse
import dataclasses
import logging
import sys
import time

import serial
from serial.tools import list_ports

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MODEM-DETECT] %(levelname)s %(message)s",
)
logger = logging.getLogger("modem_detect")

# VID connus pour les modems SIMCom / SimTech
KNOWN_VIDS = {0x1E0E, 0x2C7C}
# Mots-cles dans la description du port
KNOWN_KEYWORDS = ("simtech", "simcom", "sim7600")

BAUD_RATE = 115200
AT_RETRY_COUNT = 3
AT_RETRY_DELAY = 2.0


@dataclasses.dataclass
class ModemStatus:
    """Resultat du diagnostic modem."""
    port: str = ""
    port_description: str = ""
    modem_model: str = ""
    modem_ok: bool = False
    sim_status: str = ""
    signal_rssi: int = 99
    signal_dbm: int = -113
    signal_quality: str = "Inconnu"
    operator: str = ""
    registered: bool = False
    error: str | None = None


def _list_candidate_ports(manual_port: str | None = None) -> list[str]:
    """
    Liste les ports COM candidats pour le SIM7600.
    Si manual_port est fourni, le retourne directement.
    Sinon, filtre par VID ou description.
    """
    if manual_port:
        logger.info(f"Port manuel specifie : {manual_port}")
        return [manual_port]

    candidates = []
    all_ports = list(list_ports.comports())

    if not all_ports:
        logger.warning("Aucun port COM detecte sur le systeme")
        return []

    logger.info(f"{len(all_ports)} port(s) COM detecte(s) :")
    for p in all_ports:
        vid_str = f"VID:{p.vid:04X}" if p.vid else "VID:?"
        pid_str = f"PID:{p.pid:04X}" if p.pid else "PID:?"
        logger.info(f"  {p.device} — {p.description} ({vid_str} {pid_str})")

        # Filtrer par VID connu
        if p.vid and p.vid in KNOWN_VIDS:
            candidates.append(p.device)
            continue

        # Fallback : mots-cles dans la description
        desc_lower = (p.description or "").lower()
        manufacturer_lower = (p.manufacturer or "").lower()
        if any(kw in desc_lower or kw in manufacturer_lower for kw in KNOWN_KEYWORDS):
            candidates.append(p.device)

    # Prioriser les ports avec "AT PORT" dans le nom (port AT commands)
    candidates.sort(key=lambda p: 0 if "AT PORT" in (
        next((x.description for x in list_ports.comports() if x.device == p), "") or ""
    ).upper() else 1)

    if candidates:
        logger.info(f"{len(candidates)} port(s) SIM7600 candidat(s) : {candidates}")
    else:
        logger.warning(
            "Aucun port SIM7600 detecte par VID/PID ou description. "
            "Utilisez --port COMx pour specifier manuellement."
        )

    return candidates


def send_at_command(ser: serial.Serial, cmd: str, timeout: float = 2.0) -> str:
    """
    Envoie une commande AT et lit la reponse.
    Retourne la reponse complete (peut contenir plusieurs lignes).
    Ne leve jamais d'exception — retourne "" en cas d'erreur.
    """
    try:
        ser.reset_input_buffer()
        ser.write(f"{cmd}\r\n".encode())

        response_lines = []
        deadline = time.time() + timeout

        while time.time() < deadline:
            if ser.in_waiting > 0:
                line = ser.readline().decode(errors="replace").strip()
                if line:
                    response_lines.append(line)
                # Terminer si on recoit OK ou ERROR
                if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
                    break
            else:
                time.sleep(0.05)

        return "\n".join(response_lines)
    except Exception as e:
        logger.debug(f"Erreur lors de l'envoi de {cmd} : {e}")
        return ""


def _probe_at_port(port: str) -> serial.Serial | None:
    """
    Tente d'ouvrir le port et d'envoyer AT.
    Retourne l'objet Serial si le modem repond OK, None sinon.
    """
    try:
        ser = serial.Serial(
            port=port,
            baudrate=BAUD_RATE,
            timeout=1,
            write_timeout=2,
        )
    except serial.SerialException as e:
        logger.debug(f"Impossible d'ouvrir {port} : {e}")
        return None

    # Retry AT plusieurs fois (le modem peut mettre 10-15s a demarrer)
    for attempt in range(1, AT_RETRY_COUNT + 1):
        response = send_at_command(ser, "AT", timeout=2.0)
        if "OK" in response:
            logger.info(f"Modem repond sur {port} (tentative {attempt})")
            return ser
        if attempt < AT_RETRY_COUNT:
            logger.debug(f"AT sans reponse sur {port}, retry {attempt}/{AT_RETRY_COUNT}...")
            time.sleep(AT_RETRY_DELAY)

    logger.debug(f"Modem ne repond pas sur {port} apres {AT_RETRY_COUNT} tentatives")
    ser.close()
    return None


def detect_modem_port(manual_port: str | None = None) -> tuple[str | None, serial.Serial | None]:
    """
    Detecte le port COM du modem SIM7600 et retourne (port, serial_connection).
    Retourne (None, None) si aucun modem trouve.
    """
    candidates = _list_candidate_ports(manual_port)

    for port in candidates:
        ser = _probe_at_port(port)
        if ser is not None:
            return port, ser

    return None, None


def _parse_cpin(response: str) -> str:
    """Parse la reponse AT+CPIN? → statut SIM."""
    for line in response.split("\n"):
        if "+CPIN:" in line:
            status = line.split("+CPIN:")[1].strip()
            return status
        if "+CME ERROR: 10" in line:
            return "SIM absente"
        if "+CME ERROR:" in line:
            return f"Erreur SIM ({line.strip()})"
    return "Inconnu"


def _parse_csq(response: str) -> tuple[int, int, str]:
    """Parse AT+CSQ → (rssi, dbm, qualite_texte)."""
    for line in response.split("\n"):
        if "+CSQ:" in line:
            try:
                parts = line.split("+CSQ:")[1].strip().split(",")
                rssi = int(parts[0])
                if rssi == 99:
                    return 99, -113, "Inconnu"
                dbm = -113 + 2 * rssi
                if rssi >= 20:
                    quality = "Excellent"
                elif rssi >= 15:
                    quality = "Bon"
                elif rssi >= 10:
                    quality = "OK"
                else:
                    quality = "Faible"
                return rssi, dbm, quality
            except (ValueError, IndexError):
                pass
    return 99, -113, "Inconnu"


def _parse_cops(response: str) -> str:
    """Parse AT+COPS? → nom operateur."""
    for line in response.split("\n"):
        if "+COPS:" in line:
            # Format : +COPS: 0,0,"Free",7
            try:
                parts = line.split('"')
                if len(parts) >= 2:
                    return parts[1]
            except (ValueError, IndexError):
                pass
    return "Inconnu"


def _parse_creg(response: str) -> bool:
    """Parse AT+CREG? → True si enregistre sur le reseau."""
    for line in response.split("\n"):
        if "+CREG:" in line:
            try:
                parts = line.split("+CREG:")[1].strip().split(",")
                stat = int(parts[1]) if len(parts) >= 2 else int(parts[0])
                # 1 = enregistre, 5 = enregistre en roaming
                return stat in (1, 5)
            except (ValueError, IndexError):
                pass
    return False


def _parse_ati(response: str) -> str:
    """Parse ATI → modele du modem."""
    lines = [l for l in response.split("\n") if l and l not in ("OK", "ATI")]
    return " / ".join(lines) if lines else "Inconnu"


def diagnose_modem(port: str, ser: serial.Serial) -> ModemStatus:
    """
    Execute la sequence de diagnostic AT complete.
    Retourne un ModemStatus avec tous les champs remplis.
    """
    status = ModemStatus(port=port)

    # Recuperer la description du port
    for p in list_ports.comports():
        if p.device == port:
            vid_str = f"VID:{p.vid:04X}" if p.vid else ""
            pid_str = f"PID:{p.pid:04X}" if p.pid else ""
            status.port_description = f"{p.description} ({vid_str} {pid_str})".strip()
            break

    # 1. AT — test de base
    response = send_at_command(ser, "AT")
    status.modem_ok = "OK" in response
    if not status.modem_ok:
        status.error = "Le modem ne repond pas a la commande AT"
        return status

    # 2. ATI — identification
    response = send_at_command(ser, "ATI", timeout=3.0)
    status.modem_model = _parse_ati(response)

    # 3. AT+CPIN? — statut SIM
    response = send_at_command(ser, "AT+CPIN?", timeout=5.0)
    status.sim_status = _parse_cpin(response)

    # 4. AT+CSQ — qualite signal
    response = send_at_command(ser, "AT+CSQ")
    status.signal_rssi, status.signal_dbm, status.signal_quality = _parse_csq(response)

    # 5. AT+COPS? — operateur
    response = send_at_command(ser, "AT+COPS?", timeout=5.0)
    status.operator = _parse_cops(response)

    # 6. AT+CREG? — enregistrement reseau
    response = send_at_command(ser, "AT+CREG?")
    status.registered = _parse_creg(response)

    return status


def print_report(status: ModemStatus) -> None:
    """Affiche le rapport de diagnostic."""
    print()
    print("=" * 50)
    print("  SIM7600E-H — Diagnostic modem")
    print("=" * 50)

    if not status.modem_ok:
        print(f"  ERREUR : {status.error or 'Modem non repondant'}")
        print("=" * 50)
        return

    print(f"  Port       : {status.port} ({status.port_description})")
    print(f"  Modem      : {status.modem_model}")
    print(f"  SIM        : {status.sim_status}")

    if status.signal_rssi != 99:
        print(f"  Signal     : {status.signal_rssi}/31 ({status.signal_dbm} dBm) — {status.signal_quality}")
    else:
        print(f"  Signal     : Pas de signal")

    print(f"  Operateur  : {status.operator}")
    print(f"  Reseau     : {'Enregistre' if status.registered else 'Non enregistre'}")

    print("=" * 50)
    if status.sim_status == "READY" and status.registered:
        print("  >>> Modem operationnel <<<")
    elif status.sim_status != "READY":
        print(f"  >>> Probleme SIM : {status.sim_status} <<<")
    else:
        print("  >>> Modem detecte mais pas enregistre sur le reseau <<<")
    print("=" * 50)
    print()


def main() -> int:
    """
    Point d'entree CLI.
    Retourne : 0=OK, 1=modem absent, 2=probleme SIM.
    """
    parser = argparse.ArgumentParser(
        description="Detection et diagnostic du modem Waveshare SIM7600E-H"
    )
    parser.add_argument(
        "--port",
        help="Port COM a utiliser (ex: COM7). Si absent, auto-detection.",
        default=None,
    )
    args = parser.parse_args()

    # Detection
    port, ser = detect_modem_port(manual_port=args.port)

    if port is None or ser is None:
        print()
        print("=" * 50)
        print("  ERREUR : Modem SIM7600E-H non detecte")
        print("=" * 50)
        print()
        print("  Verifiez que :")
        print("  1. La carte SIM7600E-H est branchee en USB")
        print("  2. Les drivers sont installes (SimTech)")
        print("  3. Aucun autre programme n'utilise le port COM")
        print()

        # Lister les ports disponibles pour aider
        all_ports = list(list_ports.comports())
        if all_ports:
            print("  Ports COM disponibles :")
            for p in all_ports:
                print(f"    {p.device} — {p.description}")
            print()
            print("  Essayez : python modem_detect.py --port COMx")
        else:
            print("  Aucun port COM detecte sur le systeme.")
        print()
        return 1

    try:
        # Diagnostic
        status = diagnose_modem(port, ser)
        print_report(status)

        if status.sim_status == "READY" and status.registered:
            return 0
        elif status.sim_status != "READY":
            return 2
        else:
            return 0  # modem ok, juste pas encore enregistre
    finally:
        ser.close()
        logger.info(f"Port {port} ferme")


if __name__ == "__main__":
    sys.exit(main())
