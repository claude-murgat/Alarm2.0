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

# ── Contact sec NC local (INV-120) ──────────────────────────────────────────
# Déclenchement d'alarme par capteur câblé en NC (Normally Closed) sur une
# entrée du module SIM7600 — soit une GPIO digitale (AT+CGGETV), soit un ADC
# (AT+CADC=<channel>) selon ce qui est physiquement exposé par le HAT.
#
# Sur le HAT Waveshare SIM7600X 4G HAT (vérifié 2026-05-13 sur node2 onsite,
# firmware SIM7600M22_V2.0.1) :
#   - Label silkscreen "IO43" du header droit jaune = GPIO 43 du module
#     SIM7600 (lecture via AT+CGGETV=43 / AT+CGDRT=43,0). Mapping confirmé
#     empiriquement : touch 3V3 → CGGETV=43 lit 1 ; lâché → lit 0.
#   - GPIO 43 supporte le maintien 3V3 sans freeze (30s testé, 4818/4818
#     stable, AT health 5/5 OK). Default-low naturel quand floating.
#   - Donc DRY_CONTACT_MODE=gpio + GPIO_PIN=43 + NORMAL_VALUE=1 par défaut.
#   - Câblage minimaliste : un fil entre 3V3 et IO43 avec le contact NC
#     coupé au milieu. Pas de résistance externe nécessaire.
#
# Le mode ADC est conservé en fallback (cf historique : ADC1 fonctionne
# mais le maintien à GND freeze le module → câblage moins propre).

# Feature flag — défaut OFF pour ne pas surprendre les déploiements existants.
DRY_CONTACT_ENABLED = os.getenv("DRY_CONTACT_ENABLED", "false").lower() in ("true", "1", "yes")

# Stratégie de détection : "gpio" (AT+CGGETV=<pin>) ou "adc" (AT+CADC=<channel>).
DRY_CONTACT_MODE = os.getenv("DRY_CONTACT_MODE", "gpio").lower()

# Mode GPIO — numéro de GPIO module SIM7600 (à valider avec probe_dry_contact.py).
# Sur le HAT Waveshare SIM7600X : 43 = label IO43 (mapping validé 2026-05-13).
# Pins addressables découvertes sur firmware SIM7600M22_V2.0.1 : 3, 6, 41, 43,
# 44, 77 (40=STATUS réservé). Default 43 car c'est la seule physiquement
# exposée en breakout sur ce HAT.
DRY_CONTACT_GPIO_PIN = int(os.getenv("DRY_CONTACT_GPIO_PIN", "43"))

# Mode ADC — canal AT+CADC (0..2 sur SIM7600). Vérifié 2026-05-13 : channel 2
# = pin ADC1 du HAT. Channel 0 = VBAT (interne), Channel 1 = ERROR.
DRY_CONTACT_ADC_CHANNEL = int(os.getenv("DRY_CONTACT_ADC_CHANNEL", "2"))

# Mode ADC — seuil de bascule en mV. Le firmware SIM7600 sature à ~1800 mV
# quand on applique 3V3 sur l'entrée ADC1 (diviseur tension interne).
# Donc 900 mV = midpoint sûr entre 0 mV (NC fermé→GND) et 1800 mV (NC ouvert).
DRY_CONTACT_ADC_THRESHOLD_MV = int(os.getenv("DRY_CONTACT_ADC_THRESHOLD_MV", "900"))

# Valeur normalisée AU REPOS (0 ou 1).
# Câblage GPIO 43 retenu sur le HAT Waveshare SIM7600X (validé 2026-05-13) :
#   3V3 ─── [contact NC] ─── IO43 (= GPIO 43 module)
# Repos NC fermé → IO43=3V3 → CGGETV=43 lit 1 = NORMAL_VALUE.
# Alarme NC ouvert → IO43 flottant, default-low → CGGETV=43 lit 0.
# Front d'alarme = transition 1→0 (descendant). NORMAL_VALUE=1 par défaut.
# /!\ NE PAS inverser : maintien ADC1 à GND (mode ADC) PLANTE le module
# (constat empirique, freeze AT + USB intact, recovery via PWRKEY uniquement).
# GPIO 43 lui supporte les deux états (validé 30s+30s).
DRY_CONTACT_NORMAL_VALUE = int(os.getenv("DRY_CONTACT_NORMAL_VALUE", "1"))

# Debounce minimum (INV-120 : 200 ms valeur retenue 2026-05-13).
DRY_CONTACT_DEBOUNCE_MS = int(os.getenv("DRY_CONTACT_DEBOUNCE_MS", "200"))

# Période de polling. 100 ms en GPIO, plus long en ADC (CADC ~300 ms par lecture).
DRY_CONTACT_POLL_MS = int(os.getenv("DRY_CONTACT_POLL_MS", "100"))

# Title/Message envoyés au backend. Vide → defaults serveur.
DRY_CONTACT_TITLE = os.getenv("DRY_CONTACT_TITLE", "")
DRY_CONTACT_MESSAGE = os.getenv("DRY_CONTACT_MESSAGE", "")


# Legacy (deprecated)
GAMMU_CONFIG = os.getenv("GAMMU_CONFIG", "/etc/gammurc")
