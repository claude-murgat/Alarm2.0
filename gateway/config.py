"""
Configuration de la gateway modem on-site — Alarm 2.0.
Toutes les valeurs sont lisibles depuis les variables d'environnement.
Creer /etc/alarm-gateway.env avec ces variables pour la production.
"""
import os
import socket

# URLs des 3 noeuds backend (cluster Patroni)
NODE1_URL = os.getenv("NODE1_URL", "http://localhost:8000")
NODE2_URL = os.getenv("NODE2_URL", "http://localhost:8001")
NODE3_URL = os.getenv("NODE3_URL", "http://localhost:8002")
ALL_NODE_URLS = [NODE1_URL, NODE2_URL, NODE3_URL]

# Legacy aliases (pour sms_gateway_legacy.py)
VPS1_URL = NODE1_URL
VPS2_URL = NODE2_URL

# Cle d'authentification pour les endpoints /internal/sms/* et /internal/calls/*
GATEWAY_KEY = os.getenv("GATEWAY_KEY", "changeme-in-prod")

# Numeros de telephone d'alerte (pour le health monitor)
# Format E.164 separe par virgules : "+33600000001,+33600000002"
ALERT_RECIPIENTS = [
    n.strip() for n in os.getenv("ALERT_RECIPIENTS", "").split(",") if n.strip()
]

# Modem SIM7600E-H
MODEM_AT_PORT = os.getenv("MODEM_AT_PORT", "")  # Vide = auto-detect
MODEM_BAUD_RATE = int(os.getenv("MODEM_BAUD_RATE", "115200"))

# Intervalles (secondes)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
HEALTH_CHECK_INTERVAL_SECONDS = int(os.getenv("HEALTH_CHECK_INTERVAL_SECONDS", "300"))

# Cooldown entre deux alertes health monitor (evite le spam)
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "1800"))

# Timeout HTTP pour les requetes vers les backends
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))

# ── Contact sec NC local (INV-120 V2) ──────────────────────────────────────
# Déclenchement d'alarme par capteur câblé en NC (Normally Closed) sur la
# GPIO 43 du module SIM7600. Refonte 2026-05-18 (issue #112) : level-based
# reconciliation. La gateway poll son contact toutes les N secondes et POST
# son état courant ("open" ou "closed") vers le backend, qui reconcilie.
# Stateless : aucun cache local, aucune mémoire d'événement. Un reboot de la
# gateway est sans conséquence — le prochain poll reconcilie tout.
#
# Politique d'agrégation côté backend = OR fail-to-alarm (INV-122) sur les N
# gateways alive. Détection dissensus si states divergents > 5 min (INV-123).
#
# Sur le HAT Waveshare SIM7600X 4G HAT (vérifié 2026-05-13 sur node2 onsite,
# firmware SIM7600M22_V2.0.1) :
#   - Label silkscreen "IO43" du header droit jaune = GPIO 43 du module
#     SIM7600 (lecture via AT+CGGETV=43 / AT+CGDRT=43,0). Mapping confirmé
#     empiriquement : touch 3V3 → CGGETV=43 lit 1 ; lâché → lit 0.
#   - GPIO 43 supporte le maintien 3V3 sans freeze (30s testé, 4818/4818
#     stable, AT health 5/5 OK). Default-low naturel quand floating.
#   - Câblage minimaliste : un fil entre 3V3 et IO43 avec le contact NC
#     coupé au milieu. Pas de résistance externe nécessaire.

# Feature flag — défaut OFF pour ne pas surprendre les déploiements existants.
DRY_CONTACT_ENABLED = os.getenv("DRY_CONTACT_ENABLED", "false").lower() in ("true", "1", "yes")

# Identifiant unique de cette gateway côté backend (INV-122 multi-gateway).
# Doit être stable dans le temps pour qu'une row gateway_states soit upserted
# au même endroit à chaque poll. Par défaut = hostname système (suffisant pour
# 2 nœuds onsite identifiés par leur nom de host).
GATEWAY_ID = os.getenv("GATEWAY_ID", socket.gethostname())

# Numéro de GPIO module SIM7600. Sur le HAT Waveshare SIM7600X : 43 = label
# IO43 (mapping validé 2026-05-13). Pins addressables découvertes sur
# firmware SIM7600M22_V2.0.1 : 3, 6, 41, 43, 44, 77 (40=STATUS réservé).
# Default 43 car c'est la seule physiquement exposée en breakout sur ce HAT.
DRY_CONTACT_GPIO_PIN = int(os.getenv("DRY_CONTACT_GPIO_PIN", "43"))

# Valeur brute lue par AT+CGGETV au REPOS (0 ou 1).
# Câblage GPIO 43 retenu : 3V3 ─── [contact NC] ─── IO43.
# Repos NC fermé → IO43=3V3 → CGGETV=43 lit 1 → mappé en "closed".
# Alarme NC ouvert → IO43 flottant, default-low → CGGETV=43 lit 0 → "open".
# NORMAL_VALUE=1 par défaut. Tout sample != NORMAL_VALUE est mappé en "open".
DRY_CONTACT_NORMAL_VALUE = int(os.getenv("DRY_CONTACT_NORMAL_VALUE", "1"))

# Période de polling en secondes. Défaut 5 s (cf design level-based issue #112).
# Le backend marque une gateway comme silencieuse au-delà de
# GATEWAY_LIVENESS_WINDOW_SECONDS (défaut 15 s côté backend, soit 3 polls
# manqués). Baisser ce poll = plus de bande passante mais détection plus
# rapide ; remonter = inverse. Pour un contact sec physique, 5 s est largement
# suffisant.
DRY_CONTACT_POLL_SECONDS = float(os.getenv("DRY_CONTACT_POLL_SECONDS", "5"))


# Legacy (deprecated)
GAMMU_CONFIG = os.getenv("GAMMU_CONFIG", "/etc/gammurc")
