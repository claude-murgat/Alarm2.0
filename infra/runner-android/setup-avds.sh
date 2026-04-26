#!/usr/bin/env bash
# Cree les 3 AVDs (alarm_5552 / alarm_5554 / alarm_5556) requis par
# tests/test_failback.py et ecrit leur snapshot warm-boot.
#
# A executer EN TANT QUE le user runner (`sudo -u ghrunner -H bash setup-avds.sh`),
# pas en root (sinon les AVDs sont crees dans /root/.android/ et invisibles
# du service runner).
#
# Idempotent : skip les AVDs deja presents (sauf --force).
#
# Usage :
#   sudo -u ghrunner -H bash setup-avds.sh
#   sudo -u ghrunner -H bash setup-avds.sh --force

set -euo pipefail

# --- Config ---
ANDROID_HOME="${ANDROID_HOME:-/opt/android-sdk}"
ANDROID_API_LEVEL="30"
ANDROID_TARGET="google_apis"
ANDROID_ARCH="x86_64"
AVD_PORTS=(5552 5554 5556)
SYSTEM_IMAGE="system-images;android-${ANDROID_API_LEVEL};${ANDROID_TARGET};${ANDROID_ARCH}"
DEVICE_PROFILE="pixel_5"
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1; shift;;
        *) echo "Arg inconnu: $1" >&2; exit 1;;
    esac
done

# --- Pre-flight ---
step() { echo; echo "==> $*"; }

step "Pre-flight..."
if [[ ! -d "$ANDROID_HOME" ]]; then
    echo "ERREUR : ANDROID_HOME=$ANDROID_HOME introuvable. Lancer install-runner.sh d'abord." >&2
    exit 1
fi

export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"

for tool in sdkmanager avdmanager emulator adb; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERREUR : '$tool' pas dans PATH. SDK install incomplet ?" >&2
        exit 1
    fi
    echo "  OK $tool : $(command -v $tool)"
done

if [[ ! -e /dev/kvm ]]; then
    echo "WARNING : /dev/kvm absent. Boot AVD en software emulation (~15-25 min par AVD)."
elif [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
    echo "WARNING : /dev/kvm pas R/W pour $(whoami). User dans le groupe kvm ?"
    echo "  groups : $(groups)"
else
    echo "  KVM OK pour $(whoami)"
fi

AVD_HOME="$HOME/.android/avd"
mkdir -p "$AVD_HOME"

# --- Creation AVDs ---
step "Creation AVDs..."
for port in "${AVD_PORTS[@]}"; do
    avd_name="alarm_${port}"
    avd_dir="$AVD_HOME/${avd_name}.avd"

    if [[ -d "$avd_dir" && $FORCE -eq 0 ]]; then
        echo "  $avd_name : deja present, skip (--force pour recreer)"
        continue
    fi
    if [[ -d "$avd_dir" ]]; then
        echo "  $avd_name : --force, suppression de l'existant..."
        avdmanager delete avd -n "$avd_name" 2>/dev/null || true
    fi

    echo "  Creation $avd_name (Pixel 5, API $ANDROID_API_LEVEL, $ANDROID_TARGET, $ANDROID_ARCH)..."
    echo "no" | avdmanager create avd \
        --force \
        --name "$avd_name" \
        --package "$SYSTEM_IMAGE" \
        --device "$DEVICE_PROFILE"

    cfg="$avd_dir/config.ini"
    if [[ -f "$cfg" ]]; then
        # 2 Gio par AVD : ~6 Gio total pour 3 instances
        sed -i 's/^hw\.ramSize=.*/hw.ramSize=2048/' "$cfg" || true
        grep -q "^hw.ramSize=" "$cfg" || echo "hw.ramSize=2048" >> "$cfg"
        sed -i 's/^vm\.heapSize=.*/vm.heapSize=1024/' "$cfg" || true
        grep -q "^vm.heapSize=" "$cfg" || echo "vm.heapSize=1024" >> "$cfg"
        sed -i 's/^disk\.dataPartition\.size=.*/disk.dataPartition.size=4G/' "$cfg" || true
        grep -q "^disk.dataPartition.size=" "$cfg" || echo "disk.dataPartition.size=4G" >> "$cfg"
    fi
    echo "  $avd_name cree"
done

# --- Cold boot + snapshot ---
step "Cold boot initial pour generer snapshot warm-boot..."
adb start-server >/dev/null 2>&1 || true

for port in "${AVD_PORTS[@]}"; do
    avd_name="alarm_${port}"
    serial="emulator-${port}"
    snap="$AVD_HOME/${avd_name}.avd/snapshots/default_boot"

    if [[ -d "$snap" && $FORCE -eq 0 ]]; then
        echo "  $avd_name : snapshot deja present, skip"
        continue
    fi

    echo "  Boot $avd_name port $port (cold, ~3-5 min avec KVM)..."
    log="/tmp/emulator-${port}-bootstrap.log"
    nohup emulator -avd "$avd_name" -port "$port" \
        -no-window -no-audio -no-boot-anim \
        -gpu swiftshader_indirect \
        -accel auto \
        > "$log" 2>&1 &
    EMU_PID=$!

    # wait-for-device + boot_completed
    adb -s "$serial" wait-for-device
    booted=0
    for i in $(seq 1 120); do
        bc=$(adb -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r' || echo "")
        if [[ "$bc" == "1" ]]; then
            # PackageManager ready
            for j in $(seq 1 30); do
                if adb -s "$serial" shell pm path android >/dev/null 2>&1; then
                    booted=1
                    break
                fi
                sleep 2
            done
            booted=1
            break
        fi
        sleep 5
    done
    if [[ $booted -eq 0 ]]; then
        echo "  WARNING: $serial pas boote en 600s, kill"
        adb -s "$serial" emu kill 2>/dev/null || true
        kill "$EMU_PID" 2>/dev/null || true
        continue
    fi

    echo "  $serial : booted, desactivation animations..."
    adb -s "$serial" shell settings put global window_animation_scale 0 2>/dev/null || true
    adb -s "$serial" shell settings put global transition_animation_scale 0 2>/dev/null || true
    adb -s "$serial" shell settings put global animator_duration_scale 0 2>/dev/null || true

    echo "  $serial : kill propre pour ecrire snapshot..."
    adb -s "$serial" emu avd snapshot save default_boot 2>/dev/null || true
    sleep 2
    adb -s "$serial" emu kill 2>/dev/null || true

    # Wait que le process emulator termine (libere le port + flush snapshot)
    for i in $(seq 1 30); do
        if ! kill -0 "$EMU_PID" 2>/dev/null; then break; fi
        sleep 1
    done
    if kill -0 "$EMU_PID" 2>/dev/null; then
        echo "  WARNING: emulator pas termine en 30s, kill -9"
        kill -9 "$EMU_PID" 2>/dev/null || true
    fi
    echo "  $avd_name : snapshot ecrit"
done

# --- Recap ---
step "Recap"
for port in "${AVD_PORTS[@]}"; do
    avd_name="alarm_${port}"
    snap="$AVD_HOME/${avd_name}.avd/snapshots/default_boot"
    if [[ -d "$snap" ]]; then
        size=$(du -sm "$snap" 2>/dev/null | cut -f1)
        echo "  $avd_name : OK snapshot (${size} Mo)"
    else
        echo "  $avd_name : KO pas de snapshot — boot suivants en cold (~3-5 min)"
    fi
done

echo ""
echo "AVDs prets. Prochaine etape : bash verify.sh"
