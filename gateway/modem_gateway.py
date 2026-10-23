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
import glob
import logging
import os
import threading
import time
import wave

import requests
import serial

from config import (
    ALL_NODE_URLS, GATEWAY_ID, GATEWAY_KEY, GATEWAY_SITE, MODEM_AT_PORT, MODEM_BAUD_RATE,
    POLL_INTERVAL_SECONDS, HEALTH_CHECK_INTERVAL_SECONDS,
    ALERT_COOLDOWN_SECONDS, HTTP_TIMEOUT_SECONDS, ALERT_RECIPIENTS,
    DRY_CONTACT_ENABLED, DRY_CONTACT_GPIO_PIN,
    DRY_CONTACT_NORMAL_VALUE, DRY_CONTACT_POLL_SECONDS,
    DRY_CONTACT_SOURCE, DRY_CONTACT_HOST_PORT, DRY_CONTACT_HOST_BAUD,
    DRY_CONTACT_LIVENESS_SECONDS,
)
from modem_detect import detect_modem_port, send_at_command
from locks import at_lock  # RLock reentrant partage du port AT (cf locks.py)
from dry_contact import parse_dc_line, raw_to_state, decide_report  # logique pure (cf dry_contact.py)
from modem_health import (  # logique pure (cf modem_health.py)
    MAX_AT_FAILURES, at_failures_after, call_was_dialed, parse_network, reregister_due,
    sends_allowed,
)
from voice_call import (  # logique pure (cf voice_call.py)
    DTMF_WAIT_S, FRAME_S, HANG_UP_COMMANDS, LEAD_SILENCE_S, call_answered, call_deferred_for_sms, call_rang,
    call_ended, call_result, confirmation_file, cpcmfrm_for, dtmf_key, is_audio_port,
    lead_silence, pcm_frames,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MODEM-GW] %(levelname)s %(message)s",
)
logger = logging.getLogger("modem_gateway")

GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}

# INV-145 : pose par le keepalive (modem joignable + inscrit) ; les threads SMS/appel
# n'essaient rien tant qu'il n'est pas pose, pour ne pas bruler les 3 essais (INV-064).
modem_ready = threading.Event()


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


# INV-156 : ne recevoir que les SMS/appels de son site (vide -> default_site cote backend)
SITE_QUERY = f"?site={GATEWAY_SITE}" if GATEWAY_SITE else ""
# Contact sec et modem appartiennent a un seul site : le 1er d'une liste (cluster de test)
REPORT_SITE = GATEWAY_SITE.split(",")[0].strip()


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
                pending = _get_from_backend("/internal/sms/pending" + SITE_QUERY)
                if pending and not modem_ready.is_set():
                    logger.warning(f"{len(pending)} SMS en attente : modem hors reseau, pas d'essai (INV-145)")
                elif pending:
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

VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice")
VOICE_FILES = ("alarme.wav", "acquittee.wav", "escaladee.wav")


def _load_voice(voice_dir: str = VOICE_DIR) -> dict | None:
    """INV-132 : charge les WAV du message et des confirmations (PCM 16 bits mono,
    meme frequence 8 ou 16 kHz). None si un fichier manque ou est invalide ->
    repli sur la synthese du module."""
    try:
        voice, rates = {}, set()
        for name in VOICE_FILES:
            with wave.open(os.path.join(voice_dir, name)) as w:
                if (w.getsampwidth(), w.getnchannels()) != (2, 1):
                    raise ValueError(f"{name} : PCM 16 bits mono attendu")
                rates.add(w.getframerate())
                voice[name] = w.readframes(w.getnframes())
        if len(rates) != 1:
            raise ValueError(f"frequences differentes : {sorted(rates)}")
        rate = rates.pop()
        cpcmfrm_for(rate)  # ValueError si ni 8 ni 16 kHz
        voice["rate"] = rate
        return voice
    except Exception as e:
        logger.warning(f"Voix preenregistree indisponible ({voice_dir}) : {e} — repli TTS du module")
        return None


class CallSenderThread(threading.Thread):
    """Poll /internal/calls/pending et passe les appels : ATD, voix francaise
    injectee par le port audio USB (repli TTS module), touche 1/2 confirmee."""

    def __init__(self, ser: serial.Serial, audio_port: str | None = None):
        super().__init__(daemon=True, name="CallSender")
        self.ser = ser
        self.audio_port = audio_port
        self.voice = _load_voice()
        self.running = True
        self._urc = ""
        self._voice_started = False

    def run(self):
        logger.info("CallSenderThread demarre")
        while self.running:
            try:
                pending = _get_from_backend("/internal/calls/pending" + SITE_QUERY)
                if pending and not modem_ready.is_set():
                    logger.warning(f"{len(pending)} appel(s) en attente : modem hors reseau, pas d'essai (INV-145)")
                elif pending:
                    # INV-134 : le SMS de detail part avant l'appel vers le meme numero.
                    sms_pending = _get_from_backend("/internal/sms/pending" + SITE_QUERY)
                    for call in pending:
                        if call_deferred_for_sms(call["to_number"], sms_pending):
                            logger.info(f"Call {call['id']} differe : SMS en attente pour {call['to_number']}")
                            continue
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
        """Passe un appel, joue le message, ecoute la touche, confirme, raccroche.
        Retourne : ack_dtmf | escalate | no_answer | error"""
        try:
            with at_lock:
                r = send_at_command(self.ser, f"ATD{to_number};", timeout=5)
                # Reponse vide = port serie mort : ne pas maquiller en no_answer.
                if not call_was_dialed(r):
                    return "error"
                # Pendant l'appel, plus de send_at_command : son reset_input_buffer
                # effacerait les URC (VOICE CALL: BEGIN, +RXDTMF).
                self._urc = ""
                if not self._wait_answered(timeout=30):
                    rang = call_rang(self._urc)
                    self._hang_up()
                    if not rang:  # INV-148 : perdu avant la sonnerie -> error, rejoue
                        logger.warning(f"Appel {to_number} jamais arrive en sonnerie -> error")
                        return "error"
                    return "no_answer"
                logger.info(f"Appel decroche ({to_number})")
                key = None
                if self.voice and self.audio_port:
                    try:
                        key = self._play_voice()
                    except Exception as e:
                        logger.warning(f"Voix PCM en echec ({e})")
                        self._poll()
                        key = dtmf_key(self._urc)
                        # Repli seulement si le message n'a pas pu demarrer.
                        if not self._voice_started and not call_ended(self._urc) and not key:
                            logger.warning("Repli TTS du module")
                            key = self._play_tts(tts_message)
                else:
                    key = self._play_tts(tts_message)
                self._hang_up()
                logger.info(f"Touche recue : {key}")
                return call_result(key)

        except Exception as e:
            logger.error(f"Call erreur : {e}")
            try:
                with at_lock:
                    self._hang_up()
            except Exception:
                pass
            return "error"

    # ── E/S port AT pendant l'appel (URC conservees dans self._urc) ──

    def _poll(self):
        n = self.ser.in_waiting
        if n:
            self._urc += self.ser.read(n).decode(errors="replace").replace("\r", "")

    def _at(self, cmd: str, timeout: float = 3.0) -> str:
        """Commande AT sans vider le tampon ; rend la main des OK/ERROR."""
        start = len(self._urc)
        self.ser.write(f"{cmd}\r\n".encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._poll()
            lines = [l.strip() for l in self._urc[start:].splitlines()]
            if "OK" in lines or "ERROR" in lines or any(l.startswith("+CME ERROR") for l in lines):
                break
            time.sleep(0.01)
        return self._urc[start:]

    def _wait_answered(self, timeout: float) -> bool:
        """INV-131 : surveillance continue (URC + AT+CLCC chaque seconde)."""
        deadline, next_clcc = time.time() + timeout, 0.0
        while time.time() < deadline:
            if time.time() >= next_clcc:
                self.ser.write(b"AT+CLCC\r\n")
                next_clcc = time.time() + 1.0
            self._poll()
            if call_answered(self._urc):
                return True
            if call_ended(self._urc):
                return False
            time.sleep(0.02)
        return False

    def _hang_up(self):
        """INV-133 : AT+CHUP (raccrochage appel vocal SIMCom), ATH en secours."""
        for cmd in HANG_UP_COMMANDS:
            if "OK" in self._at(cmd).splitlines():
                return

    # ── Message + touche ──

    def _play_voice(self) -> str | None:
        """INV-132/133 : voix preenregistree injectee par le port audio USB."""
        rate = self.voice["rate"]
        self._voice_started = False
        # Ordre de l'exemple SIMCom (usb_audio_test.py) : port audio ouvert AVANT
        # AT+CPCMREG=1 et ferme APRES AT+CPCMREG=0. Dans l'ordre inverse, l'appel
        # suivant partait dans le vide (constate le 2026-10-15 sur alarm-1).
        with serial.Serial(self.audio_port, 115200, timeout=0, write_timeout=2) as audio:
            prev = self._at("AT+CPCMREG?")
            if "+CPCMREG: 1" in prev:
                logger.warning(f"Port audio reste ouvert, fermeture : {self._at('AT+CPCMREG=0').split()}")
            frm = self._at(f"AT+CPCMFRM={cpcmfrm_for(rate)}")
            reg = self._at("AT+CPCMREG=1")
            logger.info(f"Port audio : CPCMFRM {frm.split()} CPCMREG {reg.split()}")
            if "OK" not in reg.splitlines():
                raise RuntimeError("AT+CPCMREG=1 refuse")
            try:
                audio.reset_input_buffer()
                self._voice_started = True
                state = self._stream(audio, lead_silence(rate) + self.voice["alarme.wav"], rate)
                logger.info(f"Message : {state}")
                if state == "fin":
                    state = self._stream(audio, bytes(int(rate * DTMF_WAIT_S) * 2), rate)
                    logger.info(f"Attente touche : {state}")
                key = dtmf_key(self._urc)
                conf = confirmation_file(key)
                if conf and state != "raccroche":
                    logger.info(f"Confirmation : {self._stream(audio, self.voice[conf], rate, stop_on_key=False)}")
                return key
            finally:
                logger.info(f"Fermeture port audio : {self._at('AT+CPCMREG=0').split()}")

    def _stream(self, audio: serial.Serial, pcm: bytes, rate: int, stop_on_key: bool = True) -> str:
        """Injecte le PCM au rythme reel (20 ms). Retourne fin | touche | raccroche."""
        t0 = time.time()
        for n, frame in enumerate(pcm_frames(pcm, rate), 1):
            try:
                audio.write(frame)
            except serial.SerialTimeoutException:
                # Le modem cesse de lire le son des que l'appele raccroche, avant NO CARRIER.
                deadline = time.time() + 3
                while time.time() < deadline and not call_ended(self._urc):
                    self._poll()
                    time.sleep(0.05)
                if call_ended(self._urc):
                    return "raccroche"
                raise
            audio.read(8192)  # flux descendant non utilise : on le vide
            self._poll()
            if call_ended(self._urc):
                return "raccroche"
            if stop_on_key and dtmf_key(self._urc):
                return "touche"
            delay = t0 + n * FRAME_S - time.time()
            if delay > 0:
                time.sleep(delay)
        return "fin"

    def _play_tts(self, tts_message: str) -> str | None:
        """Repli INV-132 : synthese du module, envoyee a l'appele (AT+CDTAM=1)."""
        time.sleep(LEAD_SILENCE_S)
        self._at("AT+CDTAM=1")
        self._at(f'AT+CTTS=2,"{tts_message}"', timeout=10)
        # Fin de lecture signalee par +CTTS: 0, puis DTMF_WAIT_S pour la touche.
        deadline, spoken = time.time() + 20 + DTMF_WAIT_S, False
        while time.time() < deadline:
            self._poll()
            if dtmf_key(self._urc) or call_ended(self._urc):
                break
            if not spoken and "+CTTS: 0" in self._urc.replace("+CTTS:0", "+CTTS: 0"):
                spoken, deadline = True, time.time() + DTMF_WAIT_S
            time.sleep(0.05)
        return dtmf_key(self._urc)


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


# ── Dry Contact Monitor Threads (INV-120) ────────────────────────────────────

def _report_dry_contact_state(gateway_id: str, state: str):
    """POST /internal/alarms/report-state {gateway_id, state}. Erreurs HTTP
    swallowed — le prochain poll retransmet (idempotent côté backend).
    Partagé par les moniteurs modem et hôte."""
    body = {"gateway_id": gateway_id, "state": state, "site": REPORT_SITE}  # INV-155
    try:
        result = _post_to_backend("/internal/alarms/report-state", json_data=body)
        if result is None:
            logger.debug(
                f"DryContact report-state : backend injoignable (state={state}). Retry next poll."
            )
        else:
            logger.debug(f"DryContact report-state : state={state} → {result}")
    except Exception as e:
        logger.error(f"DryContact report-state erreur : {e}")


class DryContactMonitorThread(threading.Thread):
    """INV-120 V2 — surveille un contact sec NC câblé sur GPIO 43 du SIM7600.

    Stateless level-based : à chaque poll, lit AT+CGGETV=<pin>, mappe la
    valeur brute en "open"|"closed" (via normal_value), et POST l'état
    courant à /internal/alarms/report-state. Le backend reconcilie (cf
    INV-120 V2 + INV-122 OR fail-to-alarm + INV-123 dissensus).

    Aucun état conservé entre polls (pas de debounce, pas d'edge detection,
    pas de mémoire d'id alarme). Un reboot/crash de la gateway est sans
    conséquence — le prochain poll reconcilie tout via le backend.

    Si AT+CGDRT échoue (pin non addressable par le firmware), le thread
    se termine en logguant — la gateway continue sans le trigger.
    """

    def __init__(
        self,
        ser: serial.Serial,
        gateway_id: str,
        gpio_pin: int,
        normal_value: int,
        poll_seconds: float,
    ):
        super().__init__(daemon=True, name="DryContactMonitor")
        self.ser = ser
        self.gateway_id = gateway_id
        self.gpio_pin = gpio_pin
        self.normal_value = normal_value
        self.poll_interval = max(0.5, poll_seconds)
        self.running = True
        self._last_logged_state: str | None = None  # juste pour log info sur changement

    def run(self):
        logger.info(
            f"DryContactMonitorThread demarre (gateway_id={self.gateway_id}, "
            f"GPIO pin {self.gpio_pin}, poll={self.poll_interval:.1f}s, "
            f"normal_value={self.normal_value})"
        )

        # Setup : passer la pin en entrée. Si refusé → thread arreté.
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

        while self.running:
            try:
                raw = self._read_gpio()
                if raw is None:
                    # Lecture invalide : pas de POST (l'absence d'update fait que le
                    # backend voit la dernière valeur tant qu'on est dans la fenêtre
                    # liveness ; si on enchaîne plusieurs invalides au-delà de 3 polls,
                    # le backend nous considère silencieux et nous ignore — INV-122).
                    logger.debug(f"DryContact ({self.gateway_id}) : lecture invalide, skip POST")
                    time.sleep(self.poll_interval)
                    continue

                state = raw_to_state(raw, self.normal_value)

                if state != self._last_logged_state:
                    if self._last_logged_state is None:
                        logger.info(
                            f"DryContact ({self.gateway_id}) : etat initial raw={raw} → state={state}"
                        )
                    else:
                        logger.warning(
                            f"DryContact ({self.gateway_id}) : transition d'etat "
                            f"{self._last_logged_state} → {state} (raw={raw})"
                        )
                    self._last_logged_state = state

                self._report_state(state)
                time.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"DryContactMonitor boucle erreur : {e}")
                time.sleep(1.0)

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

    def _report_state(self, state: str):
        _report_dry_contact_state(self.gateway_id, state)


class HostDryContactMonitorThread(threading.Thread):
    """INV-120 V2 — contact sec lu sur un microcontrôleur USB (Arduino), PAS le
    modem. Découple le contact-sec du SIM7600 : il marche même modem HS (le
    modem drop régulièrement → avant, contact aveugle en silence).

    Le firmware (cf gateway/firmware/dry_contact_r4/) pousse des lignes
    `DC:<0|1>` en continu (heartbeat ~1 Hz + sur changement). Mapping identique
    au moniteur modem via `raw_to_state` : `raw == normal_value` → 'closed'.

    Cadence : POST /internal/alarms/report-state toutes les `poll_seconds` avec
    le dernier état connu, **tant qu'il est frais** (< `liveness_seconds`). Si le
    µC se tait (débranché/HS) au-delà, on cesse de reporter → le backend voit le
    silence (INV-122) au lieu d'un état périmé. Reconnexion auto si le port tombe.
    """

    def __init__(
        self,
        port: str,
        baud: int,
        gateway_id: str,
        normal_value: int,
        poll_seconds: float,
        liveness_seconds: float,
    ):
        super().__init__(daemon=True, name="HostDryContactMonitor")
        self.port = port
        self.baud = baud
        self.gateway_id = gateway_id
        self.normal_value = normal_value
        self.poll_interval = max(0.5, poll_seconds)
        self.liveness = max(2.0, liveness_seconds)
        self.running = True
        self._last_logged_state: str | None = None

    def _resolve_port(self) -> str | None:
        """Port explicite, sinon auto-détect (by-id Arduino puis ttyACM*)."""
        if self.port:
            return self.port
        for pat in (
            "/dev/serial/by-id/*Arduino_UNO_R4*",
            "/dev/serial/by-id/*Arduino*",
            "/dev/ttyACM*",
        ):
            matches = sorted(glob.glob(pat))
            if matches:
                return matches[0]
        return None

    def run(self):
        logger.info(
            f"HostDryContactMonitorThread demarre (gateway_id={self.gateway_id}, "
            f"port={self.port or 'auto'}, poll={self.poll_interval:.1f}s, "
            f"normal_value={self.normal_value}, liveness={self.liveness:.0f}s)"
        )
        while self.running:
            port = self._resolve_port()
            if not port:
                logger.warning(
                    "HostDryContact : aucun port µC USB trouve (Arduino debranche ?). Retry 5s."
                )
                time.sleep(5.0)
                continue
            try:
                self._serve(port)
            except Exception as e:
                logger.error(f"HostDryContact : erreur sur {port} : {e}. Reouverture dans 3s.")
                time.sleep(3.0)

    def _serve(self, port: str):
        latest_raw: int | None = None
        latest_ts = 0.0
        last_post = 0.0
        silent_warned = False
        logger.info(f"HostDryContact : ouverture {port} @{self.baud}")
        with serial.Serial(port, self.baud, timeout=1.0) as ser:
            while self.running:
                raw_line = ser.readline().decode(errors="replace")
                v = parse_dc_line(raw_line)
                now = time.monotonic()
                if v is not None:
                    latest_raw = v
                    latest_ts = now

                if now - last_post < self.poll_interval:
                    continue
                last_post = now

                state = decide_report(
                    latest_raw, latest_ts, now, self.liveness, self.normal_value,
                )
                if state is None:
                    if not silent_warned:
                        logger.warning(
                            f"HostDryContact ({self.gateway_id}) : µC muet >{self.liveness:.0f}s "
                            f"— arret du report (le backend voit le silence)"
                        )
                        silent_warned = True
                        self._last_logged_state = None  # recovery sera reloguee
                    continue
                silent_warned = False

                if state != self._last_logged_state:
                    if self._last_logged_state is None:
                        logger.info(
                            f"HostDryContact ({self.gateway_id}) : etat raw={latest_raw} → {state}"
                        )
                    else:
                        logger.warning(
                            f"HostDryContact ({self.gateway_id}) : transition "
                            f"{self._last_logged_state} → {state} (raw={latest_raw})"
                        )
                    self._last_logged_state = state
                _report_dry_contact_state(self.gateway_id, state)


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

    # INV-132 : port audio USB (interface 4) pour injecter la voix dans l'appel
    from serial.tools import list_ports as lp
    audio_port = next((p.device for p in lp.comports() if is_audio_port(p.vid, p.location)), None)
    if audio_port:
        logger.info(f"Port audio detecte : {audio_port} (voix injectee dans l'appel)")
    else:
        logger.warning("Port audio non detecte — message par synthese du module (AT+CDTAM=1)")

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

    # INV-120 V2 — poll level-based contact sec NC (opt-in via DRY_CONTACT_ENABLED)
    if DRY_CONTACT_ENABLED:
        if DRY_CONTACT_SOURCE == "host":
            threads.append(
                HostDryContactMonitorThread(
                    port=DRY_CONTACT_HOST_PORT,
                    baud=DRY_CONTACT_HOST_BAUD,
                    gateway_id=GATEWAY_ID,
                    normal_value=DRY_CONTACT_NORMAL_VALUE,
                    poll_seconds=DRY_CONTACT_POLL_SECONDS,
                    liveness_seconds=DRY_CONTACT_LIVENESS_SECONDS,
                )
            )
            logger.info(
                f"Contact sec active (source=host) : gateway_id={GATEWAY_ID}, "
                f"port={DRY_CONTACT_HOST_PORT or 'auto'}, poll={DRY_CONTACT_POLL_SECONDS:.1f}s, "
                f"normal_value={DRY_CONTACT_NORMAL_VALUE}"
            )
        else:
            threads.append(
                DryContactMonitorThread(
                    ser=ser,
                    gateway_id=GATEWAY_ID,
                    gpio_pin=DRY_CONTACT_GPIO_PIN,
                    normal_value=DRY_CONTACT_NORMAL_VALUE,
                    poll_seconds=DRY_CONTACT_POLL_SECONDS,
                )
            )
            logger.info(
                f"Contact sec active (source=modem) : gateway_id={GATEWAY_ID}, "
                f"GPIO pin{DRY_CONTACT_GPIO_PIN}, poll={DRY_CONTACT_POLL_SECONDS:.1f}s, "
                f"normal_value={DRY_CONTACT_NORMAL_VALUE}"
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
    at_failures = 0
    unregistered, reregister_tried = 0, False
    delay = 0  # 1er controle immediat : modem_ready est pose avant le 1er envoi (INV-145)
    try:
        while True:
            time.sleep(delay)
            delay = 60
            # Verifier que le modem repond toujours
            with at_lock:
                r = send_at_command(ser, "AT", timeout=3)
            at_failures = at_failures_after(r, at_failures)
            if at_failures:
                modem_ready.clear()
            if at_failures >= MAX_AT_FAILURES:
                # Le port a pu changer (modem re-enumere sur l'USB) : on sort, systemd
                # (Restart=always) relance la gateway qui re-detecte le bon port.
                logger.critical(
                    f"Modem muet sur {at_failures} keepalives AT — arret pour relance "
                    f"systemd et re-detection du port"
                )
                return 1
            if at_failures:
                logger.error("Modem ne repond pas a AT — nouvel essai dans 60s")
                continue

            # INV-129 : etat reseau -> backend (indicateur de signal app + web)
            with at_lock:
                net = parse_network(
                    send_at_command(ser, "AT+CREG?", timeout=3),
                    send_at_command(ser, "AT+CPSI?", timeout=3),
                    send_at_command(ser, "AT+CSQ", timeout=3),
                )
            _post_to_backend("/internal/alarms/modem-status", {**net, "site": REPORT_SITE})  # INV-155
            if sends_allowed(at_failures, net["registered"]):
                modem_ready.set()
            else:
                modem_ready.clear()

            # INV-128 : modem inscrit nulle part depuis ~5 min -> radio coupee/rallumee
            unregistered = 0 if net["registered"] else unregistered + 1
            if net["registered"]:
                reregister_tried = False
            elif reregister_due(unregistered, reregister_tried):
                reregister_tried = True
                logger.warning("Modem sans reseau depuis ~5 min — radio coupee puis rallumee (INV-128)")
                with at_lock:
                    send_at_command(ser, "AT+CFUN=0", timeout=15)
                    time.sleep(5)
                    send_at_command(ser, "AT+CFUN=1", timeout=15)
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
