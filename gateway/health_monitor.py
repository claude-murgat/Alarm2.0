#!/usr/bin/env python3
"""
Health Monitor on-site — Alarm 2.0

Rôle : surveille la disponibilité des deux VPS backend.
Si les DEUX sont injoignables, envoie un SMS d'alerte via la clé USB GSM
et, si Asterisk est installé, dépose un call file pour appeler l'astreinte.

Prérequis hardware : modem USB GSM en mode AT commands (optionnel — si absent, log uniquement)
Prérequis logiciel : gammu installé (optionnel), Asterisk (optionnel pour les appels voix)

Démarrage : python3 health_monitor.py
En production : service systemd alarm-health-monitor (voir install.sh)
"""
import logging
import os
import subprocess
import time
from datetime import datetime

import requests

from config import (
    VPS1_URL, VPS2_URL, GATEWAY_KEY, GAMMU_CONFIG,
    ALERT_RECIPIENTS, HEALTH_CHECK_INTERVAL_SECONDS,
    ALERT_COOLDOWN_SECONDS, HTTP_TIMEOUT_SECONDS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HEALTH-MONITOR] %(levelname)s %(message)s",
)
logger = logging.getLogger("health_monitor")

# Répertoire de spool Asterisk — si présent, on dépose des call files pour les appels voix
ASTERISK_OUTGOING_DIR = "/var/spool/asterisk/outgoing"

# Chemin vers le fichier WAV à jouer lors de l'appel (créer avec espeak ou enregistrement réel)
ALARM_AUDIO_FILE = os.getenv("ALARM_AUDIO_FILE", "/opt/alarm-gateway/alarme")

# Timestamp de la dernière alerte envoyée (None = jamais alerté)
last_alert_sent: float = 0.0


def check_vps_health(base_url: str) -> bool:
    """
    Appelle GET /health sur un VPS.
    Retourne True si le VPS répond avec HTTP 200, False sinon.
    """
    try:
        r = requests.get(
            f"{base_url}/health",
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        return r.status_code == 200
    except Exception as e:
        logger.debug(f"Impossible de joindre {base_url}/health : {e}")
        return False


def send_sms_via_gammu(to_number: str, body: str) -> tuple[bool, str]:
    """
    Envoie un SMS via la clé USB GSM avec gammu.
    Retourne (succès, message_erreur).
    Ne lève jamais d'exception — dégrade gracieusement si matériel absent.
    """
    try:
        result = subprocess.run(
            ["gammu", "--config", GAMMU_CONFIG, "--sendsms", "TEXT", to_number, body],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info(f"SMS d'alerte envoyé → {to_number}")
            return True, ""
        else:
            err = result.stderr.decode(errors="replace").strip()
            logger.warning(f"gammu erreur (returncode={result.returncode}): {err}")
            return False, err or "gammu_error"
    except FileNotFoundError:
        logger.error(
            "gammu non trouvé dans PATH ou clé USB GSM absente. "
            "SMS d'alerte non envoyé."
        )
        return False, "gammu_not_found"
    except subprocess.TimeoutExpired:
        logger.error("gammu timeout (modem ne répond pas)")
        return False, "timeout"
    except Exception as e:
        logger.error(f"Erreur inattendue lors de l'envoi SMS : {e}")
        return False, str(e)


def deposit_asterisk_call_file(to_number: str) -> bool:
    """
    Dépose un call file dans le spool Asterisk pour déclencher un appel voix.
    Asterisk appellera to_number et jouera ALARM_AUDIO_FILE quand quelqu'un décroche.
    Retourne True si le dépôt a réussi, False sinon.
    Ne lève jamais d'exception.
    """
    if not os.path.isdir(ASTERISK_OUTGOING_DIR):
        return False  # Asterisk non installé — silencieux

    # Nom unique pour éviter les collisions
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_number = to_number.replace("+", "00").replace(" ", "")
    call_file_path = os.path.join(
        ASTERISK_OUTGOING_DIR, f"alarm_{safe_number}_{timestamp}.call"
    )

    content = (
        f"Channel: DONGLE/dongle0/{to_number}\n"
        f"MaxRetries: 3\n"
        f"RetryTime: 60\n"
        f"WaitTime: 30\n"
        f"Application: Playback\n"
        f"Data: {ALARM_AUDIO_FILE}\n"
    )

    try:
        # Écrire dans un fichier temporaire puis renommer (opération atomique)
        tmp_path = call_file_path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(content)
        os.rename(tmp_path, call_file_path)
        logger.info(f"Call file Asterisk déposé pour {to_number} : {call_file_path}")
        return True
    except Exception as e:
        logger.error(f"Impossible de déposer le call file Asterisk pour {to_number} : {e}")
        return False


def send_alerts(message: str) -> None:
    """
    Envoie un SMS à tous les destinataires d'alerte.
    Dépose également des call files Asterisk si disponible.
    Si gammu est absent, log uniquement — ne crashe pas.
    """
    if not ALERT_RECIPIENTS:
        logger.warning(
            "Aucun destinataire d'alerte configuré (ALERT_RECIPIENTS vide). "
            "Configurer /etc/alarm-gateway.env avec ALERT_RECIPIENTS=+336xxxxxxxx,..."
        )
        return

    for recipient in ALERT_RECIPIENTS:
        # SMS
        success, error_msg = send_sms_via_gammu(recipient, message)
        if not success:
            logger.warning(f"SMS d'alerte non envoyé à {recipient} : {error_msg}")

        # Appel voix (si Asterisk installé)
        call_ok = deposit_asterisk_call_file(recipient)
        if call_ok:
            logger.info(f"Appel voix planifié pour {recipient}")


def main():
    global last_alert_sent

    logger.info("Health Monitor démarré")
    logger.info(f"VPS primaire : {VPS1_URL} | VPS secondaire : {VPS2_URL}")
    logger.info(f"Intervalle de vérification : {HEALTH_CHECK_INTERVAL_SECONDS}s")
    logger.info(f"Cooldown alertes : {ALERT_COOLDOWN_SECONDS}s")

    if ALERT_RECIPIENTS:
        logger.info(f"Destinataires d'alerte : {', '.join(ALERT_RECIPIENTS)}")
    else:
        logger.warning("Aucun destinataire d'alerte configuré — health monitor passif")

    if os.path.isdir(ASTERISK_OUTGOING_DIR):
        logger.info(f"Asterisk détecté ({ASTERISK_OUTGOING_DIR}) — appels voix activés")
    else:
        logger.info("Asterisk non détecté — alertes SMS uniquement")

    while True:
        try:
            vps1_ok = check_vps_health(VPS1_URL)
            vps2_ok = check_vps_health(VPS2_URL)

            if vps1_ok or vps2_ok:
                # Au moins un VPS est vivant
                if vps1_ok and vps2_ok:
                    logger.debug("VPS1 OK | VPS2 OK")
                elif vps1_ok:
                    logger.warning("VPS1 OK | VPS2 KO — backend dégradé (1/2 VPS)")
                else:
                    logger.warning("VPS1 KO | VPS2 OK — backend dégradé (1/2 VPS)")
            else:
                # Les DEUX VPS sont morts
                logger.critical("VPS1 KO | VPS2 KO — PANNE TOTALE DU BACKEND")

                now = time.monotonic()
                elapsed_since_last_alert = now - last_alert_sent

                if elapsed_since_last_alert >= ALERT_COOLDOWN_SECONDS:
                    logger.critical(
                        f"Cooldown écoulé ({elapsed_since_last_alert:.0f}s >= {ALERT_COOLDOWN_SECONDS}s) "
                        f"— envoi des alertes"
                    )
                    message = (
                        "ALERTE CRITIQUE : Les deux serveurs backend Alarm 2.0 "
                        "sont injoignables. Vérifier immédiatement."
                    )
                    send_alerts(message)
                    last_alert_sent = now
                else:
                    remaining = ALERT_COOLDOWN_SECONDS - elapsed_since_last_alert
                    logger.warning(
                        f"Panne totale détectée mais cooldown actif — "
                        f"prochaine alerte dans {remaining:.0f}s"
                    )

        except Exception as e:
            logger.error(f"Erreur inattendue dans la boucle principale : {e}")

        time.sleep(HEALTH_CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
