"""
Test E2E failback : coupe VPS2, verifie que les apps basculent sur VPS1.

Refactor pytest avec fixtures (chantier #21 step 0b — REDO apres le merge
de PR #34 dans une branche stackee qui n'a pas ete propagee a master via
le squash merge de #33).

Hors scope tier 3 actuellement : `--ignore=tests/test_failback.py` dans
pr.yml. Le test orchestre 3 emulateurs Android via ADB, or les runners CI
self-hosted n'ont pas d'emulateurs. Garde manuel / tier 4 nightly tant
que l'infra emulateurs n'est pas mise en place sur les runners.

Prerequis (run manuel) :
- Docker Compose 2-node setup (VPS1 sur :8000, VPS2 sur :8001)
- Au moins 2 emulateurs Android sur 3 (5552/5554/5556) avec l'app installee
- Variables env (toutes optionnelles, defaults sensibles) :
    ADB_PATH (defaut: 'adb' dans le PATH)
    ALARM_REPO_ROOT (defaut: cwd, racine repo Alarm2.0)
    ALARM_VPS1_URL (defaut: http://localhost:8000)
    ALARM_VPS2_URL (defaut: http://localhost:8001)

Run manuel :
    pytest tests/test_failback.py -v -s
"""
import os
import subprocess
import tempfile
import time
import uuid

import pytest
import requests


# --- Configuration via env vars (plus de hardcoded Windows path) ---
ADB = os.environ.get("ADB_PATH", "adb")
CWD = os.environ.get("ALARM_REPO_ROOT", os.getcwd())
VPS1 = os.environ.get("ALARM_VPS1_URL", "http://localhost:8000")
VPS2 = os.environ.get("ALARM_VPS2_URL", "http://localhost:8001")

ALL_EMUS = ["emulator-5552", "emulator-5554", "emulator-5556"]
USERS_CREDS = {
    "emulator-5552": ("user1", "user123"),
    "emulator-5554": ("user2", "user123"),
    "emulator-5556": ("admin", "admin123"),
}
MIN_WORKING_EMUS = 2

# Marqueurs : `failover` (compatible --skip-failover existant)
# + `chaos` (tier 4 nightly, sera promu tier 3 bloquant lorsque l'infra
# emulateurs sera disponible sur les runners self-hosted).
pytestmark = [pytest.mark.failover, pytest.mark.chaos]


# --- Helpers (logique inchangee depuis script original + helpers de #36) ---

def adb(serial, *args):
    cmd = [ADB, "-s", serial] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=15)
    return r.stdout.decode("utf-8", errors="replace")


def login(name, pwd, base=None):
    base = base or VPS1
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
    except Exception:
        return False


def docker(*args):
    """Run docker command depuis le repo root (CWD configurable). Log full stderr
    + exit code a l'erreur (non-tronque) pour simplifier le debug en CI/local."""
    r = subprocess.run(list(args), capture_output=True, timeout=30, cwd=CWD, text=True)
    if r.returncode != 0:
        print(f"  [docker] ERREUR (exit={r.returncode}): {' '.join(args)}")
        print(f"  stderr: {r.stderr}")
    return r.returncode


def wait_healthy(base, timeout=30):
    for _ in range(timeout):
        if healthy(base):
            return True
        time.sleep(1)
    return False


def wait_unhealthy(base, timeout=10):
    """Polling : retourne True quand le backend ne repond plus.
    Substitue les sleep(3) aveugles post-stop par une condition observable."""
    for _ in range(timeout):
        if not healthy(base):
            return True
        time.sleep(1)
    return False


def wait_users_heartbeating(base, expected_count, max_age_seconds=5.0, timeout=20.0):
    """Polling sur /api/test/connected-users-detailed (chantier #21 step 0a).
    Retourne le nombre d'users heartbeating sur `base` depuis < max_age_seconds."""
    deadline = time.time() + timeout
    last_count = 0
    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/api/test/connected-users-detailed", timeout=3)
            users = r.json().get("users", [])
            recent = [
                u for u in users
                if u.get("age_seconds") is not None and u["age_seconds"] < max_age_seconds
            ]
            last_count = len(recent)
            if last_count >= expected_count:
                return last_count
        except Exception:
            pass
        time.sleep(0.5)
    return last_count


def check_emu_network(serial):
    """Verifie que l'emulateur peut joindre 10.0.2.2 (host)."""
    adb(serial, "reverse", "--remove-all")
    adb(serial, "reverse", "tcp:8000", "tcp:8000")
    adb(serial, "reverse", "tcp:8001", "tcp:8001")
    out = adb(serial, "shell", "ping -c 1 -W 2 10.0.2.2")
    return "1 received" in out or "1 packets received" in out


def app_connected(serial):
    xml = get_ui_text(serial)
    return "connexion" in xml.lower() and "ok" in xml.lower()


# --- Fixtures ---

@pytest.fixture(scope="module")
def working_emulators():
    """Emulateurs avec reseau OK. Skip pytest si < MIN_WORKING_EMUS."""
    print("\n[fixture] Pre-flight : check reseau emulateurs...")
    emus = []
    for serial in ALL_EMUS:
        ok = check_emu_network(serial)
        status = "OK" if ok else "RESEAU CASSE"
        print(f"  {serial}: {status}")
        if ok:
            emus.append(serial)

    if len(emus) < MIN_WORKING_EMUS:
        pytest.skip(
            f"Seulement {len(emus)} emulateurs fonctionnels "
            f"(minimum {MIN_WORKING_EMUS})"
        )

    print(f"  {len(emus)} emulateurs fonctionnels: {emus}")
    return emus


@pytest.fixture(scope="module")
def cluster_running():
    """Setup : VPS1 + VPS2 UP et reset. Teardown : VPS2 remis UP si laisse DOWN.

    Idempotent : peut etre lance sur un cluster deja UP ou DOWN. Le `compose
    start` est sans effet si deja running. Garantit le restore de l'etat
    baseline meme si le test fail au milieu (vs script lineaire qui laissait
    VPS2 DOWN au moindre fail)."""
    print("\n[fixture] Ensure both VPS UP...")
    docker("docker", "compose", "start", "backend")
    docker("docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2", "start", "backend")

    assert wait_healthy(VPS1, 30), "VPS1 must be healthy at setup"
    assert wait_healthy(VPS2, 30), "VPS2 must be healthy at setup"

    requests.post(f"{VPS1}/api/test/reset", timeout=5)
    print(f"  VPS1: {requests.get(f'{VPS1}/health', timeout=2).json().get('role', '?')}")
    print(f"  VPS2: {requests.get(f'{VPS2}/health', timeout=2).json().get('role', '?')}")

    yield {"vps1": VPS1, "vps2": VPS2}

    # Teardown : ramener VPS2 si le test l'a laisse DOWN (succes OU echec).
    print("\n[fixture cleanup] Restore cluster baseline state...")
    try:
        requests.post(f"{VPS1}/api/test/reset", timeout=5)
    except Exception:
        pass
    docker("docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2", "start", "backend")
    wait_healthy(VPS2, 30)


@pytest.fixture
def emulators_connected(working_emulators, cluster_running):
    """Apps Android logged in et confirmees heartbeating sur VPS1.

    Utilise wait_users_heartbeating (polling observable sur l'endpoint
    /api/test/connected-users-detailed) pour verifier la connexion
    plutot qu'un sleep(10) aveugle (mini-regle team failover-blocking)."""
    print("\n[fixture] Setup apps...")
    for serial in working_emulators:
        name, pwd = USERS_CREDS[serial]
        adb(serial, "shell", "am force-stop com.alarm.critical")
        time.sleep(1)
        tok, uid, _uname = login(name, pwd)
        inject_prefs(serial, tok, name, uid)
        adb(serial, "shell", "am start -n com.alarm.critical/.MainActivity")
        print(f"  {name} sur {serial}")

    print("  Attente heartbeats (polling observable)...")
    n = wait_users_heartbeating(
        VPS1, expected_count=len(working_emulators),
        max_age_seconds=5.0, timeout=20.0,
    )
    assert n >= len(working_emulators), (
        f"Seulement {n}/{len(working_emulators)} apps heartbeating apres 20s. "
        f"Les apps ne se sont pas connectees au backend."
    )

    return working_emulators


# --- Test ---

def test_failback_vps2_to_vps1(working_emulators, cluster_running, emulators_connected):
    """Couper VPS2 -> les apps doivent failback vers VPS1.

    Scenario complet :
    1. Forcer apps sur VPS2 (couper VPS1 momentanement) -> verif elles tiennent
    2. Remonter VPS1
    3. Couper VPS2 -> verif les apps reviennent sur VPS1 (heartbeats observables)
    """
    # --- [3] Forcer les apps sur VPS2 (couper VPS1) ---
    print("\n[3] Forcer les apps sur VPS2 (couper VPS1)...")
    docker("docker", "compose", "stop", "backend")
    print("  VPS1 stoppe. Attente que les apps basculent sur VPS2 (polling observable)...")
    # On attend que les apps soient detectees heartbeating sur VPS2 (rotation
    # circulaire ApiClient apres N echecs). Plus fiable que sleep(15) aveugle.
    n_hb_vps2 = wait_users_heartbeating(VPS2, expected_count=len(working_emulators),
                                         max_age_seconds=10.0, timeout=25.0)
    print(f"  {n_hb_vps2}/{len(working_emulators)} apps heartbeating sur VPS2")

    for serial in working_emulators:
        ok = app_connected(serial)
        print(f"  {serial} sur VPS2: {'CONNECTE' if ok else 'PAS CONNECTE'}")

    # --- [4] Remonter VPS1 ---
    print("\n[4] Remonter VPS1...")
    docker("docker", "compose", "start", "backend")
    assert wait_healthy(VPS1, 30), "VPS1 must come back"
    print("  VPS1 healthy: OK")
    time.sleep(5)

    # --- [5] LE VRAI TEST : couper VPS2, apps doivent failback vers VPS1 ---
    print("\n[5] === COUPER VPS2 : apps doivent failback vers VPS1 ===")

    requests.post(f"{VPS1}/api/test/send-alarm", timeout=5)
    time.sleep(3)
    print("  Alarme creee")

    # Couper VPS2 avec retry observable (vs sleep aveugle)
    docker("docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2", "stop", "backend")
    vps2_down = wait_unhealthy(VPS2, timeout=10)
    print(f"  VPS2 stoppe: {'OK' if vps2_down else 'ECHEC - ENCORE UP'}")
    if not vps2_down:
        print("  [docker] Retry compose stop (premier essai n'a pas eteint le conteneur)...")
        subprocess.run(
            ["docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2",
             "stop", "backend"],
            timeout=30, cwd=CWD,
        )
        vps2_down = wait_unhealthy(VPS2, timeout=10)
        print(f"  Retry: {'OK' if vps2_down else 'VPS2 REFUSE DE MOURIR'}")
    assert vps2_down, "VPS2 doit etre down pour declencher le failback"

    # Monitoring : on attend que les users soient online sur VPS1 (heartbeat recus).
    # C'est la preuve irrefutable que les apps se sont reconnectees a VPS1.
    print("  Monitoring failback (critere: users online sur VPS1 via API)...")
    expected = len(working_emulators)
    failback_ok = False
    online = 0
    for t in range(0, 60, 5):
        time.sleep(5)
        try:
            status = requests.get(f"{VPS1}/api/test/status", timeout=3).json()
            online = status["connected_users"]
        except Exception:
            online = 0
        vps2_still_down = not healthy(VPS2)
        print(f"  t+{t+5}s: {online} users online sur VPS1 | "
              f"vps2={'DOWN' if vps2_still_down else 'UP!!'}")

        if online >= expected:
            print(f"\n  FAILBACK OK en {t+5}s ! ({online} users reconnectes)")
            failback_ok = True
            break

    if not failback_ok:
        print("\n  ECHEC: failback pas complet apres 60s\n  LOGCAT:")
        for serial in working_emulators:
            print(f"\n  === {serial} ===")
            print(adb(serial, "logcat", "-d", "-t", "50",
                      "-s", "ApiClient:*", "AlarmPollingService:*"))
        pytest.fail(f"Failback incomplet : {online}/{expected} users seulement apres 60s")
