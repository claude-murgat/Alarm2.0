#!/usr/bin/env python3
"""
Gateway modem SIM7600E-H — Alarm 2.0

Role : passerelle entre le backend (HTTP) et le modem USB (AT commands).
- Envoie les SMS en attente (poll /internal/sms/pending)
- Passe les appels en attente (poll /internal/calls/pending)
- Ecoute les SMS entrants (acquittement par reponse "1"/"OK")
- Surveille la sante des 3 noeuds backend

Prerequis : pip install pyserial requests
Demarrage : python modem_gateway.py
             python modem_gateway.py --port COM7  # override port
"""
import argparse
import logging
import threading
import time

import requests
import serial

from config import (
    ALL_NODE_URLS, GATEWAY_KEY, MODEM_AT_PORT, MODEM_BAUD_RATE,
    POLL_INTERVAL_SECONDS, HEALTH_CHECK_INTERVAL_SECONDS,
    ALERT_COOLDOWN_SECONDS, HTTP_TIMEOUT_SECONDS, ALERT_RECIPIENTS,
)
from modem_detect import detect_modem_port, send_at_command

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MODEM-GW] %(levelname)s %(message)s",
)
logger = logging.getLogger("modem_gateway")

GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}

# Lock partage pour l'acces serie au port AT
at_lock = threading.Lock()


# ── Helpers HTTP ─────────────────────────────────────────────────────────────

def _find_primary_url() -> str | None:
    """Trouve le backend primary parmi les noeuds disponibles."""
    for url in ALL_NODE_URLS:
        try:
            r = requests.get(f"{url}/health", timeout=HTTP_TIMEOUT_SECONDS)
            if r.status_code == 200 and r.json().get("role") == "primary":
                return url
        except Exception:
            continue
    return None


def _get_from_backend(path: str) -> list | None:
    """GET un endpoint sur le primary backend. Retourne None si injoignable."""
    url = _find_primary_url()
    if not url:
        logger.warning("Aucun backend primary disponible")
        return None
    try:
        r = requests.get(
            f"{url}{path}",
            headers=GATEWAY_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning(f"GET {path} → HTTP {r.status_code}")
        return None
    except Exception as e:
        logger.warning(f"GET {path} erreur : {e}")
        return None


def _post_to_backend(path: str, json_data: dict = None) -> dict | None:
    """POST un endpoint sur le primary backend."""
    url = _find_primary_url()
    if not url:
        return None
    try:
        r = requests.post(
            f"{url}{path}",
            headers=GATEWAY_HEADERS,
            json=json_data,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning(f"POST {path} → HTTP {r.status_code}")
        return None
    except Exception as e:
        logger.warning(f"POST {path} erreur : {e}")
        return None


# ── SMS Sender Thread ────────────────────────────────────────────────────────

class SmsSenderThread(threading.Thread):
    """Poll /internal/sms/pending et envoie les SMS via AT+CMGS."""

    def __init__(self, ser: serial.Serial):
        super().__init__(daemon=True, name="SmsSender")
        self.ser = ser
        self.running = True

    def run(self):
        logger.info("SmsSenderThread demarre")
        while self.running:
            try:
                pending = _get_from_backend("/internal/sms/pending")
                if pending:
                    for sms in pending:
                        success, error = self._send_sms(sms["to_number"], sms["body"])
                        if success:
                            _post_to_backend(f"/internal/sms/{sms['id']}/sent")
                            logger.info(f"SMS {sms['id']} envoye → {sms['to_number']}")
                        else:
                            _post_to_backend(f"/internal/sms/{sms['id']}/error",
                                           {"error": error})
                            logger.warning(f"SMS {sms['id']} echec : {error}")
            except Exception as e:
                logger.error(f"SmsSender erreur : {e}")
            time.sleep(POLL_INTERVAL_SECONDS)

    def _send_sms(self, to_number: str, body: str) -> tuple[bool, str]:
        """Envoie un SMS via AT commands. Retourne (succes, erreur)."""
        try:
            with at_lock:
                # Mode texte
                r = send_at_command(self.ser, "AT+CMGF=1", timeout=3)
                if "OK" not in r:
                    return False, "CMGF_FAILED"

                # Lancer la commande d'envoi
                self.ser.reset_input_buffer()
                self.ser.write(f'AT+CMGS="{to_number}"\r'.encode())
                time.sleep(2)

                # Attendre le prompt ">"
                prompt = b""
                deadline_prompt = time.time() + 5
                while time.time() < deadline_prompt:
                    if self.ser.in_waiting > 0:
                        prompt += self.ser.read(self.ser.in_waiting)
                        if b">" in prompt:
                            break
                    time.sleep(0.2)
                prompt_str = prompt.decode(errors="replace")
                if ">" not in prompt_str:
                    self.ser.write(b'\x1B')  # ESC pour annuler
                    return False, "NO_PROMPT"

                # Envoyer le corps + Ctrl+Z
                self.ser.write(body.encode() + b'\x1A')

                # Attendre la reponse (timeout 30s)
                response = b""
                deadline = time.time() + 30
                while time.time() < deadline:
                    if self.ser.in_waiting > 0:
                        response += self.ser.read(self.ser.in_waiting)
                        if b"OK" in response or b"ERROR" in response:
                            break
                    time.sleep(0.5)

                result = response.decode(errors="replace")
                if "OK" in result:
                    return True, ""
                return False, f"AT_ERROR: {result.strip()}"

        except Exception as e:
            return False, str(e)


# ── Call Sender Thread ───────────────────────────────────────────────────────

class CallSenderThread(threading.Thread):
    """Poll /internal/calls/pending et passe les appels via ATD + TTS."""

    def __init__(self, ser: serial.Serial):
        super().__init__(daemon=True, name="CallSender")
        self.ser = ser
        self.running = True

    def run(self):
        logger.info("CallSenderThread demarre")
        while self.running:
            try:
                pending = _get_from_backend("/internal/calls/pending")
                if pending:
                    for call in pending:
                        result = self._make_call(
                            call["to_number"],
                            call["tts_message"],
                        )
                        _post_to_backend(
                            f"/internal/calls/{call['id']}/result",
                            {"result": result},
                        )
                        logger.info(f"Call {call['id']} → {call['to_number']} : {result}")
            except Exception as e:
                logger.error(f"CallSender erreur : {e}")
            time.sleep(POLL_INTERVAL_SECONDS)

    def _make_call(self, to_number: str, tts_message: str) -> str:
        """Passe un appel, joue le TTS, ecoute la reponse DTMF.
        Retourne : ack_dtmf | escalate | no_answer | error"""
        try:
            with at_lock:
                # Appeler
                r = send_at_command(self.ser, f"ATD{to_number};", timeout=5)
                if "ERROR" in r:
                    return "error"

                # Attendre la reponse (polling AT+CLCC pendant 30s)
                answered = False
                deadline = time.time() + 30
                while time.time() < deadline:
                    clcc = send_at_command(self.ser, "AT+CLCC", timeout=2)
                    if ",0,0," in clcc:  # state 0 = active
                        answered = True
                        break
                    if "NO CARRIER" in clcc or "BUSY" in clcc:
                        return "no_answer"
                    time.sleep(2)

                if not answered:
                    send_at_command(self.ser, "ATH", timeout=3)
                    return "no_answer"

                # Jouer le TTS
                time.sleep(1)  # Petit delai pour stabiliser l'appel
                send_at_command(self.ser, f'AT+CTTS=2,"{tts_message}"', timeout=10)

                # Attendre DTMF (via +DTMF unsolicited, ou ecoute audio)
                # Pour l'instant : attendre 30s une reponse DTMF via URC
                dtmf_key = self._wait_for_dtmf(timeout=30)

                # Raccrocher
                send_at_command(self.ser, "ATH", timeout=3)

                if dtmf_key == "1":
                    return "ack_dtmf"
                elif dtmf_key == "2":
                    return "escalate"
                else:
                    return "no_answer"

        except Exception as e:
            logger.error(f"Call erreur : {e}")
            try:
                with at_lock:
                    send_at_command(self.ser, "ATH", timeout=3)
            except Exception:
                pass
            return "error"

    def _wait_for_dtmf(self, timeout: float = 30) -> str | None:
        """Attend une notification DTMF (+DTMF: X) sur le port serie.
        Retourne le digit ou None si timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting > 0:
                line = self.ser.readline().decode(errors="replace").strip()
                if "+DTMF:" in line:
                    # Format: +DTMF: 1
                    digit = line.split(":")[1].strip()
                    logger.info(f"DTMF recu : {digit}")
                    return digit
            time.sleep(0.1)
        return None


# ── SMS Receiver Thread ──────────────────────────────────────────────────────

class SmsReceiverThread(threading.Thread):
    """Ecoute les SMS entrants via +CMTI et acquitte si body = '1'/'OK'."""

    def __init__(self, ser: serial.Serial):
        super().__init__(daemon=True, name="SmsReceiver")
        self.ser = ser
        self.running = True

    def run(self):
        logger.info("SmsReceiverThread demarre")
        # Activer les notifications SMS
        with at_lock:
            send_at_command(self.ser, "AT+CNMI=2,1,0,0,0", timeout=3)

        while self.running:
            try:
                # Lire les lignes non-sollicitees (tryLock pour ne pas bloquer)
                if at_lock.acquire(blocking=False):
                    try:
                        if self.ser.in_waiting > 0:
                            line = self.ser.readline().decode(errors="replace").strip()
                            if "+CMTI:" in line:
                                self._handle_incoming_sms(line)
                    finally:
                        at_lock.release()
            except Exception as e:
                logger.error(f"SmsReceiver erreur : {e}")
            time.sleep(1)

    def _handle_incoming_sms(self, cmti_line: str):
        """Traite une notification +CMTI: "SM",3 → lit le SMS, acquitte si pertinent."""
        try:
            # Extraire l'index du SMS
            parts = cmti_line.split(",")
            index = int(parts[-1].strip())
            logger.info(f"SMS entrant detecte (index {index})")

            # Lire le SMS
            with at_lock:
                response = send_at_command(self.ser, f"AT+CMGR={index}", timeout=5)

            # Parser le sender et le body
            sender = ""
            body = ""
            lines = response.split("\n")
            for i, line in enumerate(lines):
                if "+CMGR:" in line:
                    # Format: +CMGR: "REC UNREAD","+33612345678","","26/04/16,18:30:00+08"
                    parts = line.split('"')
                    if len(parts) >= 4:
                        sender = parts[3]
                    # Le body est la ligne suivante
                    if i + 1 < len(lines):
                        body = lines[i + 1].strip()

            logger.info(f"SMS de {sender} : '{body}'")

            # Verifier si c'est un acquittement
            body_lower = body.lower().strip()
            if body_lower in ("1", "ok", "oui", "ack"):
                logger.info(f"Acquittement SMS recu de {sender}")
                result = _post_to_backend(
                    "/internal/alarms/active/ack-by-phone",
                    {"phone_number": sender},
                )
                if result:
                    logger.info(f"Alarme acquittee par SMS de {sender}")
                else:
                    logger.warning(f"Echec acquittement par SMS de {sender}")

            # Supprimer le SMS du modem
            with at_lock:
                send_at_command(self.ser, f"AT+CMGD={index}", timeout=3)

        except Exception as e:
            logger.error(f"Erreur traitement SMS entrant : {e}")


# ── Health Monitor Thread ────────────────────────────────────────────────────

class HealthMonitorThread(threading.Thread):
    """Surveille les 3 noeuds backend et alerte par SMS si tous sont KO."""

    def __init__(self, ser: serial.Serial):
        super().__init__(daemon=True, name="HealthMonitor")
        self.ser = ser
        self.running = True
        self.last_alert_time = 0

    def run(self):
        logger.info("HealthMonitorThread demarre")
        while self.running:
            try:
                all_down = True
                for url in ALL_NODE_URLS:
                    try:
                        r = requests.get(f"{url}/health", timeout=HTTP_TIMEOUT_SECONDS)
                        if r.status_code == 200:
                            all_down = False
                            break
                    except Exception:
                        continue

                if all_down:
                    now = time.time()
                    if now - self.last_alert_time > ALERT_COOLDOWN_SECONDS:
                        logger.critical("TOUS LES BACKENDS SONT INJOIGNABLES")
                        self._send_alert_sms()
                        self.last_alert_time = now
                else:
                    self.last_alert_time = 0

            except Exception as e:
                logger.error(f"HealthMonitor erreur : {e}")
            time.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

    def _send_alert_sms(self):
        """Envoie un SMS d'alerte aux destinataires configures."""
        if not ALERT_RECIPIENTS:
            logger.warning("Pas de destinataires d'alerte configures (ALERT_RECIPIENTS)")
            return

        message = "ALERTE ALARME MURGAT: Tous les backends sont injoignables. Intervention requise."
        for recipient in ALERT_RECIPIENTS:
            try:
                with at_lock:
                    send_at_command(self.ser, "AT+CMGF=1", timeout=3)
                    self.ser.reset_input_buffer()
                    self.ser.write(f'AT+CMGS="{recipient}"\r'.encode())
                    time.sleep(1)
                    self.ser.write(message.encode() + b'\x1A')
                    time.sleep(10)
                    # Lire la reponse
                    response = self.ser.read(self.ser.in_waiting or 1).decode(errors="replace")
                    if "OK" in response:
                        logger.info(f"Alerte SMS envoyee → {recipient}")
                    else:
                        logger.error(f"Echec alerte SMS → {recipient}")
            except Exception as e:
                logger.error(f"Erreur alerte SMS → {recipient} : {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Gateway modem SIM7600E-H — Alarm 2.0"
    )
    parser.add_argument("--port", help="Port COM (ex: COM7). Auto-detect si absent.")
    args = parser.parse_args()

    logger.info("=== Gateway Modem SIM7600E-H demarree ===")

    # Detecter le modem
    manual_port = args.port or MODEM_AT_PORT or None
    port, ser = detect_modem_port(manual_port=manual_port)

    if not ser:
        logger.critical("Modem non detecte. Abandon.")
        return 1

    logger.info(f"Modem connecte sur {port}")

    # Initialiser le modem
    with at_lock:
        send_at_command(ser, "AT+CMGF=1", timeout=3)      # Mode texte SMS
        send_at_command(ser, "AT+CNMI=2,1,0,0,0", timeout=3)  # Notifications SMS

    # Verifier la connectivite backend
    primary = _find_primary_url()
    if primary:
        logger.info(f"Backend primary : {primary}")
    else:
        logger.warning("Aucun backend primary — les SMS seront envoyes des qu'un backend sera disponible")

    # Demarrer les threads
    threads = [
        SmsSenderThread(ser),
        CallSenderThread(ser),
        SmsReceiverThread(ser),
        HealthMonitorThread(ser),
    ]

    for t in threads:
        t.start()
        logger.info(f"Thread {t.name} demarre")

    logger.info(f"Gateway operationnelle — {len(threads)} threads actifs")
    logger.info(f"Poll SMS/calls toutes les {POLL_INTERVAL_SECONDS}s")
    logger.info(f"Health check toutes les {HEALTH_CHECK_INTERVAL_SECONDS}s")

    # Boucle principale (keepalive)
    try:
        while True:
            time.sleep(60)
            # Verifier que le modem repond toujours
            with at_lock:
                r = send_at_command(ser, "AT", timeout=3)
                if "OK" not in r:
                    logger.error("Modem ne repond plus a AT — tentative de reconnexion")
    except KeyboardInterrupt:
        logger.info("Arret demande (Ctrl+C)")
    finally:
        for t in threads:
            t.running = False
        ser.close()
        logger.info("Port serie ferme, gateway arretee")

    return 0


if __name__ == "__main__":
    exit(main())
