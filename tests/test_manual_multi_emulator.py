"""
Test multi-emulateur avec failover HA (3 noeuds Patroni + etcd)

Prerequis :
  - 3 instances Docker Compose demarrees :
      docker compose --env-file .env.node1 -p node1 up --build -d
      docker compose --env-file .env.node2 -p node2 up --build -d
      docker compose --env-file .env.node3 -p node3 up --build -d
  - 3 emulateurs demarres (ports 5552, 5554, 5556)
  - APK debug installe sur les 3
  - adb reverse tcp:8000/8001/8002 configure

Usage :
  python tests/test_manual_multi_emulator.py
"""

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Configuration — 3 noeuds
# ---------------------------------------------------------------------------
NODES = {
    "node1": {"backend": "http://localhost:8000", "patroni": "http://localhost:8008", "project": "node1"},
    "node2": {"backend": "http://localhost:8001", "patroni": "http://localhost:8009", "project": "node2"},
    "node3": {"backend": "http://localhost:8002", "patroni": "http://localhost:8010", "project": "node3"},
}

USERS = {
    "user1": {"name": "user1", "password": "user123"},
    "user2": {"name": "user2", "password": "user123"},
    "admin": {"name": "admin", "password": "admin123"},
}

APP_PACKAGE = "com.alarm.critical"
ADB_PATH = os.environ.get("ADB_PATH", r"C:\Users\Charles\Android\Sdk\platform-tools\adb.exe")
CWD = r"C:\Users\Charles\Desktop\Projet Claude\Alarm2.0"
REPORT_DIR = os.path.join(CWD, "tests", "reports")


@dataclass
class EmulatorContext:
    serial: str
    user: str
    token: str = ""


# ---------------------------------------------------------------------------
# Diagnostic — collecte d'etat pour analyse post-mortem
# ---------------------------------------------------------------------------

class DiagnosticCollector:
    """Collecte l'etat du systeme a chaque phase et en cas d'echec.
    Ecrit un rapport dans tests/reports/report_YYYYMMDD_HHMMSS.txt"""

    def __init__(self, emulators=None):
        self.emulators = emulators or []
        self.phases = []
        self.start_time = time.time()
        os.makedirs(REPORT_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.report_path = os.path.join(REPORT_DIR, f"report_{ts}.txt")
        self.lines = []

    def _log(self, msg):
        elapsed = time.time() - self.start_time
        line = f"[{elapsed:7.1f}s] {msg}"
        self.lines.append(line)

    def snapshot(self, label):
        """Capture un snapshot complet du systeme."""
        self._log(f"=== SNAPSHOT: {label} ===")

        # Etat des 3 backends
        for name, node in NODES.items():
            try:
                r = requests.get(f"{node['backend']}/health", timeout=2)
                health = r.json()
                self._log(f"  {name}: role={health.get('role')} db={health.get('db')} loop={health.get('escalation_loop')} status={health.get('status')}")
            except Exception as e:
                self._log(f"  {name}: DOWN ({e.__class__.__name__})")

        # Cluster Patroni (depuis le premier backend qui repond)
        for name, node in NODES.items():
            try:
                r = requests.get(f"{node['backend']}/api/cluster", timeout=2)
                cluster = r.json()
                q = cluster.get("quorum", {})
                self._log(f"  Quorum: {q.get('healthy')}/{q.get('total')} has_quorum={q.get('has_quorum')}")
                for m in cluster.get("members", []):
                    lag = m.get("lag", "-")
                    self._log(f"    {m['name']}: role={m['role']} state={m['state']} lag={lag}")
                break
            except Exception:
                continue

        # Connected users (depuis le primary)
        try:
            primary = find_primary_backend()
            status = requests.get(f"{primary}/api/test/status", timeout=2).json()
            self._log(f"  Connected users: {status.get('connected_users')}/{status.get('users')}")
            self._log(f"  Alarms: active={status['alarms']['active']} ack={status['alarms']['acknowledged']}")
        except Exception:
            self._log(f"  Status: unavailable")

        # Docker containers
        try:
            r = subprocess.run(["docker", "ps", "--format", "{{.Names}} {{.Status}}"],
                              capture_output=True, text=True, timeout=5)
            for line in sorted(r.stdout.strip().splitlines()):
                if line.startswith("node"):
                    self._log(f"  container: {line}")
        except Exception:
            pass

        self._log("")

    def snapshot_emulators(self, label):
        """Capture le logcat et l'etat UI des emulateurs."""
        self._log(f"=== EMULATORS: {label} ===")
        for serial in self.emulators:
            self._log(f"  --- {serial} ---")
            # Logcat ApiClient + AlarmPollingService (30 dernieres lignes)
            try:
                cmd = [ADB_PATH, "-s", serial, "logcat", "-d", "-t", "30",
                       "-s", "ApiClient:*", "AlarmPollingService:*"]
                r = subprocess.run(cmd, capture_output=True, timeout=10)
                logcat = r.stdout.decode("utf-8", errors="replace").strip()
                for line in logcat.splitlines()[-15:]:
                    self._log(f"    {line.strip()}")
            except Exception as e:
                self._log(f"    logcat error: {e}")

            # UI state
            try:
                cmd = [ADB_PATH, "-s", serial, "shell", "uiautomator dump /sdcard/window_dump.xml"]
                subprocess.run(cmd, capture_output=True, timeout=10)
                cmd = [ADB_PATH, "-s", serial, "shell", "cat /sdcard/window_dump.xml"]
                r = subprocess.run(cmd, capture_output=True, timeout=10)
                xml = r.stdout.decode("utf-8", errors="replace")
                # Extraire les textes visibles
                import re
                texts = re.findall(r'text="([^"]+)"', xml)
                texts = [t for t in texts if t.strip()]
                self._log(f"    UI texts: {texts[:10]}")
            except Exception as e:
                self._log(f"    UI error: {e}")

        self._log("")

    def backend_logs(self, label, since_seconds=30):
        """Capture les logs Docker des 3 backends."""
        self._log(f"=== BACKEND LOGS: {label} (last {since_seconds}s) ===")
        for name in NODES:
            project = NODES[name]["project"]
            try:
                cmd = ["docker", "compose", "-p", project, "logs",
                       f"--since={since_seconds}s", "--tail=30", "backend"]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=CWD)
                lines = r.stdout.strip().splitlines()
                # Garder les lignes pertinentes (EVENT, ERROR, WARNING, escalat, heartbeat)
                relevant = [l for l in lines if any(kw in l.lower() for kw in
                           ["event", "error", "warn", "escalat", "heartbeat", "leader", "primary", "replica"])]
                if relevant:
                    self._log(f"  --- {name} ---")
                    for line in relevant[-10:]:
                        self._log(f"    {line.strip()[:200]}")
            except Exception:
                pass
        self._log("")

    def on_phase_start(self, phase_name):
        """Appeler au debut de chaque phase."""
        self.snapshot(f"BEFORE {phase_name}")

    def on_phase_end(self, phase_name):
        """Appeler a la fin de chaque phase."""
        self.phases.append({"name": phase_name, "status": "PASS"})

    def on_failure(self, phase_name, error):
        """Appeler en cas d'echec — capture maximale."""
        self.phases.append({"name": phase_name, "status": "FAIL", "error": str(error)})
        self._log(f"!!! FAILURE in {phase_name}: {error} !!!")
        self.snapshot(f"FAILURE {phase_name}")
        self.backend_logs(f"FAILURE {phase_name}", since_seconds=60)
        if self.emulators:
            self.snapshot_emulators(f"FAILURE {phase_name}")

    def write_report(self):
        """Ecrit le rapport final."""
        with open(self.report_path, "w", encoding="utf-8") as f:
            f.write(f"Test Report - {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Duration: {time.time() - self.start_time:.1f}s\n")
            f.write(f"{'=' * 70}\n\n")

            # Resume des phases
            f.write("PHASES:\n")
            for p in self.phases:
                status = "PASS" if p["status"] == "PASS" else f"FAIL: {p.get('error', '?')}"
                f.write(f"  {p['name']}: {status}\n")
            f.write(f"\n{'=' * 70}\n\n")

            # Logs detailles
            f.write("DETAILED LOG:\n")
            for line in self.lines:
                f.write(f"{line}\n")

        print(f"\n  Rapport: {self.report_path}")
        return self.report_path


# ---------------------------------------------------------------------------
# Helpers — Backend API
# ---------------------------------------------------------------------------

def api_post(path, base=None, token=None, json_body=None):
    if base is None:
        base = find_primary_backend()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.post(f"{base}{path}", headers=headers, json=json_body, timeout=10)


def api_get(path, base=None, token=None):
    if base is None:
        base = find_primary_backend()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get(f"{base}{path}", headers=headers, timeout=10)


def find_primary_backend():
    """Trouve le backend du noeud primary parmi les 3."""
    for name, node in NODES.items():
        try:
            r = requests.get(f"{node['backend']}/health", timeout=2)
            if r.status_code == 200 and r.json().get("role") == "primary":
                return node["backend"]
        except Exception:
            continue
    # Fallback : retourner le premier qui repond
    for name, node in NODES.items():
        try:
            r = requests.get(f"{node['backend']}/health", timeout=2)
            if r.status_code == 200:
                return node["backend"]
        except Exception:
            continue
    raise TestAssertionError("Aucun backend accessible")


def find_primary_node():
    """Retourne le nom du noeud primary (node1/node2/node3)."""
    for name, node in NODES.items():
        try:
            r = requests.get(f"{node['backend']}/health", timeout=2)
            if r.status_code == 200 and r.json().get("role") == "primary":
                return name
        except Exception:
            continue
    return None


def login(username, password, base=None):
    if base is None:
        base = find_primary_backend()
    r = requests.post(f"{base}/api/auth/login",
                      json={"name": username, "password": password}, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["access_token"], data["user"]["id"], data["user"]["name"]


def reset_backend(base=None):
    if base is None:
        base = find_primary_backend()
    api_post("/api/test/reset", base=base)
    api_post("/api/test/reset-clock", base=base)
    api_post("/api/test/reset-sms-queue", base=base)


def get_active_alarms(base=None, token=None):
    r = api_get("/api/alarms/active", base=base, token=token)
    r.raise_for_status()
    return r.json()


def get_my_alarms(token, base=None):
    r = api_get("/api/alarms/mine", base=base, token=token)
    r.raise_for_status()
    return r.json()


def get_status(base=None):
    r = api_get("/api/test/status", base=base)
    r.raise_for_status()
    return r.json()


def send_alarm(base=None):
    r = api_post("/api/test/send-alarm", base=base)
    r.raise_for_status()
    return r.json()


def resolve_alarm(alarm_id, token, base=None):
    r = api_post(f"/api/alarms/{alarm_id}/resolve", base=base, token=token)
    r.raise_for_status()
    return r.json()


def trigger_escalation(base=None):
    r = api_post("/api/test/trigger-escalation", base=base)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Helpers — Node control (docker compose par projet)
# ---------------------------------------------------------------------------

def node_healthy(node_name):
    base = NODES[node_name]["backend"]
    try:
        r = requests.get(f"{base}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def node_role(node_name):
    base = NODES[node_name]["backend"]
    try:
        r = requests.get(f"{base}/health", timeout=2)
        if r.status_code == 200:
            return r.json().get("role", "unknown")
    except Exception:
        pass
    return "down"


def stop_node(node_name):
    """Arrete TOUT le noeud (patroni + backend + etcd) — simule une panne VPS."""
    project = NODES[node_name]["project"]
    subprocess.run(["docker", "compose", "-p", project, "stop"],
                   capture_output=True, timeout=30, cwd=CWD)
    print(f"  [{node_name}] Tout stoppe (patroni + DB + backend + etcd)")


def start_node(node_name):
    """Redemarre le noeud."""
    project = NODES[node_name]["project"]
    subprocess.run(["docker", "compose", "-p", project, "start"],
                   capture_output=True, timeout=30, cwd=CWD)
    print(f"  [{node_name}] Redemarre")


def wait_node_healthy(node_name, timeout=60):
    for _ in range(timeout):
        if node_healthy(node_name):
            return True
        time.sleep(1)
    return False


def wait_new_primary(excluded_node=None, timeout=60):
    """Attend qu'un noeud (autre que excluded_node) devienne primary."""
    for t in range(timeout):
        for name in NODES:
            if name == excluded_node:
                continue
            if node_role(name) == "primary":
                return name
        time.sleep(1)
    return None


def get_node_events(node_name, since_seconds=30, event_type=None):
    """Parse les [EVENT] des logs d'un noeud specifique."""
    project = NODES[node_name]["project"]
    cmd = ["docker", "compose", "-p", project, "logs",
           f"--since={since_seconds}s", "backend"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, cwd=CWD)
    events = []
    for line in result.stdout.splitlines():
        if "[EVENT]" not in line:
            continue
        json_str = line.split("[EVENT]", 1)[1].strip()
        try:
            event = json.loads(json_str)
            if event_type is None or event.get("type") == event_type:
                events.append(event)
        except json.JSONDecodeError:
            continue
    return events


# ---------------------------------------------------------------------------
# Helpers — ADB / Emulator
# ---------------------------------------------------------------------------

def adb(serial, *args):
    cmd = [ADB_PATH, "-s", serial] + list(args)
    result = subprocess.run(cmd, capture_output=True, timeout=15)
    stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
    stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
    return stdout, stderr, result.returncode


def get_ui_xml(serial):
    adb(serial, "shell", "uiautomator dump /sdcard/window_dump.xml")
    stdout, _, _ = adb(serial, "shell", "cat /sdcard/window_dump.xml")
    return stdout or ""


def launch_app(serial):
    adb(serial, "shell", f"am start -n {APP_PACKAGE}/.MainActivity")
    time.sleep(2)


def force_stop_app(serial):
    adb(serial, "shell", f"am force-stop {APP_PACKAGE}")


def inject_shared_prefs(serial, token, user_name, user_id):
    device_token = str(uuid.uuid4())
    xml_content = f"""<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="token">{token}</string>
    <string name="user_name">{user_name}</string>
    <int name="user_id" value="{user_id}" />
    <string name="device_token">{device_token}</string>
</map>"""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
    tmp.write(xml_content)
    tmp.close()
    try:
        adb(serial, "push", tmp.name, "/data/local/tmp/alarm_prefs.xml")
        adb(serial, "shell", f"run-as {APP_PACKAGE} mkdir -p shared_prefs")
        adb(serial, "shell",
            f"run-as {APP_PACKAGE} cp /data/local/tmp/alarm_prefs.xml shared_prefs/alarm_prefs.xml")
        adb(serial, "shell", "rm /data/local/tmp/alarm_prefs.xml")
    finally:
        os.unlink(tmp.name)


def setup_emulators_parallel(emus, tokens, user_ids):
    def _setup(name, ctx):
        force_stop_app(ctx.serial)
        inject_shared_prefs(ctx.serial, tokens[name], name, user_ids[name])
        launch_app(ctx.serial)
        print(f"  {name} sur {ctx.serial}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_setup, n, c): n for n, c in emus.items()}
        for f in as_completed(futures):
            f.result()


def assert_user_sees_alarm_on_ui(serial, timeout=10):
    for _ in range(timeout):
        xml = get_ui_xml(serial)
        if "test alarm" in xml.lower() or "alarme" in xml.lower():
            print(f"  OK: Alarme visible sur {serial}")
            return True
        time.sleep(1)
    raise TestAssertionError(f"ECHEC: Alarme non visible sur {serial} apres {timeout}s")


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

class TestAssertionError(Exception):
    pass


def assert_true(condition, msg):
    if not condition:
        raise TestAssertionError(f"ECHEC: {msg}")
    print(f"  OK: {msg}")


def assert_alarm_active(token, base=None, should_exist=True):
    alarms = get_my_alarms(token, base)
    active = [a for a in alarms if a["status"] in ("active", "escalated")]
    if should_exist:
        assert_true(len(active) > 0, "Alarme active visible pour cet utilisateur")
    else:
        assert_true(len(active) == 0, "Aucune alarme active pour cet utilisateur")
    return active


# ---------------------------------------------------------------------------
# Scenario principal — 3 noeuds HA
# ---------------------------------------------------------------------------

def run_scenario(emu1_serial, emu2_serial, emu3_serial):
    print("=" * 70)
    print("TEST HA 3 NOEUDS — FAILOVER COMPLET (DB + BACKEND)")
    print("=" * 70)

    diag = DiagnosticCollector(emulators=[emu1_serial, emu2_serial, emu3_serial])
    # current_phase est mis a jour dans _run_phases via diag._current_phase
    diag._current_phase = "INIT"

    try:
        return _run_phases(emu1_serial, emu2_serial, emu3_serial, diag)
    except TestAssertionError as e:
        diag.on_failure(diag._current_phase, e)
        diag.write_report()
        raise
    except Exception as e:
        diag.on_failure(diag._current_phase, e)
        diag.write_report()
        raise


def _run_phases(emu1_serial, emu2_serial, emu3_serial, diag):
    diag._current_phase = "PHASE 0"

    # ------------------------------------------------------------------
    # PHASE 0 : Verifier que les 3 noeuds sont up
    # ------------------------------------------------------------------
    print("\n--- PHASE 0 : Verification cluster ---")
    diag.on_phase_start("PHASE 0: Verification cluster")
    for name in NODES:
        ok = wait_node_healthy(name, timeout=30)
        role = node_role(name) if ok else "DOWN"
        assert_true(ok, f"{name} healthy (role={role})")

    primary = find_primary_node()
    assert_true(primary is not None, f"Un primary existe: {primary}")
    primary_url = NODES[primary]["backend"]

    # Verifier replication : les 3 noeuds repondent aux GET
    for name, node in NODES.items():
        try:
            r = requests.get(f"{node['backend']}/health", timeout=2)
            assert_true(r.status_code == 200, f"{name} repond ({r.json().get('role')})")
        except Exception as e:
            assert_true(False, f"{name} repond: {e}")

    # Reset via le primary
    reset_backend(base=primary_url)

    # Login + setup emulateurs
    tokens = {}
    user_ids = {}
    for uname, creds in USERS.items():
        token, uid, _ = login(creds["name"], creds["password"], base=primary_url)
        tokens[uname] = token
        user_ids[uname] = uid
        print(f"  {uname} (id={uid})")

    emus = {
        "user1": EmulatorContext(serial=emu1_serial, user="user1"),
        "user2": EmulatorContext(serial=emu2_serial, user="user2"),
        "admin": EmulatorContext(serial=emu3_serial, user="admin"),
    }

    print("  Setup emulateurs...")
    setup_emulators_parallel(emus, tokens, user_ids)
    print("  Attente heartbeats (8s)...")
    time.sleep(8)

    status = get_status(base=primary_url)
    assert_true(status["connected_users"] >= 3,
                f"3+ users connectes ({status['connected_users']})")

    # ------------------------------------------------------------------
    # PHASE 1 : Alarme + replication sur les 3 noeuds
    # ------------------------------------------------------------------
    diag.on_phase_end("PHASE 0")

    diag._current_phase = "PHASE 1"
    print("\n--- PHASE 1 : Alarme + verification replication ---")
    diag.on_phase_start("PHASE 1: Alarme + replication")

    alarm_data = send_alarm(base=primary_url)
    alarm_id = alarm_data["alarm_id"]
    print(f"  Alarme creee id={alarm_id} sur {primary}")

    time.sleep(3)

    # Tous les noeuds doivent voir l'alarme (replication streaming)
    for name, node in NODES.items():
        try:
            alarms = requests.get(f"{node['backend']}/api/alarms/active", timeout=5).json()
            has_alarm = any(a["id"] == alarm_id for a in alarms)
            assert_true(has_alarm, f"{name} voit l'alarme {alarm_id} (replication OK)")
        except Exception as e:
            assert_true(False, f"{name} replication: {e}")

    # user1 voit l'alarme
    assert_alarm_active(tokens["user1"], base=primary_url)
    diag.on_phase_end("PHASE 1")
    print("  Phase 1 OK")

    # ------------------------------------------------------------------
    # PHASE 2 : Tuer le PRIMARY ENTIER (DB + backend + etcd)
    # ------------------------------------------------------------------
    diag._current_phase = "PHASE 2"
    print(f"\n--- PHASE 2 : Tuer le primary ({primary}) ---")
    diag.on_phase_start("PHASE 2: Kill primary")

    stop_node(primary)
    # Attendre que le noeud soit vraiment down
    for _ in range(15):
        if not node_healthy(primary):
            break
        time.sleep(1)
    assert_true(not node_healthy(primary), f"{primary} est DOWN")

    # Un nouveau primary doit emerger
    print("  Attente nouveau primary...")
    new_primary = wait_new_primary(excluded_node=primary, timeout=60)
    assert_true(new_primary is not None, f"Nouveau primary: {new_primary}")

    new_primary_url = NODES[new_primary]["backend"]
    print(f"  {new_primary} est le nouveau primary")

    # L'alarme doit persister
    time.sleep(3)
    alarms = get_active_alarms(base=new_primary_url, token=tokens["admin"])
    assert_true(len(alarms) > 0, "Alarme persiste apres failover")
    assert_true(alarms[0]["id"] == alarm_id,
                f"Meme alarme id={alarm_id} (replication OK)")

    # Le nouveau primary accepte les ecritures
    resolve_alarm(alarm_id, tokens["user1"], base=new_primary_url)
    print("  Ecriture OK sur le nouveau primary (alarme resolue)")

    # Creer une nouvelle alarme
    alarm2 = send_alarm(base=new_primary_url)
    alarm_id2 = alarm2["alarm_id"]
    print(f"  Nouvelle alarme creee id={alarm_id2} sur {new_primary}")

    diag.on_phase_end("PHASE 2")
    print("  Phase 2 OK")

    # ------------------------------------------------------------------
    # PHASE 3 : Remonter le noeud crash, verifier rejoin
    # ------------------------------------------------------------------
    diag._current_phase = "PHASE 3"
    print(f"\n--- PHASE 3 : Remonter {primary} ---")
    diag.on_phase_start("PHASE 3: Rejoin")

    start_node(primary)
    ok = wait_node_healthy(primary, timeout=60)
    assert_true(ok, f"{primary} a rejoint le cluster")

    # Verifier que le noeud revenu voit la nouvelle alarme
    time.sleep(5)
    rejoined_url = NODES[primary]["backend"]
    try:
        alarms = requests.get(f"{rejoined_url}/api/alarms/active", timeout=5).json()
        has_alarm2 = any(a["id"] == alarm_id2 for a in alarms)
        assert_true(has_alarm2, f"{primary} voit l'alarme {alarm_id2} (resync OK)")
    except Exception as e:
        assert_true(False, f"{primary} resync: {e}")

    diag.on_phase_end("PHASE 3")
    print("  Phase 3 OK")

    # ------------------------------------------------------------------
    # PHASE 4 : Escalade sur le nouveau primary
    # ------------------------------------------------------------------
    diag._current_phase = "PHASE 4"
    print(f"\n--- PHASE 4 : Escalade sur {new_primary} ---")
    diag.on_phase_start("PHASE 4: Escalade")

    # Re-login sur le primary courant et remettre les users online
    for uname, creds in USERS.items():
        token, uid, _ = login(creds["name"], creds["password"], base=new_primary_url)
        tokens[uname] = token
        # Envoyer un heartbeat pour marquer le user online
        requests.post(f"{new_primary_url}/api/devices/heartbeat",
                      headers={"Authorization": f"Bearer {token}"}, timeout=5)

    time.sleep(3)

    trigger_escalation(base=new_primary_url)
    time.sleep(3)
    alarms = get_active_alarms(base=new_primary_url, token=tokens["admin"])
    alarm = alarms[0]
    assert_true(alarm["assigned_user_id"] == user_ids["user2"],
                f"Escalade OK: alarm assignee a user2 ({alarm['assigned_user_id']})")

    trigger_escalation(base=new_primary_url)
    time.sleep(3)
    alarms = get_active_alarms(base=new_primary_url, token=tokens["admin"])
    alarm = alarms[0]
    assert_true(alarm["assigned_user_id"] == user_ids["admin"],
                f"Escalade OK: alarm assignee a admin ({alarm['assigned_user_id']})")

    # Les 3 users voient l'alarme (cumulative)
    assert_alarm_active(tokens["user1"], base=new_primary_url)
    assert_alarm_active(tokens["user2"], base=new_primary_url)
    assert_alarm_active(tokens["admin"], base=new_primary_url)
    print("  Escalade cumulative OK")

    # Resolution
    resolve_alarm(alarm["id"], tokens["admin"], base=new_primary_url)
    time.sleep(3)
    assert_alarm_active(tokens["user1"], base=new_primary_url, should_exist=False)
    diag.on_phase_end("PHASE 4")
    print("  Phase 4 OK")

    # ------------------------------------------------------------------
    # PHASE 5 : Verifier que les 3 noeuds sont coherents
    # ------------------------------------------------------------------
    diag._current_phase = "PHASE 5"
    print("\n--- PHASE 5 : Coherence finale ---")
    diag.on_phase_start("PHASE 5: Coherence")
    time.sleep(5)
    for name, node in NODES.items():
        try:
            r = requests.get(f"{node['backend']}/health", timeout=2)
            role = r.json().get("role", "unknown")
            alarms = requests.get(f"{node['backend']}/api/alarms/active", timeout=5).json()
            assert_true(len(alarms) == 0, f"{name} ({role}): 0 alarmes actives")
        except Exception as e:
            assert_true(False, f"{name} coherence: {e}")

    diag.on_phase_end("PHASE 5")
    print("  Phase 5 OK")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    print("\n--- CLEANUP ---")
    reset_backend()

    # Rapport final (meme en cas de succes)
    diag.snapshot("FINAL")
    diag.write_report()

    print("\n" + "=" * 70)
    print("TOUS LES TESTS PASSES")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test HA 3 noeuds")
    parser.add_argument("--emu1", default="emulator-5552")
    parser.add_argument("--emu2", default="emulator-5554")
    parser.add_argument("--emu3", default="emulator-5556")
    args = parser.parse_args()

    try:
        run_scenario(args.emu1, args.emu2, args.emu3)
    except TestAssertionError as e:
        print(f"\n{'=' * 70}")
        print(f"TEST ECHOUE: {e}")
        print(f"{'=' * 70}")
        sys.exit(1)
    except Exception as e:
        print(f"\n{'=' * 70}")
        print(f"ERREUR INATTENDUE: {e}")
        import traceback
        traceback.print_exc()
        print(f"{'=' * 70}")
        sys.exit(2)
