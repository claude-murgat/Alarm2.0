"""
Configuration de la gateway SMS on-site.
Toutes les valeurs sont lisibles depuis les variables d'environnement.
Créer /etc/alarm-gateway.env avec ces variables pour la production.
"""
import os

# URLs des deux VPS backend (réplication PostgreSQL)
VPS1_URL = os.getenv("VPS1_URL", "http://vps1.example.com:8000")
VPS2_URL = os.getenv("VPS2_URL", "http://vps2.example.com:8000")

# Clé d'authentification pour les endpoints /internal/sms/*
GATEWAY_KEY = os.getenv("GATEWAY_KEY", "changeme-in-prod")

# Numéros de téléphone d'alerte (pour le health monitor)
# Format E.164 séparé par virgules : "+33600000001,+33600000002"
ALERT_RECIPIENTS = [
    n.strip() for n in os.getenv("ALERT_RECIPIENTS", "").split(",") if n.strip()
]

# Chemin vers le fichier de configuration gammu
# Pour le configurer : gammu-config (ou éditer manuellement /etc/gammurc)
GAMMU_CONFIG = os.getenv("GAMMU_CONFIG", "/etc/gammurc")

# Intervalles (secondes)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
HEALTH_CHECK_INTERVAL_SECONDS = int(os.getenv("HEALTH_CHECK_INTERVAL_SECONDS", "300"))

# Cooldown entre deux alertes health monitor (évite le spam)
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "1800"))

# Timeout HTTP pour les requêtes vers les VPS
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))
