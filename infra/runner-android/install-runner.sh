#!/usr/bin/env bash
# Bootstrap runner self-hosted Linux dedie emulateurs Android.
#
# A executer EN ROOT sur la VM Linux fraichement provisionnee
# (Ubuntu 22.04+ recommande). Idempotent : re-executable.
#
# Usage :
#   sudo bash install-runner.sh --token <REGISTRATION_TOKEN>
#
# Token : `gh api -X POST repos/claude-murgat/Alarm2.0/actions/runners/registration-token --jq .token`
# Valide 1h.
#
# Effet :
#   1. Install JDK 17, build deps, Docker CE
#   2. Install Android SDK (cmdline-tools + platform-tools + emulator + system-image)
#   3. Cree le user `ghrunner` (groupes: kvm, docker)
#   4. Telecharge actions/runner v2.333.1, configure, install systemd service

set -euo pipefail

# --- Config ---
RUNNER_VERSION="2.333.1"
RUNNER_USER="ghrunner"
RUNNER_HOME="/opt/actions-runner-android"
REPO_URL="https://github.com/claude-murgat/Alarm2.0"
RUNNER_NAME="alarm-android-1"
RUNNER_LABELS="self-hosted,linux,alarm-android"
ANDROID_HOME="/opt/android-sdk"
ANDROID_API_LEVEL="30"
ANDROID_TARGET="google_apis"
ANDROID_ARCH="x86_64"
SDK_PACKAGES=(
    "platform-tools"
    "emulator"
    "platforms;android-${ANDROID_API_LEVEL}"
    "build-tools;34.0.0"
    "system-images;android-${ANDROID_API_LEVEL};${ANDROID_TARGET};${ANDROID_ARCH}"
)

# --- Parse args ---
TOKEN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --token) TOKEN="$2"; shift 2;;
        --runner-name) RUNNER_NAME="$2"; shift 2;;
        --runner-version) RUNNER_VERSION="$2"; shift 2;;
        *) echo "Arg inconnu: $1" >&2; exit 1;;
    esac
done

if [[ -z "$TOKEN" ]]; then
    echo "ERREUR : --token <REGISTRATION_TOKEN> requis." >&2
    echo "  gh api -X POST repos/claude-murgat/Alarm2.0/actions/runners/registration-token --jq .token" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERREUR : a executer en root (sudo bash $0 --token ...)" >&2
    exit 1
fi

step() { echo; echo "==> $*"; }

# --- 1. APT deps ---
step "Install dependances APT..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q \
    openjdk-17-jdk \
    unzip curl wget \
    socat libpulse0 \
    ca-certificates gnupg \
    git python3 python3-pip

# --- 2. Docker CE (si absent) ---
step "Install Docker CE (skip si present)..."
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor --batch --yes -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    UBUNTU_CODENAME=$(. /etc/os-release && echo "$UBUNTU_CODENAME")
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $UBUNTU_CODENAME stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
else
    echo "  docker deja installe : $(docker --version)"
fi

# --- 3. KVM check (info, le script continue meme si KO pour debug) ---
step "Verification KVM (/dev/kvm)..."
if [[ -e /dev/kvm ]]; then
    ls -l /dev/kvm
    echo "  KVM OK"
else
    echo "  WARNING: /dev/kvm absent. Active la nested virt au niveau de l'hyperviseur."
    echo "  L'install continue mais l'emulateur tournera en software (5-10x plus lent)."
fi

# --- 4. User runner ---
step "User runner '$RUNNER_USER'..."
if ! id -u "$RUNNER_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$RUNNER_USER"
    echo "  user $RUNNER_USER cree"
else
    echo "  user $RUNNER_USER existe deja"
fi
# Groupes : kvm pour /dev/kvm, docker pour socket
usermod -aG kvm "$RUNNER_USER" 2>/dev/null || groupadd kvm && usermod -aG kvm "$RUNNER_USER"
usermod -aG docker "$RUNNER_USER"

# --- 5. Android SDK ---
step "Install Android SDK dans $ANDROID_HOME..."
if [[ ! -d "$ANDROID_HOME/cmdline-tools/latest" ]]; then
    mkdir -p "$ANDROID_HOME/cmdline-tools"
    cd /tmp
    CMDLINE_ZIP="commandlinetools-linux-11076708_latest.zip"
    if [[ ! -f "$CMDLINE_ZIP" ]]; then
        wget -q "https://dl.google.com/android/repository/${CMDLINE_ZIP}"
    fi
    unzip -q -o "$CMDLINE_ZIP" -d "$ANDROID_HOME/cmdline-tools/"
    # Le zip extrait dans cmdline-tools/cmdline-tools/, on renomme en `latest`
    mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
    rm -f "$CMDLINE_ZIP"
fi
chown -R "$RUNNER_USER:$RUNNER_USER" "$ANDROID_HOME"

# Accept licenses + install packages (en tant que user runner pour bons droits)
sudo -u "$RUNNER_USER" -H bash <<EOF
set -e
export ANDROID_HOME="$ANDROID_HOME"
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:\$PATH"
yes | sdkmanager --licenses >/dev/null 2>&1 || true
sdkmanager --install $(printf '"%s" ' "${SDK_PACKAGES[@]}")
EOF

# Exporter ANDROID_HOME globalement
cat > /etc/profile.d/android.sh <<EOF
export ANDROID_HOME="$ANDROID_HOME"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="\$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator"
EOF
chmod +x /etc/profile.d/android.sh

# --- 6. actions/runner ---
step "Install actions/runner v$RUNNER_VERSION dans $RUNNER_HOME..."

# Desinstall propre si existant
if [[ -d "$RUNNER_HOME" ]]; then
    echo "  install precedente detectee, cleanup..."
    if systemctl list-unit-files | grep -q "actions.runner.*alarm-android"; then
        SVC=$(systemctl list-unit-files | grep -E "actions.runner.*alarm-android" | head -1 | awk '{print $1}')
        systemctl stop "$SVC" 2>/dev/null || true
        cd "$RUNNER_HOME"
        sudo -u "$RUNNER_USER" ./svc.sh uninstall 2>/dev/null || true
    fi
    # Unregister du repo (best-effort, requiert un removal token frais)
    if [[ -f "$RUNNER_HOME/config.sh" ]]; then
        cd "$RUNNER_HOME"
        # Le user lui-meme demande un removal token via gh API si dispo.
        # Sinon : a cleanup manuellement dans GH UI.
        sudo -u "$RUNNER_USER" ./config.sh remove --token "$TOKEN" 2>/dev/null || \
            echo "  WARNING: unregister echoue (token est un registration token, pas un removal). Cleanup manuel possible dans GH Settings -> Actions -> Runners."
    fi
    rm -rf "$RUNNER_HOME"
fi

mkdir -p "$RUNNER_HOME"
chown "$RUNNER_USER:$RUNNER_USER" "$RUNNER_HOME"

cd "$RUNNER_HOME"
RUNNER_TARBALL="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
sudo -u "$RUNNER_USER" wget -q "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TARBALL}"
sudo -u "$RUNNER_USER" tar xzf "$RUNNER_TARBALL"
rm -f "$RUNNER_TARBALL"

# --- 7. Configure runner ---
step "Enregistrement runner sur $REPO_URL labels [$RUNNER_LABELS]..."
sudo -u "$RUNNER_USER" ./config.sh \
    --url "$REPO_URL" \
    --token "$TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work "_work" \
    --unattended \
    --replace

# --- 8. Install + start systemd service ---
step "Install service systemd..."
./svc.sh install "$RUNNER_USER"
./svc.sh start
sleep 2
SVC=$(systemctl list-unit-files | grep -E "actions.runner.*alarm-android" | head -1 | awk '{print $1}')
if systemctl is-active --quiet "$SVC"; then
    echo "  OK service: $SVC [active]"
else
    echo "  WARNING: $SVC pas active. Check : journalctl -u $SVC -n 50"
fi

# --- 9. Final ---
echo ""
echo "Runner installe."
echo "Verifie sur : $REPO_URL/settings/actions/runners"
echo "Le runner '$RUNNER_NAME' doit apparaitre Idle."
echo ""
echo "Prochaine etape : sudo -u $RUNNER_USER -H bash setup-avds.sh"
