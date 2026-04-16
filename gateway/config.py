"""
Configuration de la gateway modem on-site — Alarm 2.0.
Toutes les valeurs sont lisibles depuis les variables d'environnement.
Creer /etc/alarm-gateway.env avec ces variables pour la production.
"""
import os

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

# Legacy (deprecated)
GAMMU_CONFIG = os.getenv("GAMMU_CONFIG", "/etc/gammurc")
