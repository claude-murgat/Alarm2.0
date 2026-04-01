#!/usr/bin/env python3
"""
Gateway SMS on-site — Alarm 2.0

Rôle : poll les SMS en attente depuis le backend cloud et les envoie
via la clé USB GSM (modem AT commands, ex: Huawei E173, ZTE MF190).

Prérequis hardware : modem USB GSM en mode AT commands (PAS HiLink)
Prérequis logiciel : gammu installé (apt-get install gammu)

Démarrage : python3 sms_gateway.py
En production : service systemd alarm-sms-gateway (voir install.sh)
"""
import logging
import subprocess
import time

import requests

from config import (
    VPS1_URL, VPS2_URL, GATEWAY_KEY, GAMMU_CONFIG,
    POLL_INTERVAL_SECONDS, HTTP_TIMEOUT_SECONDS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SMS-GATEWAY] %(levelname)s %(message)s",
)
logger = logging.getLogger("sms_gateway")

GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}


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
            logger.info(f"SMS envoyé → {to_number}")
            return True, ""
        else:
            err = result.stderr.decode(errors="replace").strip()
            logger.warning(f"gammu erreur (returncode={result.returncode}): {err}")
            return False, err or "gammu_error"
    except FileNotFoundError:
        logger.error(
            "gammu non trouvé dans PATH ou clé USB GSM absente. "
            "SMS non envoyé — sera réessayé au prochain cycle."
        )
        return False, "gammu_not_found"
    except subprocess.TimeoutExpired:
        logger.error("gammu timeout (modem ne répond pas)")
        return False, "timeout"
    except Exception as e:
        logger.error(f"Erreur inattendue lors de l'envoi SMS : {e}")
        return False, str(e)


def fetch_pending_sms(base_url: str) -> list[dict] | None:
    """Récupère les SMS en attente depuis un VPS. Retourne None si indisponible."""
    try:
        r = requests.get(
            f"{base_url}/internal/sms/pending",
            headers=GATEWAY_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning(f"GET /internal/sms/pending → HTTP {r.status_code} sur {base_url}")
        return None
    except Exception as e:
        logger.warning(f"Impossible de joindre {base_url} : {e}")
        return None


def mark_sent(base_url: str, sms_id: int) -> None:
    try:
        requests.post(
            f"{base_url}/internal/sms/{sms_id}/sent",
            headers=GATEWAY_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.warning(f"Impossible de marquer SMS {sms_id} comme envoyé : {e}")


def mark_error(base_url: str, sms_id: int, error: str) -> None:
    try:
        requests.post(
            f"{base_url}/internal/sms/{sms_id}/error",
            json={"error": error},
            headers=GATEWAY_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.warning(f"Impossible de marquer SMS {sms_id} en erreur : {e}")


def main():
    logger.info("Gateway SMS démarrée")
    logger.info(f"VPS primaire : {VPS1_URL} | VPS secondaire : {VPS2_URL}")
    logger.info(f"Intervalle de poll : {POLL_INTERVAL_SECONDS}s")

    while True:
        try:
            # Essayer VPS1, fallback sur VPS2
            pending = fetch_pending_sms(VPS1_URL)
            active_url = VPS1_URL
            if pending is None:
                logger.info("VPS1 indisponible, bascule sur VPS2")
                pending = fetch_pending_sms(VPS2_URL)
                active_url = VPS2_URL

            if pending is None:
                logger.warning("Les deux VPS sont injoignables — retry dans 30s")
            elif len(pending) == 0:
                logger.debug("Aucun SMS en attente")
            else:
                logger.info(f"{len(pending)} SMS en attente à envoyer")
                for sms in pending:
                    sms_id = sms["id"]
                    to_number = sms["to_number"]
                    body = sms["body"]

                    success, error_msg = send_sms_via_gammu(to_number, body)

                    if success:
                        mark_sent(active_url, sms_id)
                    else:
                        mark_error(active_url, sms_id, error_msg)

        except Exception as e:
            logger.error(f"Erreur inattendue dans la boucle principale : {e}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
