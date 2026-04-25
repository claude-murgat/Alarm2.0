"""
Test E2E failback : coupe VPS2, verifie que les apps basculent sur VPS1.

Refactor pytest avec fixtures (chantier #21 step 0b). Hors scope tier 3 actuellement
(`--ignore=tests/test_failback.py` dans pr.yml) — sera promu en tier 3 bloquant
en step 4 du chantier #21.

Prerequis :
- Docker Compose 2-node setup (VPS1 sur :8000, VPS2 sur :8001)
- Au moins 2 emulateurs Android sur 3 (5552/5554/5556) avec l'app installee
- Variables env (toutes optionnelles) :
    ADB_PATH (defaut: 'adb' dans le PATH)
    ALARM_REPO_ROOT (defaut: cwd, racine du repo Alarm2.0)
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

# Note : pas de re-wrap de sys.stdout / sys.stderr ici (le script original le faisait
# pour gerer Windows console). En pytest, c'est `pytest` qui gere la capture stdout
# et notre reassignation cassait `--collect-only` (I/O on closed file). pytest passe
# `-s` pour print direct si besoin, et utilise PYTHONIOENCODING=utf-8 sinon.


# --- Configuration via env vars ---
# Ne plus hardcoder le path ADB ni la racine du repo : portable a tout poste.
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

# Marqueurs : `failover` (manipule Docker stop/start, ciblable via --skip-failover)
# + `chaos` (tier 4 nightly, sera promu tier 3 bloquant en step 4 du chantier #21).
pytestmark = [pytest.mark.failover, pytest.mark.chaos]


# --- Helpers (logique inchangee depuis script original) ---

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
    """Run docker command depuis le repo root (CWD configurable). Log status code +
    full stderr a l'erreur (pas tronque) pour simplifier le debug en CI."""
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


def wait_users_heartbeating(base, expected_count, max_age_seconds=5.0, timeout=15.0):
    """Polling sur /api/test/connected-users-detailed (chantier #21 step 0a) pour
    attendre que `expected_count` users aient heartbeate il y a moins de
    `max_age_seconds` secondes.

    Remplace les `time.sleep(N)` aveugles : on connait l'instant exact ou les apps
    sont effectivement connectees, sans devoir hardcoder un delai majorant.

    Retourne le nombre d'users heartbeating au moment du return (== expected_count
    en cas de succes, < en cas de timeout).
    """
    deadline = time.time() + timeout
    last_count = 0
    while time.time() < deadline:
        try:
            r = requests.get(f"{base}/api/test/connected-users-detailed", timeout=3)
            users = r.json()["users"]
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


# --- Fixtures ---

@pytest.fixture(scope="module")
def working_emulators():
    """Emulateurs avec reseau OK. Skip si < MIN_WORKING_EMUS."""
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

    Idempotent : peut etre lance sur un cluster deja UP ou DOWN. Le `compose start`
    est sans effet si deja running."""
    print("\n[fixture] Ensure both VPS UP...")
    docker("docker", "compose", "start", "backend")
    docker("docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2", "start", "backend")

    assert wait_healthy(VPS1, 30), "VPS1 must be healthy at setup"
    assert wait_healthy(VPS2, 30), "VPS2 must be healthy at setup"

    requests.post(f"{VPS1}/api/test/reset", timeout=5)
    print(f"  VPS1: {requests.get(f'{VPS1}/health', timeout=2).json().get('role', '?')}")
    print(f"  VPS2: {requests.get(f'{VPS2}/health', timeout=2).json().get('role', '?')}")

    yield {"vps1": VPS1, "vps2": VPS2}

    # Teardown : ramener VPS2 si le test l'a laisse DOWN (succes ou echec).
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

    Utilise /api/test/connected-users-detailed pour confirmer la heartbeat
    plutot que `time.sleep(10)` aveugle (cf chantier #21 step 0a)."""
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
    print("  VPS1 stoppe. Attente 15s...")
    time.sleep(15)

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

    # Couper VPS2 (avec 1 retry si refuse)
    docker("docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2", "stop", "backend")
    time.sleep(3)
    vps2_down = not healthy(VPS2)
    print(f"  VPS2 stoppe: {'OK' if vps2_down else 'ECHEC - ENCORE UP'}")
    if not vps2_down:
        subprocess.run(
            ["docker", "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2",
             "stop", "backend"],
            timeout=30, cwd=CWD,
        )
        time.sleep(3)
        vps2_down = not healthy(VPS2)
        print(f"  Retry: {'OK' if vps2_down else 'VPS2 REFUSE DE MOURIR'}")
    assert vps2_down, "VPS2 doit etre down pour declencher le failback"

    # Monitoring : on attend que les users soient online sur VPS1 (heartbeat recus).
    # C'est la preuve irrefutable que les apps se sont reconnectees a VPS1.
    print("  Monitoring failback (critere: users online sur VPS1 via API)...")
    expected = len(working_emulators)
    failback_ok = False
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
