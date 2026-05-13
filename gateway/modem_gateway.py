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
    DRY_CONTACT_ENABLED, DRY_CONTACT_MODE, DRY_CONTACT_GPIO_PIN,
    DRY_CONTACT_ADC_CHANNEL, DRY_CONTACT_ADC_THRESHOLD_MV,
    DRY_CONTACT_NORMAL_VALUE, DRY_CONTACT_DEBOUNCE_MS, DRY_CONTACT_POLL_MS,
    DRY_CONTACT_TITLE, DRY_CONTACT_MESSAGE,
)
from modem_detect import detect_modem_port, send_at_command
from dtmf_decoder import DtmfDecoder

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

    def __init__(self, ser: serial.Serial, audio_port: str | None = None):
        super().__init__(daemon=True, name="CallSender")
        self.ser = ser
        self.audio_port = audio_port
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

                # Jouer le TTS (local au module — ne passe pas toujours sur la ligne)
                time.sleep(2)  # Stabiliser l'appel
                send_at_command(self.ser, f'AT+CTTS=2,"{tts_message}"', timeout=10)

                # Attendre DTMF via URC (+RXDTMF: X ou +DTMF: X)
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
        """Attend une touche DTMF pendant un appel.
        Strategie 1 : ecoute le port audio USB (Goertzel sur PCM 8kHz)
        Strategie 2 (fallback) : ecoute les URC +DTMF sur le port AT

        Retourne le digit ou None si timeout."""
        import numpy as np

        # Tenter la detection via le port audio (Goertzel)
        if self.audio_port:
            try:
                audio_ser = serial.Serial(
                    port=self.audio_port,
                    baudrate=115200,
                    timeout=0.1,
                )
                decoder = DtmfDecoder(sample_rate=8000)
                block_bytes = 410  # 205 samples * 2 bytes (16-bit)
                deadline = time.time() + timeout

                while time.time() < deadline:
                    raw = audio_ser.read(block_bytes)
                    if len(raw) >= block_bytes:
                        samples = np.frombuffer(raw[:block_bytes], dtype=np.int16)
                        key = decoder.detect(samples)
                        if key:
                            logger.info(f"DTMF Goertzel : {key}")
                            audio_ser.close()
                            return key
                    time.sleep(0.01)

                audio_ser.close()
                return None
            except Exception as e:
                logger.warning(f"Audio port erreur ({self.audio_port}): {e}, fallback URC")

        # Fallback : ecoute les notifications URC +DTMF sur le port AT
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting > 0:
                line = self.ser.readline().decode(errors="replace").strip()
                if "+DTMF:" in line or "+RXDTMF:" in line:
                    # Format: +DTMF: 1 ou +RXDTMF: 1
                    digit = line.split(":")[-1].strip()
                    logger.info(f"DTMF URC : {digit}")
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


# ── Dry Contact Monitor Thread (INV-120) ─────────────────────────────────────

class DryContactMonitorThread(threading.Thread):
    """INV-120 — surveille un contact sec NC câblé sur une entrée du SIM7600.

    Deux modes :
      - mode="gpio" : polling AT+CGGETV=<pin>, 0/1 lu directement.
                      Nécessite AT+CGDRT=<pin>,0 au démarrage.
      - mode="adc"  : polling AT+CADC=<channel>, lecture mV, seuil binaire.
                      Pas de setup AT préalable. Default sur HAT Waveshare
                      SIM7600X (vérifié 2026-05-13 : channel 2 = pin ADC1
                      du HAT, sature à ~1800 mV pour 3V3 appliqué).

    Logique commune : debounce N ms (lecture brute stable pendant
    debounce_ms avant validation), edge detection sur la valeur
    normalisée 0/1, front montant depuis normal_value → POST trigger.

    Edge detection (pas niveau) : tant que le contact reste ouvert, pas de
    nouveau POST. Pour redéclencher, le contact doit physiquement repasser
    fermé puis se rouvrir. C'est l'anti-spam naturel pendant la boucle
    d'escalade (le 409 backend complète si jamais un front partait quand
    même).

    Si le setup AT échoue (mode/canal/pin non supporté par le firmware),
    le thread se termine en logguant — la gateway continue sans le trigger.
    """

    def __init__(
        self,
        ser: serial.Serial,
        mode: str,
        gpio_pin: int,
        adc_channel: int,
        adc_threshold_mv: int,
        normal_value: int,
        debounce_ms: int,
        poll_ms: int,
        title: str = "",
        message: str = "",
    ):
        super().__init__(daemon=True, name="DryContactMonitor")
        self.ser = ser
        self.mode = mode
        self.gpio_pin = gpio_pin
        self.adc_channel = adc_channel
        self.adc_threshold_mv = adc_threshold_mv
        self.normal_value = normal_value
        self.debounce_ms = debounce_ms
        self.poll_interval = max(0.02, poll_ms / 1000.0)
        self.title = title
        self.message = message
        self.running = True

        # État machine debounce
        self._last_raw: int | None = None
        self._raw_changed_at: float = 0.0
        self._stable_value: int | None = None

    def _source_label(self) -> str:
        if self.mode == "adc":
            return f"ADC ch{self.adc_channel} (seuil {self.adc_threshold_mv}mV)"
        return f"GPIO pin {self.gpio_pin}"

    def run(self):
        logger.info(
            f"DryContactMonitorThread demarre (mode={self.mode}, "
            f"{self._source_label()}, debounce={self.debounce_ms}ms, "
            f"poll={self.poll_interval*1000:.0f}ms, normal_value={self.normal_value})"
        )

        # Setup spécifique au mode
        if self.mode == "gpio":
            try:
                with at_lock:
                    resp = send_at_command(self.ser, f"AT+CGDRT={self.gpio_pin},0", timeout=2.0)
                if "OK" not in resp:
                    logger.error(
                        f"DryContact GPIO : pin {self.gpio_pin} non addressable "
                        f"(AT+CGDRT refusé) — reponse : {resp.strip()}. Thread arrete."
                    )
                    return
                logger.info(f"DryContact GPIO : pin {self.gpio_pin} configuree en entree")
            except Exception as e:
                logger.error(f"DryContact GPIO : init pin {self.gpio_pin} echec : {e}. Thread arrete.")
                return
        elif self.mode == "adc":
            # Sanity check : le canal ADC répond ?
            try:
                with at_lock:
                    resp = send_at_command(self.ser, f"AT+CADC={self.adc_channel}", timeout=2.0)
                if "+CADC:" not in resp or "ERROR" in resp:
                    logger.error(
                        f"DryContact ADC : channel {self.adc_channel} ne repond pas "
                        f"(AT+CADC refusé) — reponse : {resp.strip()}. Thread arrete."
                    )
                    return
                logger.info(f"DryContact ADC : channel {self.adc_channel} OK")
            except Exception as e:
                logger.error(f"DryContact ADC : init channel {self.adc_channel} echec : {e}. Thread arrete.")
                return
        else:
            logger.error(f"DryContact : mode '{self.mode}' inconnu (attendu : gpio|adc). Thread arrete.")
            return

        while self.running:
            try:
                val = self._read_signal()
                if val is None:
                    time.sleep(self.poll_interval)
                    continue

                now = time.monotonic()

                # Initialisation : premier sample
                if self._last_raw is None:
                    self._last_raw = val
                    self._raw_changed_at = now
                    self._stable_value = val
                    logger.info(f"DryContact ({self._source_label()}) : etat initial normalise = {val}")
                    time.sleep(self.poll_interval)
                    continue

                # Tracker les changements de la lecture brute
                if val != self._last_raw:
                    self._last_raw = val
                    self._raw_changed_at = now
                    time.sleep(self.poll_interval)
                    continue

                # Lecture brute stable depuis _raw_changed_at
                stable_for_ms = (now - self._raw_changed_at) * 1000
                if val != self._stable_value and stable_for_ms >= self.debounce_ms:
                    prev_stable = self._stable_value
                    self._stable_value = val
                    logger.info(
                        f"DryContact ({self._source_label()}) : transition stable "
                        f"{prev_stable} → {val} (stable {stable_for_ms:.0f}ms ≥ debounce {self.debounce_ms}ms)"
                    )

                    # Front sortant = transition depuis normal_value → alarme
                    if prev_stable == self.normal_value and val != self.normal_value:
                        logger.warning(
                            f"DryContact ({self._source_label()}) : FRONT D'ALARME detecte → trigger backend"
                        )
                        self._fire_trigger()

                time.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"DryContactMonitor boucle erreur : {e}")
                time.sleep(1.0)

    def _read_signal(self) -> int | None:
        """Lit la valeur normalisée 0/1. Dispatch GPIO ou ADC + seuil."""
        if self.mode == "gpio":
            return self._read_gpio()
        return self._read_adc_thresholded()

    def _read_gpio(self) -> int | None:
        """AT+CGGETV=<pin> → +CGGETV: <pin>,<val>."""
        try:
            with at_lock:
                resp = send_at_command(self.ser, f"AT+CGGETV={self.gpio_pin}", timeout=1.0)
        except Exception as e:
            logger.debug(f"DryContact read_gpio erreur : {e}")
            return None
        for line in resp.split("\n"):
            line = line.strip()
            if line.startswith("+CGGETV:"):
                try:
                    return int(line.split(":", 1)[1].strip().split(",")[1].strip())
                except (ValueError, IndexError):
                    return None
        return None

    def _read_adc_thresholded(self) -> int | None:
        """AT+CADC=<channel> → +CADC: <voltage_mV>. Applique seuil → 0/1."""
        try:
            with at_lock:
                resp = send_at_command(self.ser, f"AT+CADC={self.adc_channel}", timeout=1.5)
        except Exception as e:
            logger.debug(f"DryContact read_adc erreur : {e}")
            return None
        for line in resp.split("\n"):
            line = line.strip()
            if line.startswith("+CADC:"):
                try:
                    mv = int(line.split(":", 1)[1].strip())
                    return 1 if mv >= self.adc_threshold_mv else 0
                except (ValueError, IndexError):
                    return None
        return None

    def _fire_trigger(self):
        """POST /internal/alarms/trigger. 409 traité comme non-erreur (alarme deja active)."""
        body: dict = {}
        if self.title:
            body["title"] = self.title
        if self.message:
            body["message"] = self.message

        try:
            result = _post_to_backend("/internal/alarms/trigger", json_data=body)
            if result is None:
                # _post_to_backend logue deja le HTTP code en warning.
                # Le 409 (alarme deja active, INV-001) tombe ici — c'est normal et
                # explicitement non-erreur cote gateway (cf INV-120 idempotence).
                logger.info(
                    "DryContact trigger : backend a refuse (probablement 409 alarme deja active) "
                    "ou injoignable. Pas de retry — front consume."
                )
            else:
                logger.info(
                    f"DryContact trigger : alarme #{result.get('id')} cree "
                    f"(assigned={result.get('assigned_user_id')})"
                )
        except Exception as e:
            logger.error(f"DryContact trigger erreur : {e}")


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

    # Detecter le port audio (pour DTMF Goertzel)
    audio_port = None
    from serial.tools import list_ports as lp
    for p in lp.comports():
        if p.vid and p.vid in (0x1E0E, 0x2C7C) and "audio" in (p.description or "").lower():
            audio_port = p.device
            break
    if audio_port:
        logger.info(f"Port audio detecte : {audio_port} (DTMF Goertzel)")
    else:
        logger.warning("Port audio non detecte — DTMF via URC fallback uniquement")

    # Verifier la connectivite backend
    primary = _find_primary_url()
    if primary:
        logger.info(f"Backend primary : {primary}")
    else:
        logger.warning("Aucun backend primary — les SMS seront envoyes des qu'un backend sera disponible")

    # Demarrer les threads
    threads = [
        SmsSenderThread(ser),
        CallSenderThread(ser, audio_port=audio_port),
        SmsReceiverThread(ser),
        HealthMonitorThread(ser),
    ]

    # INV-120 — Trigger par contact sec NC local (opt-in via DRY_CONTACT_ENABLED)
    if DRY_CONTACT_ENABLED:
        threads.append(
            DryContactMonitorThread(
                ser=ser,
                mode=DRY_CONTACT_MODE,
                gpio_pin=DRY_CONTACT_GPIO_PIN,
                adc_channel=DRY_CONTACT_ADC_CHANNEL,
                adc_threshold_mv=DRY_CONTACT_ADC_THRESHOLD_MV,
                normal_value=DRY_CONTACT_NORMAL_VALUE,
                debounce_ms=DRY_CONTACT_DEBOUNCE_MS,
                poll_ms=DRY_CONTACT_POLL_MS,
                title=DRY_CONTACT_TITLE,
                message=DRY_CONTACT_MESSAGE,
            )
        )
        if DRY_CONTACT_MODE == "adc":
            logger.info(
                f"Contact sec active : mode=ADC ch{DRY_CONTACT_ADC_CHANNEL}, "
                f"seuil={DRY_CONTACT_ADC_THRESHOLD_MV}mV, "
                f"debounce={DRY_CONTACT_DEBOUNCE_MS}ms, normal_value={DRY_CONTACT_NORMAL_VALUE}"
            )
        else:
            logger.info(
                f"Contact sec active : mode=GPIO pin{DRY_CONTACT_GPIO_PIN}, "
                f"debounce={DRY_CONTACT_DEBOUNCE_MS}ms, normal_value={DRY_CONTACT_NORMAL_VALUE}"
            )
    else:
        logger.info("Contact sec desactive (DRY_CONTACT_ENABLED=false)")

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
