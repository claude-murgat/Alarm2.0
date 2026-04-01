#!/usr/bin/env bash
# install.sh — Installation de la gateway SMS + health monitor on-site
# Alarm 2.0 — Machine Linux locale avec clé USB GSM
#
# Usage : sudo bash install.sh
# Prérequis : Python 3.10+, connexion internet (pour apt + pip), clé USB GSM
#
# Ce script :
#   1. Installe gammu
#   2. Installe les dépendances Python
#   3. Crée le fichier de configuration /etc/alarm-gateway.env
#   4. Installe et active deux services systemd :
#      - alarm-sms-gateway   (poll les SMS en attente et les envoie)
#      - alarm-health-monitor (surveille les VPS, alerte si panne totale)

set -euo pipefail

# ─── Paramètres ────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/alarm-gateway"
ENV_FILE="/etc/alarm-gateway.env"
SERVICE_USER="alarm-gateway"
PYTHON="python3"

# ─── Couleurs pour les logs ────────────────────────────────────────────────────
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
RESET="\033[0m"

info()    { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }

# ─── Vérifications préalables ─────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    error "Ce script doit être exécuté en tant que root (sudo bash install.sh)"
    exit 1
fi

if ! command -v $PYTHON &>/dev/null; then
    error "Python 3 non trouvé. Installer avec : apt-get install python3"
    exit 1
fi

info "=== Installation Alarm 2.0 — Gateway SMS + Health Monitor ==="

# ─── 1. Dépendances système ────────────────────────────────────────────────────
info "Installation de gammu..."
apt-get update -qq
apt-get install -y gammu python3-pip

# ─── 2. Utilisateur système dédié ─────────────────────────────────────────────
if ! id -u "$SERVICE_USER" &>/dev/null; then
    info "Création de l'utilisateur système '$SERVICE_USER'..."
    useradd --system --no-create-home --shell /bin/false "$SERVICE_USER"
fi

# ─── 3. Répertoire d'installation ─────────────────────────────────────────────
info "Copie des scripts dans $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$(dirname "$0")"/*.py "$INSTALL_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"

# ─── 4. Dépendances Python ────────────────────────────────────────────────────
info "Installation des dépendances Python..."
$PYTHON -m pip install --quiet --break-system-packages -r "$(dirname "$0")/requirements.txt"

# ─── 5. Fichier de configuration ──────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    info "Création du fichier de configuration $ENV_FILE..."
    cat > "$ENV_FILE" << 'EOF'
# Configuration Alarm 2.0 — Gateway SMS + Health Monitor
# Modifier ces valeurs pour votre installation

# URLs des deux VPS backend
VPS1_URL=http://vps1.example.com:8000
VPS2_URL=http://vps2.example.com:8000

# Clé d'authentification pour les endpoints /internal/sms/*
# Doit correspondre à GATEWAY_KEY dans le docker-compose.yml du backend
GATEWAY_KEY=changeme-in-prod

# Numéros de téléphone à alerter en cas de panne totale
# Format E.164, séparés par virgules (ex: +33600000001,+33600000002)
ALERT_RECIPIENTS=

# Chemin vers la configuration gammu (créer avec : gammu-config)
GAMMU_CONFIG=/etc/gammurc

# Intervalles en secondes
POLL_INTERVAL_SECONDS=30
HEALTH_CHECK_INTERVAL_SECONDS=300

# Cooldown entre deux alertes (évite le spam SMS en cas de panne prolongée)
ALERT_COOLDOWN_SECONDS=1800

# Timeout HTTP pour les requêtes vers les VPS
HTTP_TIMEOUT_SECONDS=10

# Chemin vers le fichier audio Asterisk (sans extension .wav)
# Enregistrer un message avec : arecord -f cd /opt/alarm-gateway/alarme.wav
ALARM_AUDIO_FILE=/opt/alarm-gateway/alarme
EOF
    chmod 600 "$ENV_FILE"
    warn "IMPORTANT : Éditer $ENV_FILE avec vos paramètres avant de démarrer les services."
else
    info "Fichier de configuration $ENV_FILE déjà présent — non modifié."
fi

# ─── 6. Service systemd — alarm-sms-gateway ───────────────────────────────────
info "Installation du service alarm-sms-gateway..."
cat > /etc/systemd/system/alarm-sms-gateway.service << EOF
[Unit]
Description=Alarm 2.0 — SMS Gateway (poll VPS + send via USB GSM key)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON $INSTALL_DIR/sms_gateway.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=alarm-sms-gateway

# Sécurité minimale
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# ─── 7. Service systemd — alarm-health-monitor ────────────────────────────────
info "Installation du service alarm-health-monitor..."
cat > /etc/systemd/system/alarm-health-monitor.service << EOF
[Unit]
Description=Alarm 2.0 — Health Monitor (surveille les VPS, alerte si panne totale)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON $INSTALL_DIR/health_monitor.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=alarm-health-monitor

# Accès au spool Asterisk si présent
SupplementaryGroups=asterisk

# Sécurité minimale
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# ─── 8. Activation des services ───────────────────────────────────────────────
info "Rechargement systemd et activation des services..."
systemctl daemon-reload
systemctl enable alarm-sms-gateway alarm-health-monitor

info ""
info "=== Installation terminée ==="
info ""
info "Prochaines étapes :"
info "  1. Configurer la clé USB GSM   : gammu-config  (ou éditer /etc/gammurc)"
info "  2. Éditer la configuration     : nano $ENV_FILE"
info "  3. Démarrer les services       : systemctl start alarm-sms-gateway alarm-health-monitor"
info "  4. Vérifier les logs           : journalctl -u alarm-sms-gateway -u alarm-health-monitor -f"
info ""
warn "Les services ne démarrent PAS automatiquement — configurer $ENV_FILE d'abord."
info ""
info "Test SMS manuel (une fois la clé configurée) :"
info "  gammu --config /etc/gammurc --sendsms TEXT +33600000001 'Test Alarm 2.0'"
