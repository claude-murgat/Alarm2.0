"""
Test isole de failback : coupe VPS2, verifie que les apps basculent sur VPS1.
Requiert au minimum 2 emulateurs fonctionnels sur 3.
"""

import os
import io
import json
import subprocess
import sys
import tempfile
import time
import uuid
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ADB = r"C:\Users\Charles\Android\Sdk\platform-tools\adb.exe"
ALL_EMUS = ["emulator-5552", "emulator-5554", "emulator-5556"]
USERS_CREDS = {
    "emulator-5552": ("user1", "user123"),
    "emulator-5554": ("user2", "user123"),
    "emulator-5556": ("admin", "admin123"),
}
VPS1 = "http://localhost:8000"
VPS2 = "http://localhost:8001"
CWD = r"C:\Users\Charles\Desktop\Projet Claude\Alarm2.0"
MIN_WORKING_EMUS = 2


def adb(serial, *args):
    cmd = [ADB, "-s", serial] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=15)
    return r.stdout.decode("utf-8", errors="replace")


def login(name, pwd, base=VPS1):
    r = requests.post(f"{base}/api/auth/login", json={"name": name, "password": pwd}, timeout=5)
    r.raise_for_status()
    d = r.json()
    return d["access_token"], d["user"]["id"], d["user"]["name"]


def inject_prefs(serial, token, name, uid):
    xml = f"""<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="token">{token}</string>
    <string name="user_name">{name}</string>
    <int name="user_id" value="{uid}" />
    <string name="device_token">{uuid.uuid4()}</string>
</map>"""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
    tmp.write(xml); tmp.close()
    adb(serial, "push", tmp.name, "/data/local/tmp/alarm_prefs.xml")
    adb(serial, "shell", "run-as com.alarm.critical mkdir -p shared_prefs")
    adb(serial, "shell", "run-as com.alarm.critical cp /data/local/tmp/alarm_prefs.xml shared_prefs/alarm_prefs.xml")
    adb(serial, "shell", "rm /data/local/tmp/alarm_prefs.xml")
    os.unlink(tmp.name)


def get_ui_text(serial):
    adb(serial, "shell", "uiautomator dump /sdcard/window_dump.xml")
    return adb(serial, "shell", "cat /sdcard/window_dump.xml")


def healthy(base):
    try:
        return requests.get(f"{base}/health", timeout=2).status_code == 200
    except:
        return False


def docker(*args):
    r = subprocess.run(list(args), capture_output=True, timeout=30, cwd=CWD, text=True)
    if r.returncode != 0:
        print(f"  [docker] ERREUR: {' '.join(args)}")
        print(f"  stderr: {r.stderr[:200]}")
    return r.returncode


def wait_healthy(base, timeout=30):
    for _ in range(timeout):
        if healthy(base):
            return True
        time.sleep(1)
    return False


def check_emu_network(serial):
    """Verifie que l'emulateur peut joindre 10.0.2.2 (host)."""
    adb(serial, "reverse", "--remove-all")
    adb(serial, "reverse", "tcp:8000", "tcp:8000")
    adb(serial, "reverse", "tcp:8001", "tcp:8001")
    # Test ping vers le gateway emulateur
    out = adb(serial, "shell", "ping -c 1 -W 2 10.0.2.2")
    return "1 received" in out or "1 packets received" in out


def app_shows_alarm(serial):
    xml = get_ui_text(serial)
    return "test alarm" in xml.lower() or "alarme active" in xml.lower()


def app_connected(serial):
    xml = get_ui_text(serial)
    return "connexion" in xml.lower() and "ok" in xml.lower()


# ====================================================================
print("=" * 60)
print("TEST FAILBACK VPS2 -> VPS1")
print("=" * 60)

# --- [0] Pre-flight : check emulators network ---
print("\n[0] Pre-flight : check reseau emulateurs...")
working_emus = []
for serial in ALL_EMUS:
    ok = check_emu_network(serial)
    status = "OK" if ok else "RESEAU CASSE"
    print(f"  {serial}: {status}")
    if ok:
        working_emus.append(serial)

if len(working_emus) < MIN_WORKING_EMUS:
    print(f"\n  ABANDON: seulement {len(working_emus)} emulateurs fonctionnels (minimum {MIN_WORKING_EMUS})")
    sys.exit(2)

print(f"  {len(working_emus)} emulateurs fonctionnels: {working_emus}")

# --- [1] Ensure both VPS UP ---
print("\n[1] Ensure both VPS UP...")
docker("docker", "compose", "start", "backend")
docker("docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2", "start", "backend")
assert wait_healthy(VPS1, 30), "VPS1 must be healthy"
assert wait_healthy(VPS2, 30), "VPS2 must be healthy"
requests.post(f"{VPS1}/api/test/reset", timeout=5)
print(f"  VPS1: {requests.get(f'{VPS1}/health', timeout=2).json()['role']}")
print(f"  VPS2: {requests.get(f'{VPS2}/health', timeout=2).json()['role']}")

# --- [2] Setup apps ---
print("\n[2] Setup apps...")
for serial in working_emus:
    name, pwd = USERS_CREDS[serial]
    adb(serial, "shell", "am force-stop com.alarm.critical")
    time.sleep(1)
    tok, uid, uname = login(name, pwd)
    inject_prefs(serial, tok, name, uid)
    adb(serial, "shell", "am start -n com.alarm.critical/.MainActivity")
    print(f"  {name} sur {serial}")

print("  Attente 10s (heartbeats)...")
time.sleep(10)

for serial in working_emus:
    ok = app_connected(serial)
    print(f"  {serial}: {'CONNECTE' if ok else 'PAS CONNECTE'}")

# --- [3] Forcer les apps sur VPS2 (couper VPS1) ---
print("\n[3] Forcer les apps sur VPS2 (couper VPS1)...")
docker("docker", "compose", "stop", "backend")
print("  VPS1 stoppe. Attente 15s...")
time.sleep(15)

for serial in working_emus:
    ok = app_connected(serial)
    print(f"  {serial} sur VPS2: {'CONNECTE' if ok else 'PAS CONNECTE'}")

# --- [4] Remonter VPS1 ---
print("\n[4] Remonter VPS1...")
docker("docker", "compose", "start", "backend")
assert wait_healthy(VPS1, 30), "VPS1 must come back"
print(f"  VPS1 healthy: OK")
time.sleep(5)

# --- [5] LE VRAI TEST : couper VPS2, apps doivent failback vers VPS1 ---
print("\n[5] === COUPER VPS2 : apps doivent failback vers VPS1 ===")

# Creer alarme
requests.post(f"{VPS1}/api/test/send-alarm", timeout=5)
time.sleep(3)
print("  Alarme creee")

# Couper VPS2
docker("docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2", "stop", "backend")
time.sleep(3)
vps2_down = not healthy(VPS2)
print(f"  VPS2 stoppe: {'OK' if vps2_down else 'ECHEC - ENCORE UP'}")
if not vps2_down:
    # Retry
    subprocess.run(["docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2",
                    "stop", "backend"], timeout=30, cwd=CWD)
    time.sleep(3)
    vps2_down = not healthy(VPS2)
    print(f"  Retry: {'OK' if vps2_down else 'VPS2 REFUSE DE MOURIR'}")
assert vps2_down, "VPS2 doit etre down"

# Monitoring : on attend que les users soient online sur VPS1 (heartbeat recus)
# C'est la preuve irrefutable que les apps se sont reconnectees a VPS1
print("  Monitoring failback (critere: users online sur VPS1 via API)...")
for t in range(0, 60, 5):
    time.sleep(5)
    try:
        status = requests.get(f"{VPS1}/api/test/status", timeout=3).json()
        online = status["connected_users"]
    except:
        online = 0
    vps2_still_down = not healthy(VPS2)
    print(f"  t+{t+5}s: {online} users online sur VPS1 | vps2={'DOWN' if vps2_still_down else 'UP!!'}")

    if online >= len(working_emus):
        print(f"\n  FAILBACK OK en {t+5}s ! ({online} users reconnectes)")
        break
else:
    print(f"\n  ECHEC: failback pas complet apres 60s")
    print("\n  LOGCAT:")
    for serial in working_emus:
        print(f"\n  === {serial} ===")
        print(adb(serial, "logcat", "-d", "-t", "50", "-s", "ApiClient:*", "AlarmPollingService:*"))
    docker("docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2", "start", "backend")
    requests.post(f"{VPS1}/api/test/reset", timeout=5)
    sys.exit(1)

# Cleanup
print("\n[6] Cleanup...")
requests.post(f"{VPS1}/api/test/reset", timeout=5)
docker("docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2", "start", "backend")

print("\n" + "=" * 60)
print("TEST PASSE")
print("=" * 60)
