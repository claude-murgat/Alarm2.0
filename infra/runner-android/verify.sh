#!/usr/bin/env bash
# Smoke-test du runner Linux self-hosted Android. Affiche un recap PASS/FAIL
# des prerequis pour que tier4-failback.yml puisse tourner.
#
# Usage : bash verify.sh
#         (peut etre lance en tant que toi-meme ou en tant que ghrunner)

set +e

ANDROID_HOME="${ANDROID_HOME:-/opt/android-sdk}"
RUNNER_USER="ghrunner"

declare -i PASS=0
declare -i FAIL=0
RESULTS=()

check() {
    local name="$1"
    local cmd="$2"
    local fix_hint="$3"
    if eval "$cmd" >/dev/null 2>&1; then
        local detail
        detail=$(eval "$cmd" 2>&1 | head -1)
        RESULTS+=("PASS|$name|$detail|")
        ((PASS++))
    else
        RESULTS+=("FAIL|$name||$fix_hint")
        ((FAIL++))
    fi
}

# --- KVM ---
check "KVM (/dev/kvm exists)" \
    "[[ -e /dev/kvm ]]" \
    "Activer nested virt au niveau hyperviseur (helper enable-hyperv-nested-virt.ps1 si Hyper-V)"

check "KVM accessible R/W (groupe kvm)" \
    "[[ -r /dev/kvm && -w /dev/kvm ]]" \
    "sudo usermod -aG kvm \$USER puis se reconnecter"

# --- Java JDK 17+ ---
check "Java JDK 17+" \
    "java -version 2>&1 | grep -qE 'version \"(17|18|19|2[0-9])'" \
    "sudo apt install openjdk-17-jdk"

# --- Android SDK ---
check "ANDROID_HOME exists" \
    "[[ -d \"$ANDROID_HOME\" ]]" \
    "Lancer install-runner.sh"

for tool in sdkmanager avdmanager emulator adb; do
    check "SDK tool: $tool dans PATH" \
        "command -v $tool" \
        "Verifier ANDROID_HOME/cmdline-tools/latest/bin, /platform-tools, /emulator dans PATH (cf /etc/profile.d/android.sh)"
done

# --- AVDs requis (via le HOME du user runner) ---
RUNNER_HOME=$(getent passwd "$RUNNER_USER" 2>/dev/null | cut -d: -f6)
if [[ -z "$RUNNER_HOME" ]]; then RUNNER_HOME="$HOME"; fi

for port in 5552 5554 5556; do
    avd_name="alarm_${port}"
    avd_dir="$RUNNER_HOME/.android/avd/${avd_name}.avd"

    check "AVD $avd_name (sous $RUNNER_USER)" \
        "[[ -f \"$avd_dir/config.ini\" ]]" \
        "Lancer (en tant que $RUNNER_USER) : sudo -u $RUNNER_USER -H bash setup-avds.sh"

    check "Snapshot warm-boot $avd_name" \
        "[[ -d \"$avd_dir/snapshots/default_boot\" ]]" \
        "Le 1er boot ecrit le snapshot (cf setup-avds.sh)"
done

# --- gh CLI (optionnel mais utile pour debug) ---
check "gh CLI present" \
    "command -v gh" \
    "(optionnel) sudo apt install gh ; pour query API depuis la VM"

# --- Runner systemd service ---
SVC=$(systemctl list-unit-files 2>/dev/null | grep -E "actions.runner.*alarm-android" | head -1 | awk '{print $1}')
if [[ -n "$SVC" ]]; then
    check "Service systemd runner ($SVC)" \
        "systemctl is-active --quiet '$SVC'" \
        "sudo systemctl status $SVC ; sudo journalctl -u $SVC -n 50"
else
    RESULTS+=("FAIL|Service systemd runner||Lancer install-runner.sh --token <TOKEN>")
    ((FAIL++))
fi

# --- Docker (pour cluster backend du test) ---
check "Docker daemon running" \
    "docker version --format '{{.Server.Version}}'" \
    "sudo systemctl start docker"

check "User '$RUNNER_USER' dans le groupe docker" \
    "id $RUNNER_USER 2>/dev/null | grep -q docker" \
    "sudo usermod -aG docker $RUNNER_USER"

# --- Affichage ---
echo
printf '======================================================================\n'
printf '  Recap verify.sh - runner Linux Android Alarm2.0\n'
printf '======================================================================\n'
for r in "${RESULTS[@]}"; do
    IFS='|' read -r tag name detail fix <<< "$r"
    if [[ "$tag" == "PASS" ]]; then
        printf '  [PASS] %s\n' "$name"
        [[ -n "$detail" ]] && printf '         %s\n' "$detail"
    else
        printf '  [FAIL] %s\n' "$name"
        [[ -n "$fix" ]] && printf '         fix: %s\n' "$fix"
    fi
done
echo
printf '  Total : %d PASS / %d FAIL\n' "$PASS" "$FAIL"
printf '======================================================================\n'
echo

if [[ $FAIL -eq 0 ]]; then
    echo "Tout est vert. Lance le smoke-test du workflow :"
    echo '  gh workflow run "Tier 4 - Failback E2E (nightly)" --repo claude-murgat/Alarm2.0'
    echo "  gh run watch --repo claude-murgat/Alarm2.0"
    exit 0
else
    echo "Corrige les FAIL ci-dessus puis relance bash verify.sh"
    exit 1
fi
