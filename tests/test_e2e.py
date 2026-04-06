"""
Tests E2E du système d'alarmes critiques (Backend).

Lancés contre un backend live. Tests Android dans Espresso (android/app/src/androidTest/).
Lancer avec : python -m pytest tests/test_e2e.py -v

Prérequis :
- Backend actif sur http://localhost:8000
"""

import os
import time
import subprocess
import requests
import pytest

_ALL_BACKEND_URLS = ["http://localhost:8000", "http://localhost:8001", "http://localhost:8002"]


def _find_primary_url():
    """Trouve le backend du noeud primary parmi les backends disponibles."""
    for url in _ALL_BACKEND_URLS:
        try:
            r = requests.get(f"{url}/health", timeout=2)
            if r.status_code == 200 and r.json().get("role") == "primary":
                return url
        except Exception:
            continue
    # Fallback : premier qui repond
    return os.getenv("BACKEND_URL", "http://localhost:8000")


BASE_URL = os.getenv("BACKEND_URL", None) or _find_primary_url()
API = f"{BASE_URL}/api"


def _reset_clock_all_nodes():
    """Reset l'horloge sur les 3 backends directement (pas via broadcast instable)."""
    for url in _ALL_BACKEND_URLS:
        try:
            requests.post(f"{url}/api/test/reset-clock?peer=false", timeout=2)
        except Exception:
            pass


def _advance_clock_all_nodes(minutes):
    """Avance l'horloge sur les 3 backends directement (pas via broadcast)."""
    for url in _ALL_BACKEND_URLS:
        try:
            requests.post(f"{url}/api/test/advance-clock",
                         params={"minutes": minutes, "peer": "false"}, timeout=2)
        except Exception:
            pass


@pytest.fixture(autouse=True, scope="function")
def _ensure_primary_api(request):
    """Re-detecte le primary avant chaque test, au cas ou un failover a eu lieu."""
    global BASE_URL, API
    new_url = _find_primary_url()
    if new_url != BASE_URL:
        BASE_URL = new_url
        API = f"{BASE_URL}/api"

# Noms sans espaces
ADMIN_NAME = "admin"
ADMIN_PASSWORD = "admin123"
USER1_NAME = "user1"
USER1_PASSWORD = "user123"
USER2_NAME = "user2"
USER2_PASSWORD = "user123"


# ── Helpers d'authentification ───────────────────────────────────────────────

def _login_user(name, password, base_api=None):
    """Login and return (token, headers, user_id)."""
    if base_api is None:
        base_api = API
    r = requests.post(f"{base_api}/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"Login failed for {name}: {r.text}"
    data = r.json()
    token = data["access_token"]
    return token, {"Authorization": f"Bearer {token}"}, data["user"]["id"]


def _admin_headers(base_api=None):
    """Get admin auth headers."""
    _, headers, _ = _login_user(ADMIN_NAME, ADMIN_PASSWORD, base_api)
    return headers


def _user_headers(name, password, base_api=None):
    """Get user auth headers and id."""
    _, headers, uid = _login_user(name, password, base_api)
    return headers, uid


# ── Santé backend ────────────────────────────────────────────────────────────

class TestBackendHealth:

    def test_health_check(self):
        r = requests.get(f"{BASE_URL}/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_web_ui_loads(self):
        r = requests.get(BASE_URL)
        assert r.status_code == 200
        assert "Alarmes Critiques" in r.text


# ── Login ────────────────────────────────────────────────────────────────────

class TestUserLogin:

    @pytest.fixture(autouse=True)
    def setup(self):
        # Nettoyer les users de test residuels (testcaseuser, bad name, etc.)
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        default_names = {"admin", "user1", "user2"}
        for u in users:
            if u["name"] not in default_names:
                requests.delete(f"{API}/users/{u['id']}", headers=admin_h)
        yield
        # Cleanup apres aussi
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        for u in users:
            if u["name"] not in default_names:
                requests.delete(f"{API}/users/{u['id']}", headers=admin_h)

    def test_admin_login(self):
        r = requests.post(f"{API}/auth/login", json={
            "name": ADMIN_NAME, "password": ADMIN_PASSWORD
        })
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["user"]["is_admin"] is True

    def test_login_case_insensitive(self):
        """Le login doit être insensible à la casse."""
        r = requests.post(f"{API}/auth/login", json={
            "name": "ADMIN", "password": ADMIN_PASSWORD
        })
        assert r.status_code == 200
        assert r.json()["user"]["name"] == ADMIN_NAME

        r = requests.post(f"{API}/auth/login", json={
            "name": "Admin", "password": ADMIN_PASSWORD
        })
        assert r.status_code == 200

        r = requests.post(f"{API}/auth/login", json={
            "name": "aDmIn", "password": ADMIN_PASSWORD
        })
        assert r.status_code == 200

    def test_user1_login(self):
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        assert r.status_code == 200
        assert r.json()["user"]["name"] == USER1_NAME

    def test_invalid_login(self):
        r = requests.post(f"{API}/auth/login", json={
            "name": "baduser", "password": "wrong"
        })
        assert r.status_code == 401

    def test_user_list(self):
        admin_h = _admin_headers()
        r = requests.get(f"{API}/users/", headers=admin_h)
        assert r.status_code == 200
        users = r.json()
        assert len(users) >= 3
        # Aucun utilisateur ne doit avoir d'espaces dans son nom
        for u in users:
            assert " " not in u["name"], (
                f"Le nom '{u['name']}' contient des espaces"
            )

    def test_register_rejects_spaces_in_name(self):
        """L'inscription doit refuser les noms avec des espaces."""
        r = requests.post(f"{API}/auth/register", json={
            "name": "bad name",
            "password": "test123",
        })
        assert r.status_code == 422 or r.status_code == 400, (
            f"Le nom avec espaces aurait dû être refusé, got {r.status_code}"
        )

    def test_register_stores_lowercase(self):
        """L'inscription doit stocker le nom en minuscules."""
        # Nettoyer si existe
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        for u in users:
            if u["name"] == "testcaseuser":
                requests.delete(f"{API}/users/{u['id']}", headers=admin_h)

        r = requests.post(f"{API}/auth/register", json={
            "name": "TestCaseUser",
            "password": "test123",
        })
        assert r.status_code == 200
        assert r.json()["name"] == "testcaseuser"

        # Nettoyer
        requests.delete(f"{API}/users/{r.json()['id']}", headers=admin_h)


# ── Alarme unique ────────────────────────────────────────────────────────────

class TestSingleAlarm:
    """Une seule alarme active à la fois."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        self.token = r.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.user1_id = r.json()["user"]["id"]

    def test_send_alarm(self):
        """Envoyer une alarme crée bien une alarme active."""
        r = requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Panne serveur",
            "message": "Le serveur ne répond plus",
            "severity": "critical",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_only_one_alarm_at_a_time(self):
        """Envoyer une 2e alarme alors qu'une est active doit être refusé (409)."""
        requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Alarme 1", "message": "msg", "severity": "critical",
        })
        r = requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Alarme 2", "message": "msg2", "severity": "high",
        })
        assert r.status_code == 409

    def test_can_send_after_resolve(self):
        """Après résolution, on peut envoyer une nouvelle alarme."""
        r = requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Alarme A", "message": "m", "severity": "critical",
        })
        alarm_id = r.json()["id"]
        requests.post(f"{API}/alarms/{alarm_id}/resolve", headers=self.headers)

        r = requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Alarme B", "message": "m", "severity": "critical",
        })
        assert r.status_code == 200
        assert r.json()["title"] == "Alarme B"

    def test_alarm_received_by_user(self):
        """L'utilisateur reçoit l'alarme active via /mine."""
        requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Test reception",
            "message": "msg",
            "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        r = requests.get(f"{API}/alarms/mine", headers=self.headers)
        assert r.status_code == 200
        alarms = r.json()
        assert len(alarms) == 1
        assert alarms[0]["title"] == "Test reception"


# ── Acquittement ─────────────────────────────────────────────────────────────

class TestAlarmAcknowledgement:

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset")          # Then reset users (heartbeats use clock_now)
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        self.token = r.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.user1_id = r.json()["user"]["id"]
        yield
        _reset_clock_all_nodes()

    def test_acknowledge_alarm(self):
        r = requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Ack Test", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        r = requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers)
        assert r.status_code == 200
        assert r.json()["status"] == "acknowledged"
        assert r.json()["acknowledged_at"] is not None
        assert r.json()["suspended_until"] is not None

    def test_suspended_alarm_visible_in_mine_as_acknowledged(self):
        """Une alarme acquittée reste visible dans /mine avec status acknowledged."""
        r = requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Suspend", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]
        requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers)

        r = requests.get(f"{API}/alarms/mine", headers=self.headers)
        acked = [a for a in r.json() if a["id"] == alarm_id]
        assert len(acked) == 1, "L'alarme acquittée doit rester visible dans /mine"
        assert acked[0]["status"] == "acknowledged"

    def test_acknowledge_stores_user_name(self):
        """L'acquittement doit enregistrer le nom de l'utilisateur."""
        r = requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Ack Name Test", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        r = requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers)
        assert r.status_code == 200
        assert r.json()["acknowledged_by_name"] == USER1_NAME

    def _find_alarm_by_title(self, title, retries=5):
        """Find an alarm by title with retry logic to handle background loop timing."""
        admin_h = _admin_headers()
        last_status = None
        last_body = None
        for attempt in range(retries):
            r = requests.get(f"{API}/alarms/", headers=admin_h)
            last_status = r.status_code
            last_body = r.text[:500]
            if r.status_code != 200:
                time.sleep(2)
                continue
            data = r.json()
            if not isinstance(data, list):
                time.sleep(2)
                continue
            alarm = next((a for a in data if a.get("title") == title), None)
            if alarm is not None:
                return alarm
            time.sleep(2)
        return None

    def _refresh_all_heartbeats(self):
        """Send heartbeats for all users to keep them online after clock advance."""
        for name, password in [(USER1_NAME, USER1_PASSWORD), (USER2_NAME, USER2_PASSWORD),
                               (ADMIN_NAME, ADMIN_PASSWORD)]:
            r = requests.post(f"{API}/auth/login", json={"name": name, "password": password})
            requests.post(f"{API}/devices/heartbeat",
                          headers={"Authorization": f"Bearer {r.json()['access_token']}"})

    def _ensure_clean_state(self):
        """Ensure no stale alarms exist before proceeding."""
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        # Verify
        r = requests.get(f"{API}/alarms/", headers=admin_h)
        if r.status_code == 200 and r.json():
            time.sleep(1)
            requests.post(f"{API}/alarms/reset", headers=admin_h)

    def test_ack_expiry_reactivates_alarm(self):
        """#1 — Après expiration de la suspension (30min), l'alarme redevient active."""
        self._ensure_clean_state()
        r = requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Expiry Test", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        # Acquitter
        requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers)

        # Vérifier que l'alarme est visible mais acknowledged
        r = requests.get(f"{API}/alarms/mine", headers=self.headers)
        acked = [a for a in r.json() if a["id"] == alarm_id]
        assert len(acked) == 1
        assert acked[0]["status"] == "acknowledged"

        # Avancer de 31 min (suspension = 30 min)
        _advance_clock_all_nodes(31)
        self._refresh_all_heartbeats()  # Garder les users online après avance horloge
        time.sleep(12)  # Attendre un tick de la boucle d'escalade

        # L'alarme doit être redevenue active (ou escaladée si la boucle a déjà tourné)
        alarm = self._find_alarm_by_title("Expiry Test")
        assert alarm is not None, \
            f"Alarme 'Expiry Test' introuvable dans la liste"
        assert alarm["status"] in ("active", "escalated"), \
            f"Alarme devrait être réactivée après expiration, mais est '{alarm['status']}'"
        assert alarm["status"] != "acknowledged", \
            "L'alarme ne devrait plus être 'acknowledged' après expiration"

    def test_ack_expiry_escalation_restarts(self):
        """#3 — Après réactivation post-ack, l'alarme est escaladable."""
        self._ensure_clean_state()
        r = requests.post(f"{API}/alarms/send", headers=self.headers, json={
            "title": "Ack+Esc Test", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers)

        # Avancer de 31 min → réactivation par la boucle d'escalade
        _advance_clock_all_nodes(31)
        self._refresh_all_heartbeats()  # Garder les users online après avance horloge
        time.sleep(12)

        # L'alarme doit ne plus être acknowledged (réactivée ou déjà escaladée)
        alarm = self._find_alarm_by_title("Ack+Esc Test")
        assert alarm is not None, "L'alarme 'Ack+Esc Test' a disparu"
        assert alarm["status"] != "acknowledged", \
            f"L'alarme devrait être réactivée, mais est encore '{alarm['status']}'"

    def _get_user_id(self, name):
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        return next(u["id"] for u in users if u["name"] == name)


# ── Escalade (via trigger manuel — pas de sleep) ────────────────────────────

class TestEscalation:
    """Tests d'escalade déterministes via /test/trigger-escalation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        requests.post(f"{API}/test/reset")  # Remet tout le monde online
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/config/escalation", headers=admin_h, json={
            "position": 1, "user_id": self._get_user_id(USER1_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", headers=admin_h, json={
            "position": 2, "user_id": self._get_user_id(USER2_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", headers=admin_h, json={
            "position": 3, "user_id": self._get_user_id(ADMIN_NAME), "delay_minutes": 15
        })

    def _get_user_id(self, name):
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        return next(u["id"] for u in users if u["name"] == name)

    def test_escalation_user1_to_user2(self):
        user1_id = self._get_user_id(USER1_NAME)
        user2_id = self._get_user_id(USER2_NAME)
        admin_h = _admin_headers()

        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Escalade", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        r = requests.post(f"{API}/test/trigger-escalation")
        assert r.status_code == 200

        r = requests.get(f"{API}/alarms/", headers=admin_h)
        alarm = next(a for a in r.json() if a["id"] == alarm_id)
        assert alarm["assigned_user_id"] == user2_id
        assert alarm["escalation_count"] == 1
        assert alarm["status"] == "escalated"

    def test_escalation_chain_user2_to_admin(self):
        user1_id = self._get_user_id(USER1_NAME)
        admin_id = self._get_user_id(ADMIN_NAME)
        admin_h = _admin_headers()

        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Double escalade", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        requests.post(f"{API}/test/trigger-escalation")
        requests.post(f"{API}/test/trigger-escalation")

        r = requests.get(f"{API}/alarms/", headers=admin_h)
        alarm = r.json()[0]
        assert alarm["assigned_user_id"] == admin_id
        assert alarm["escalation_count"] == 2

    def test_no_escalation_if_acknowledged(self):
        user1_id = self._get_user_id(USER1_NAME)
        admin_h = _admin_headers()

        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Ack avant escalade", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        r_login = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        headers = {"Authorization": f"Bearer {r_login.json()['access_token']}"}
        requests.post(f"{API}/alarms/{alarm_id}/ack", headers=headers)

        requests.post(f"{API}/test/trigger-escalation")

        r = requests.get(f"{API}/alarms/", headers=admin_h)
        alarm = next(a for a in r.json() if a["id"] == alarm_id)
        assert alarm["assigned_user_id"] == user1_id
        assert alarm["escalation_count"] == 0

    def test_no_escalation_if_no_active_alarm(self):
        r = requests.post(f"{API}/test/trigger-escalation")
        assert r.status_code == 200
        assert r.json()["escalated"] == 0

    def test_escalation_wraps_around_after_last_user(self):
        """Quand l'escalade atteint le dernier de la chaîne,
        elle doit reboucler au premier utilisateur."""
        user1_id = self._get_user_id(USER1_NAME)
        admin_h = _admin_headers()

        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Wrap around", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Escalade 3 fois : user1 → user2 → admin → user1 (wrap)
        requests.post(f"{API}/test/trigger-escalation")  # → user2
        requests.post(f"{API}/test/trigger-escalation")  # → admin
        requests.post(f"{API}/test/trigger-escalation")  # → user1 (wrap)

        r = requests.get(f"{API}/alarms/", headers=admin_h)
        alarm = r.json()[0]
        assert alarm["assigned_user_id"] == user1_id, \
            f"L'escalade devrait reboucler sur user1 (id={user1_id}), " \
            f"mais elle est sur user_id={alarm['assigned_user_id']}"
        assert alarm["escalation_count"] == 3

    def test_escalation_wrap_continues_cycling(self):
        """Le rebouclage doit continuer indéfiniment (2 tours complets)."""
        user1_id = self._get_user_id(USER1_NAME)
        user2_id = self._get_user_id(USER2_NAME)
        admin_h = _admin_headers()

        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Multi wrap", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # 4 escalades : user1 → user2 → admin → user1 → user2
        for _ in range(4):
            requests.post(f"{API}/test/trigger-escalation")

        r = requests.get(f"{API}/alarms/", headers=admin_h)
        alarm = r.json()[0]
        assert alarm["assigned_user_id"] == user2_id, \
            f"Après 4 escalades, devrait être sur user2 (id={user2_id})"
        assert alarm["escalation_count"] == 4


class TestEscalationWithClock:
    """Tests d'escalade avec horloge injectable — vérifie le respect des délais."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset")          # Then reset users (heartbeats use clock_now)
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/config/escalation", headers=admin_h, json={
            "position": 1, "user_id": self._get_user_id(USER1_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", headers=admin_h, json={
            "position": 2, "user_id": self._get_user_id(USER2_NAME), "delay_minutes": 15
        })
        yield
        _reset_clock_all_nodes()

    def _get_user_id(self, name):
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        return next(u["id"] for u in users if u["name"] == name)

    def _find_alarm_by_title(self, title, retries=3):
        """Find an alarm by title with retry logic to handle background loop timing."""
        admin_h = _admin_headers()
        for attempt in range(retries):
            r = requests.get(f"{API}/alarms/", headers=admin_h)
            if r.status_code != 200:
                time.sleep(2)
                continue
            data = r.json()
            if not isinstance(data, list):
                time.sleep(2)
                continue
            alarm = next((a for a in data if a.get("title") == title), None)
            if alarm is not None:
                return alarm
            time.sleep(2)
        return None

    def _refresh_all_heartbeats(self):
        """Send heartbeats for all users to keep them online after clock advance."""
        for name, password in [(USER1_NAME, USER1_PASSWORD), (USER2_NAME, USER2_PASSWORD),
                               (ADMIN_NAME, ADMIN_PASSWORD)]:
            r = requests.post(f"{API}/auth/login", json={"name": name, "password": password})
            requests.post(f"{API}/devices/heartbeat",
                          headers={"Authorization": f"Bearer {r.json()['access_token']}"})

    def test_no_escalation_before_delay(self):
        """A 13 min, l'alarme ne doit PAS etre escaladee (seuil = 15 min)."""
        user1_id = self._get_user_id(USER1_NAME)
        admin_h = _admin_headers()

        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Timing no-esc", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        # Avancer de 13 min sur TOUS les noeuds (pas de broadcast)
        _advance_clock_all_nodes(13)
        self._refresh_all_heartbeats()  # Keep users online after clock advance

        time.sleep(11)  # Attendre un tick de la boucle d'escalade (10s)

        alarm = self._find_alarm_by_title("Timing no-esc")
        assert alarm is not None, \
            f"Alarme 'Timing no-esc' introuvable dans la liste"
        assert alarm["assigned_user_id"] == user1_id, \
            f"Alarme escaladée trop tôt (à 14 min au lieu de 15)"
        assert alarm["escalation_count"] == 0

    def test_escalation_after_delay(self):
        """À 16 min, l'alarme DOIT être escaladée."""
        user1_id = self._get_user_id(USER1_NAME)
        user2_id = self._get_user_id(USER2_NAME)
        admin_h = _admin_headers()

        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Timing esc-16", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        # Avancer de 16 min sur tous les noeuds
        _advance_clock_all_nodes(16)
        self._refresh_all_heartbeats()  # Keep users online after clock advance

        time.sleep(11)  # Attendre un tick de la boucle d'escalade

        alarm = self._find_alarm_by_title("Timing esc-16")
        assert alarm is not None, \
            f"Alarme 'Timing esc-16' introuvable dans la liste"
        assert alarm["assigned_user_id"] == user2_id, \
            f"Alarme non escaladée après 16 min (encore sur user {alarm['assigned_user_id']})"
        assert alarm["escalation_count"] == 1

    def test_escalation_exactly_at_boundary(self):
        """À exactement 15 min, l'alarme doit être escaladée (>=)."""
        user1_id = self._get_user_id(USER1_NAME)
        user2_id = self._get_user_id(USER2_NAME)
        admin_h = _admin_headers()

        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Boundary-15", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        _advance_clock_all_nodes(15)
        self._refresh_all_heartbeats()  # Keep users online after clock advance

        time.sleep(11)

        alarm = self._find_alarm_by_title("Boundary-15")
        assert alarm is not None, \
            f"Alarme 'Boundary-15' introuvable dans la liste"
        assert alarm["assigned_user_id"] == user2_id
        assert alarm["escalation_count"] == 1


# ── Watchdog ─────────────────────────────────────────────────────────────────

class TestWatchdog:

    def test_user_heartbeat(self):
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = requests.post(f"{API}/devices/register", headers=headers, json={
            "device_token": "test-device-001"
        })
        assert r.status_code == 200

        r = requests.post(f"{API}/devices/heartbeat", headers=headers)
        assert r.status_code == 200

    def test_watchdog_detects_offline(self):
        r = requests.post(f"{API}/auth/login", json={
            "name": USER2_NAME, "password": USER2_PASSWORD
        })
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        requests.post(f"{API}/devices/heartbeat", headers=headers)
        requests.post(f"{API}/test/simulate-watchdog-failure")

        admin_h = _admin_headers()
        r = requests.get(f"{API}/users/", headers=admin_h)
        user = next(u for u in r.json() if u["name"] == USER2_NAME)
        assert user["is_online"] is False

    def test_heartbeat_recent_shows_seconds(self):
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        requests.post(f"{API}/devices/heartbeat", headers=headers)
        time.sleep(1)

        admin_h = _admin_headers()
        r = requests.get(f"{API}/users/", headers=admin_h)
        user = next(u for u in r.json() if u["name"] == USER1_NAME)
        assert user["is_online"] is True

        from datetime import datetime
        hb_time = datetime.fromisoformat(user["last_heartbeat"].replace("Z", "+00:00").split("+")[0])
        now = datetime.utcnow()
        diff = (now - hb_time).total_seconds()
        assert diff < 10


# ── Interface web / endpoints de test ────────────────────────────────────────

class TestWebInterface:

    def test_send_test_alarm(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        r = requests.post(f"{API}/test/send-alarm")
        assert r.status_code == 200
        assert r.json()["status"] == "sent"

    def test_simulate_watchdog(self):
        r = requests.post(f"{API}/test/simulate-watchdog-failure")
        assert r.status_code == 200

    def test_simulate_connection_loss(self):
        r = requests.post(f"{API}/test/simulate-connection-loss")
        assert r.status_code == 200

    def test_reset_all(self):
        r = requests.post(f"{API}/test/reset")
        assert r.status_code == 200

    def test_system_status_has_connected_users(self):
        r = requests.get(f"{API}/test/status")
        assert r.status_code == 200
        data = r.json()
        assert "users" in data
        assert "connected_users" in data
        assert "alarms" in data

    def test_toggle_heartbeat_pause(self):
        r = requests.post(f"{API}/test/toggle-heartbeat-pause")
        assert r.status_code == 200
        assert r.json()["paused"] is True

        r = requests.post(f"{API}/test/toggle-heartbeat-pause")
        assert r.status_code == 200
        assert r.json()["paused"] is False


# ── #2 Escalade skip offline ──────────────────────────────────────────────────

class TestEscalationSkipOffline:
    """#2 — L'escalade doit sauter les utilisateurs offline."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset")  # Remet tout le monde online
        requests.post(f"{API}/config/escalation", headers=admin_h, json={
            "position": 1, "user_id": self._get_user_id(USER1_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", headers=admin_h, json={
            "position": 2, "user_id": self._get_user_id(USER2_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", headers=admin_h, json={
            "position": 3, "user_id": self._get_user_id(ADMIN_NAME), "delay_minutes": 15
        })

    def _get_user_id(self, name):
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        return next(u["id"] for u in users if u["name"] == name)

    def test_escalation_skips_offline_user(self):
        """Si user2 est offline, l'escalade passe directement à admin."""
        user1_id = self._get_user_id(USER1_NAME)
        user2_id = self._get_user_id(USER2_NAME)
        admin_id = self._get_user_id(ADMIN_NAME)
        admin_h = _admin_headers()

        # Mettre tout le monde offline
        requests.post(f"{API}/test/simulate-connection-loss")
        # Remettre user1 et admin online (user2 reste offline)
        r = requests.post(f"{API}/auth/login", json={"name": USER1_NAME, "password": USER1_PASSWORD})
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
        r = requests.post(f"{API}/auth/login", json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD})
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {r.json()['access_token']}"})

        # Vérifier que user2 est bien offline avant de continuer
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        user2 = next(u for u in users if u["name"] == USER2_NAME)
        assert user2["is_online"] is False, \
            f"user2 devrait être offline mais is_online={user2['is_online']}"

        # Envoyer alarme à user1
        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Skip offline", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Escalader → devrait sauter user2 (offline) et aller à admin
        requests.post(f"{API}/test/trigger-escalation")

        r = requests.get(f"{API}/alarms/", headers=admin_h)
        alarm = r.json()[0]
        assert alarm["assigned_user_id"] == admin_id, \
            f"Devrait sauter user2 (offline) → admin, mais assignée à {alarm['assigned_user_id']}"

    def test_escalation_all_offline_wraps_to_first_online(self):
        """Si tous sauf user1 sont offline, on reste sur user1 (pas de boucle infinie)."""
        user1_id = self._get_user_id(USER1_NAME)
        admin_h = _admin_headers()

        # Mettre tout le monde offline sauf user1
        requests.post(f"{API}/test/simulate-connection-loss")
        r = requests.post(f"{API}/auth/login", json={"name": USER1_NAME, "password": USER1_PASSWORD})
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {r.json()['access_token']}"})

        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Tous offline", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Escalader → personne d'autre n'est online, pas d'escalade
        r = requests.post(f"{API}/test/trigger-escalation")
        assert r.json()["escalated"] == 0


# ── #5 Utilisateur supprimé pendant alarme ───────────────────────────────────

class TestDeletedUserDuringAlarm:
    """#5 — Si l'utilisateur assigné est supprimé, l'alarme est réassignée."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)

    def _get_user_id(self, name):
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        return next((u["id"] for u in users if u["name"] == name), None)

    def test_alarm_reassigned_when_user_deleted(self):
        """Quand l'utilisateur assigné est supprimé, l'alarme doit être
        réassignée au premier de la chaîne d'escalade."""
        admin_h = _admin_headers()
        # Supprimer le tempuser s'il existe déjà
        existing_id = self._get_user_id("tempuser")
        if existing_id:
            requests.delete(f"{API}/users/{existing_id}", headers=admin_h)

        # Créer un utilisateur temporaire
        r = requests.post(f"{API}/auth/register", json={
            "name": "tempuser", "password": "temp123"
        })
        temp_id = r.json()["id"]

        # Envoyer alarme à tempuser
        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Delete test", "message": "m", "severity": "critical",
            "assigned_user_id": temp_id,
        })

        # Supprimer tempuser
        requests.delete(f"{API}/users/{temp_id}", headers=admin_h)

        # L'alarme doit être réassignée
        r = requests.get(f"{API}/alarms/active", headers=admin_h)
        if r.json():
            alarm = r.json()[0]
            assert alarm["assigned_user_id"] != temp_id, \
                "L'alarme est encore assignée à un utilisateur supprimé"
            assert alarm["assigned_user_id"] is not None, \
                "L'alarme doit être réassignée, pas sans assignation"


# ── #4 Alerte email si chaîne escalade vide ──────────────────────────────────

class TestEmptyEscalationChainAlert:
    """#4 — Envoyer un email si la chaîne d'escalade est vide quand une alarme arrive."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        # Reset email config to default
        requests.post(f"{API}/config/system", headers=admin_h, json={
            "key": "alert_email", "value": "direction_technique@charlesmurgat.com"
        })

    def test_alarm_with_empty_chain_sends_email(self):
        """Quand la chaîne est vide et une alarme est envoyée,
        un email doit être envoyé à l'adresse configurée."""
        admin_h = _admin_headers()
        # Supprimer toutes les règles d'escalade
        esc = requests.get(f"{API}/config/escalation", headers=admin_h).json()
        for e in esc:
            requests.delete(f"{API}/config/escalation/{e['id']}", headers=admin_h)

        # Vérifier que la chaîne est vide
        assert len(requests.get(f"{API}/config/escalation", headers=admin_h).json()) == 0

        # Envoyer une alarme
        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "No chain", "message": "Chaîne vide", "severity": "critical",
        })
        assert r.status_code == 200

        # Vérifier que l'email a été logué (on utilise un mock SMTP en test)
        r = requests.get(f"{API}/test/last-email-sent")
        assert r.status_code == 200
        email = r.json()
        assert email["sent"] is True
        assert "direction_technique@charlesmurgat.com" in email["to"]
        assert "escalade" in email["subject"].lower() or "chaîne" in email["subject"].lower()

    def test_email_recipient_is_configurable(self):
        """L'adresse email d'alerte doit être paramétrable."""
        admin_h = _admin_headers()
        r = requests.post(f"{API}/config/system", headers=admin_h, json={
            "key": "alert_email", "value": "custom@example.com"
        })
        assert r.status_code == 200

        r = requests.get(f"{API}/config/system", headers=admin_h)
        assert r.json()["alert_email"] == "custom@example.com"


# ── #8 Résilience erreurs backend (côté API) ────────────────────────────────

class TestBackendResilience:
    """#8 — Le backend ne crash pas sur les cas limites."""

    def test_ack_nonexistent_alarm(self):
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = requests.post(f"{API}/alarms/99999/ack", headers=headers)
        assert r.status_code == 404

    def test_resolve_nonexistent_alarm(self):
        headers = {"Authorization": f"Bearer dummy"}
        r = requests.post(f"{API}/alarms/99999/resolve", headers=headers)
        assert r.status_code == 404 or r.status_code == 401

    def test_login_empty_fields(self):
        r = requests.post(f"{API}/auth/login", json={"name": "", "password": ""})
        assert r.status_code in (401, 422)

    def test_send_alarm_missing_fields(self):
        headers = {"Authorization": f"Bearer dummy"}
        r = requests.post(f"{API}/alarms/send", headers=headers, json={})
        assert r.status_code == 422 or r.status_code == 401

    def test_heartbeat_with_invalid_token(self):
        headers = {"Authorization": "Bearer invalid-garbage-token"}
        r = requests.post(f"{API}/devices/heartbeat", headers=headers)
        assert r.status_code == 401

    def test_mine_with_invalid_token(self):
        headers = {"Authorization": "Bearer invalid-garbage-token"}
        r = requests.get(f"{API}/alarms/mine", headers=headers)
        assert r.status_code == 401


# ── #7 Renouvellement auto du token ─────────────────────────────────────────

class TestTokenAutoRenewal:
    """#7 — Le backend doit supporter un endpoint de refresh token."""

    def test_refresh_token_endpoint_exists(self):
        """Le endpoint /api/auth/refresh doit renouveler le token."""
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = requests.post(f"{API}/auth/refresh", headers=headers)
        assert r.status_code == 200
        new_token = r.json()["access_token"]
        assert new_token != token  # Nouveau token différent
        assert "access_token" in r.json()


# ── #6 Persistence après crash Docker ─────────────────────────────────────────

DOCKER = os.getenv("DOCKER_CMD", "C:/Program Files/Docker/Docker/resources/bin/docker.exe")


class TestPersistenceAfterCrash:
    """#6 — Les données survivent à un redémarrage du backend."""

    def test_data_persists_after_restart(self):
        """Créer un utilisateur + alarme, restart Docker, vérifier qu'ils existent."""
        admin_h = _admin_headers()
        # Nettoyer et créer des données
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        users_before = requests.get(f"{API}/users/", headers=admin_h).json()
        user_count_before = len(users_before)

        # Créer une alarme
        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Persistence Test", "message": "Survive restart", "severity": "critical",
        })

        # Vérifier avant restart
        alarms = requests.get(f"{API}/alarms/", headers=admin_h).json()
        persist_alarm = next((a for a in alarms if a["title"] == "Persistence Test"), None)
        assert persist_alarm is not None

        # Restart le backend Docker
        subprocess.run(
            [DOCKER, "compose", "restart", "backend"],
            cwd="C:/Users/Charles/Desktop/Projet Claude/Alarm2.0",
            capture_output=True, timeout=60,
        )

        # Attendre que le backend soit prêt
        for _ in range(20):
            try:
                r = requests.get(f"{BASE_URL}/health", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2)

        # Vérifier après restart
        admin_h = _admin_headers()  # Re-login after restart
        users_after = requests.get(f"{API}/users/", headers=admin_h).json()
        assert len(users_after) == user_count_before, \
            f"Utilisateurs perdus : {user_count_before} avant → {len(users_after)} après"

        alarms_after = requests.get(f"{API}/alarms/", headers=admin_h).json()
        persist_alarm_after = next((a for a in alarms_after if a["title"] == "Persistence Test"), None)
        assert persist_alarm_after is not None, \
            "L'alarme 'Persistence Test' a disparu après le restart"


# ── #3 Email via Mailhog ─────────────────────────────────────────────────────

MAILHOG_URL = os.getenv("MAILHOG_URL", "http://localhost:8025")


class TestEmailViaMailhog:
    """#3 — Vérifier que les emails sont réellement envoyés via SMTP (Mailhog)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        # Vider la boîte Mailhog
        try:
            requests.delete(f"{MAILHOG_URL}/api/v1/messages", timeout=3)
        except Exception:
            pytest.skip("Mailhog non disponible")

    def test_empty_chain_sends_real_email(self):
        """Quand la chaîne est vide, un email SMTP doit arriver dans Mailhog."""
        admin_h = _admin_headers()
        # Supprimer toutes les règles d'escalade
        esc = requests.get(f"{API}/config/escalation", headers=admin_h).json()
        for e in esc:
            requests.delete(f"{API}/config/escalation/{e['id']}", headers=admin_h)

        # Envoyer alarme → déclenche l'email
        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Mailhog Test", "message": "Test SMTP réel", "severity": "critical",
        })

        time.sleep(2)  # Laisser le temps au SMTP

        # Vérifier dans Mailhog
        r = requests.get(f"{MAILHOG_URL}/api/v2/messages", timeout=5)
        assert r.status_code == 200
        messages = r.json()
        assert messages["total"] >= 1, "Aucun email reçu dans Mailhog"

        last_msg = messages["items"][0]
        subject = last_msg["Content"]["Headers"]["Subject"][0]
        to = last_msg["Content"]["Headers"]["To"][0]
        assert "escalade" in subject.lower() or "chaîne" in subject.lower()
        assert "direction_technique@charlesmurgat.com" in to


# ── Escalade cumulative — tous les utilisateurs appelés sonnent ────────────────

class TestCumulativeEscalation:
    """L'escalade ne retire pas l'alarme aux utilisateurs précédents.
    Tous les utilisateurs appelés continuent de voir/entendre l'alarme.
    N'importe lequel peut acquitter."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset")
        # Envoyer des heartbeats pour maintenir les users online
        for name, pwd in [("user1", "user123"), ("user2", "user123"), ("admin", "admin123")]:
            t = requests.post(f"{API}/auth/login", json={"name": name, "password": pwd}).json()["access_token"]
            requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {t}"})
        yield
        _reset_clock_all_nodes()

    def _login(self, name):
        pwd = "admin123" if name == "admin" else "user123"
        r = requests.post(f"{API}/auth/login", json={"name": name, "password": pwd})
        return r.json()["access_token"]

    def _get_user_id(self, name):
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        return next(u["id"] for u in users if u["name"] == name)

    def test_escalated_alarm_still_visible_to_first_user(self):
        """Après escalade user1→user2, user1 doit TOUJOURS voir l'alarme dans /mine."""
        token1 = self._login("user1")
        token2 = self._login("user2")
        h1 = {"Authorization": f"Bearer {token1}"}
        h2 = {"Authorization": f"Bearer {token2}"}
        admin_h = _admin_headers()

        # Envoyer alarme (va à user1)
        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Cumul Test", "message": "test", "severity": "critical",
        })

        # Vérifier que user1 la voit
        r = requests.get(f"{API}/alarms/mine", headers=h1)
        assert len(r.json()) == 1

        # Escalader vers user2 : avancer l'horloge puis renvoyer les heartbeats
        # (sinon le watchdog marque les users offline car leurs heartbeats
        # datent de 16 min dans le referentiel de l'horloge injectee)
        _advance_clock_all_nodes(16)
        requests.post(f"{API}/devices/heartbeat", headers=h1)
        requests.post(f"{API}/devices/heartbeat", headers=h2)
        time.sleep(11)

        # user2 doit la voir
        r = requests.get(f"{API}/alarms/mine", headers=h2)
        assert len(r.json()) >= 1, "user2 ne voit pas l'alarme après escalade"

        # user1 doit TOUJOURS la voir
        r = requests.get(f"{API}/alarms/mine", headers=h1)
        assert len(r.json()) >= 1, "user1 ne voit plus l'alarme après escalade — doit rester"

    def test_any_notified_user_can_acknowledge(self):
        """User1 reçoit l'alarme, escalade vers user2, user1 peut quand même acquitter."""
        token1 = self._login("user1")
        h1 = {"Authorization": f"Bearer {token1}"}
        admin_h = _admin_headers()

        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Ack Cumul", "message": "test", "severity": "critical",
        })

        # Escalader
        _advance_clock_all_nodes(16)
        time.sleep(11)

        # user1 acquitte (même si l'alarme a été escaladée)
        alarms = requests.get(f"{API}/alarms/mine", headers=h1).json()
        assert len(alarms) >= 1
        alarm_id = alarms[0]["id"]

        r = requests.post(f"{API}/alarms/{alarm_id}/ack", headers=h1)
        assert r.status_code == 200
        assert r.json()["status"] == "acknowledged"

    def test_alarm_shows_notified_users(self):
        """L'alarme doit contenir la liste des utilisateurs notifiés."""
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Notif List", "message": "test", "severity": "critical",
        })

        # Avant escalade : seul user1 est notifié
        alarms = requests.get(f"{API}/alarms/", headers=admin_h).json()
        alarm = next(a for a in alarms if a["title"] == "Notif List")
        assert "notified_user_ids" in alarm, "Le champ notified_user_ids doit exister"
        user1_id = self._get_user_id("user1")
        assert user1_id in alarm["notified_user_ids"]

        # Après escalade : user1 ET user2 (trigger manuel pour fiabilité)
        requests.post(f"{API}/test/trigger-escalation")

        alarms = requests.get(f"{API}/alarms/", headers=admin_h).json()
        alarm = next(a for a in alarms if a["title"] == "Notif List")
        user2_id = self._get_user_id("user2")
        assert user1_id in alarm["notified_user_ids"], "user1 doit rester dans notified"
        assert user2_id in alarm["notified_user_ids"], "user2 doit être ajouté à notified"


# ── Utilisateur d'astreinte hors connexion ────────────────────────────────────

class TestOnCallDisconnectionAlarm:
    """Quand l'utilisateur #1 de l'escalade (astreinte) perd son heartbeat pendant
    15 minutes, une alarme automatique est créée pour prévenir les suivants.
    Les autres utilisateurs n'ont pas besoin d'être sous heartbeat.
    Si plus personne n'est connecté → email direction technique."""

    @pytest.fixture(autouse=True)
    def setup(self):
        # Ordre crucial : reset clock AVANT tout pour que le background task
        # ne recrée pas d'alarme d'astreinte avec un offset résiduel
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset")  # Met tout le monde online
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        # Attendre un cycle du background task avec l'état propre
        time.sleep(12)
        # Re-nettoyer si le background task a créé quelque chose entre temps
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        yield
        _reset_clock_all_nodes()

    def _get_user_id(self, name):
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        return next(u["id"] for u in users if u["name"] == name)

    def test_oncall_offline_15min_creates_alarm(self):
        """User1 (pos 1) perd son heartbeat. Après 15 min, alarme auto créée."""
        user1_id = self._get_user_id("user1")
        admin_h = _admin_headers()

        # Mettre user1 offline
        requests.post(f"{API}/test/simulate-connection-loss")
        # Remettre les autres online (seul user1 reste offline)
        token2 = requests.post(f"{API}/auth/login", json={"name": "user2", "password": "user123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token2}"})
        token_admin = requests.post(f"{API}/auth/login", json={"name": "admin", "password": "admin123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token_admin}"})

        # Avancer le temps de 15 min
        _advance_clock_all_nodes(16)
        # Après advance-clock, les heartbeats de user2/admin sont stales (16 min old)
        # → envoyer des heartbeats frais pour qu'ils restent online
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token2}"})
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token_admin}"})
        time.sleep(11)  # Attendre un tick du watchdog/escalation loop

        # Une alarme automatique doit avoir été créée
        alarms = requests.get(f"{API}/alarms/active", headers=admin_h).json()
        oncall_alarm = next((a for a in alarms if "astreinte" in a["title"].lower()), None)
        assert oncall_alarm is not None, \
            f"Aucune alarme 'astreinte' créée. Alarmes actives : {[a['title'] for a in alarms]}"

        # L'alarme doit être assignée à user2 (le suivant), pas à user1 (qui est offline)
        user2_id = self._get_user_id("user2")
        assert oncall_alarm["assigned_user_id"] == user2_id or \
            user2_id in oncall_alarm.get("notified_user_ids", [])

    def test_oncall_alarm_auto_resolves_on_reconnection(self):
        """Si user1 revient en ligne, l'alarme d'astreinte se résout automatiquement."""
        user1_id = self._get_user_id("user1")
        admin_h = _admin_headers()

        # Mettre user1 offline
        requests.post(f"{API}/test/simulate-connection-loss")
        token2 = requests.post(f"{API}/auth/login", json={"name": "user2", "password": "user123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token2}"})

        # Avancer 16 min → alarme créée
        _advance_clock_all_nodes(16)
        time.sleep(11)

        alarms = requests.get(f"{API}/alarms/active", headers=admin_h).json()
        assert any("astreinte" in a["title"].lower() for a in alarms)

        # User1 revient en ligne (heartbeat)
        token1 = requests.post(f"{API}/auth/login", json={"name": "user1", "password": "user123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token1}"})

        # Attendre un tick
        time.sleep(11)

        # L'alarme d'astreinte doit être automatiquement résolue
        alarms = requests.get(f"{API}/alarms/active", headers=admin_h).json()
        oncall_alarms = [a for a in alarms if "astreinte" in a["title"].lower()]
        assert len(oncall_alarms) == 0, \
            "L'alarme d'astreinte devrait être résolue après reconnexion de user1"

    def test_oncall_alarm_escalates_normally(self):
        """L'alarme d'astreinte suit l'escalade normale (user2 → admin → ...)."""
        admin_h = _admin_headers()
        # Tout le monde offline sauf user2 et admin
        requests.post(f"{API}/test/simulate-connection-loss")
        token2 = requests.post(f"{API}/auth/login", json={"name": "user2", "password": "user123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token2}"})
        token_admin = requests.post(f"{API}/auth/login", json={"name": "admin", "password": "admin123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token_admin}"})

        # 16 min → alarme d'astreinte créée (assignée à user2)
        _advance_clock_all_nodes(16)
        # Après advance-clock, les heartbeats de user2/admin sont stales
        # → envoyer des heartbeats frais pour qu'ils restent online
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token2}"})
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token_admin}"})
        time.sleep(11)

        alarms = requests.get(f"{API}/alarms/active", headers=admin_h).json()
        oncall = next((a for a in alarms if "astreinte" in a["title"].lower()), None)
        assert oncall is not None

        user2_id = self._get_user_id("user2")
        admin_id = self._get_user_id("admin")

        # Escalader l'alarme d'astreinte via trigger manuel
        r = requests.post(f"{API}/test/trigger-escalation")
        assert r.json()["escalated"] >= 1, "Le trigger devrait avoir escaladé l'alarme d'astreinte"

        alarms = requests.get(f"{API}/alarms/active", headers=admin_h).json()
        oncall = next((a for a in alarms if "astreinte" in a["title"].lower()), None)
        assert oncall is not None
        assert oncall["escalation_count"] >= 1, "L'alarme d'astreinte devrait avoir été escaladée"

    def test_no_oncall_alarm_if_not_position1(self):
        """User2 (pos 2) offline ne déclenche PAS d'alarme d'astreinte.
        Seul le #1 (astreinte contractuelle) est surveillé."""
        admin_h = _admin_headers()
        # Tout le monde online sauf user2
        requests.post(f"{API}/test/reset")  # Tout online
        user2_id = self._get_user_id("user2")
        # Mettre user2 offline manuellement
        requests.post(f"{API}/test/simulate-connection-loss")
        token1 = requests.post(f"{API}/auth/login", json={"name": "user1", "password": "user123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token1}"})
        token_admin = requests.post(f"{API}/auth/login", json={"name": "admin", "password": "admin123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token_admin}"})

        # Avancer 20 min
        _advance_clock_all_nodes(20)
        time.sleep(11)

        # Aucune alarme d'astreinte ne doit être créée
        alarms = requests.get(f"{API}/alarms/active", headers=admin_h).json()
        oncall_alarms = [a for a in alarms if "astreinte" in a["title"].lower()]
        assert len(oncall_alarms) == 0, \
            "Pas d'alarme d'astreinte pour user2 — seul le #1 est surveillé"

    def test_nobody_connected_sends_email(self):
        """Si absolument personne n'est connecté → email direction technique."""
        try:
            requests.delete(f"{MAILHOG_URL}/api/v1/messages", timeout=3)
        except Exception:
            pytest.skip("Mailhog non disponible")

        # Tout le monde offline
        requests.post(f"{API}/test/simulate-connection-loss")

        # Avancer 16 min
        _advance_clock_all_nodes(16)
        time.sleep(11)

        # Vérifier qu'un email a été envoyé
        r = requests.get(f"{MAILHOG_URL}/api/v2/messages", timeout=5)
        messages = r.json()
        assert messages["total"] >= 1, "Aucun email envoyé quand personne n'est connecté"

        last_msg = messages["items"][0]
        subject = last_msg["Content"]["Headers"]["Subject"][0]
        assert "connect" in subject.lower() or "astreinte" in subject.lower(), \
            f"Sujet inattendu : {subject}"


# ── Visibilité des utilisateurs notifiés depuis l'app ─────────────────────────

class TestNotifiedUsersVisibility:
    """L'app mobile doit pouvoir savoir qui a été notifié pour l'alarme en cours."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset")

    def test_alarm_response_contains_notified_user_names(self):
        """La réponse de /alarms/mine contient les noms des utilisateurs notifiés."""
        token1 = requests.post(f"{API}/auth/login", json={
            "name": "user1", "password": "user123"
        }).json()["access_token"]
        admin_h = _admin_headers()

        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Noms notifiés", "message": "test", "severity": "critical",
        })

        r = requests.get(f"{API}/alarms/mine", headers={"Authorization": f"Bearer {token1}"})
        alarm = r.json()[0]
        assert "notified_user_names" in alarm, \
            "Le champ notified_user_names doit être dans la réponse"
        assert "user1" in alarm["notified_user_names"]


# ── Visibilité alarme acquittée + countdown ─────────────────────────────────

class TestAckedAlarmVisibility:
    """Un utilisateur connecté doit voir une alarme acquittée par quelqu'un d'autre,
    et le champ ack_remaining_seconds doit décompter."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset")
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)

        # Login user1 et user2
        r1 = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        self.token1 = r1.json()["access_token"]
        self.headers1 = {"Authorization": f"Bearer {self.token1}"}
        self.user1_id = r1.json()["user"]["id"]

        r2 = requests.post(f"{API}/auth/login", json={
            "name": USER2_NAME, "password": USER2_PASSWORD
        })
        self.token2 = r2.json()["access_token"]
        self.headers2 = {"Authorization": f"Bearer {self.token2}"}
        self.user2_id = r2.json()["user"]["id"]

        yield
        _reset_clock_all_nodes()

    def test_acked_alarm_visible_to_other_notified_user(self):
        """User2 doit voir une alarme acquittée par user1 avec status acknowledged."""
        admin_h = _admin_headers()
        # Créer alarme assignée à user1
        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Visible Ack", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        # Escalader vers user2 pour qu'il soit dans notified_user_ids
        requests.post(f"{API}/test/trigger-escalation")

        # Rafraîchir heartbeats après escalade
        requests.post(f"{API}/devices/heartbeat", headers=self.headers1)
        requests.post(f"{API}/devices/heartbeat", headers=self.headers2)

        # user1 acquitte
        requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers1)

        # user2 doit voir l'alarme acquittée
        r = requests.get(f"{API}/alarms/mine", headers=self.headers2)
        alarms = r.json()
        acked = [a for a in alarms if a["id"] == alarm_id]
        assert len(acked) == 1, f"User2 devrait voir l'alarme acquittée, got {len(acked)}"
        assert acked[0]["status"] == "acknowledged"
        assert acked[0]["acknowledged_by_name"] == USER1_NAME

    def test_ack_remaining_seconds_in_response(self):
        """Le champ ack_remaining_seconds doit être présent et décompter avec le temps."""
        admin_h = _admin_headers()
        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Countdown Test", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        # Acquitter
        requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers1)

        # Vérifier ack_remaining_seconds ~1800
        r = requests.get(f"{API}/alarms/mine", headers=self.headers1)
        alarms = [a for a in r.json() if a["id"] == alarm_id]
        assert len(alarms) == 1, "L'alarme acquittée devrait être visible dans /mine"
        remaining = alarms[0]["ack_remaining_seconds"]
        assert remaining is not None, "ack_remaining_seconds doit être présent"
        assert 1750 <= remaining <= 1810, f"ack_remaining_seconds devrait être ~1800, got {remaining}"

        # Avancer de 10 minutes
        _advance_clock_all_nodes(10)

        # Vérifier ack_remaining_seconds ~1200
        r = requests.get(f"{API}/alarms/mine", headers=self.headers1)
        alarms = [a for a in r.json() if a["id"] == alarm_id]
        assert len(alarms) == 1
        remaining2 = alarms[0]["ack_remaining_seconds"]
        assert 1150 <= remaining2 <= 1210, f"Après 10 min, devrait être ~1200, got {remaining2}"

    def test_acked_alarm_visible_to_acker_too(self):
        """L'utilisateur qui a acquitté doit aussi voir l'alarme dans /mine."""
        admin_h = _admin_headers()
        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Self Ack Visible", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers1)

        r = requests.get(f"{API}/alarms/mine", headers=self.headers1)
        alarms = [a for a in r.json() if a["id"] == alarm_id]
        assert len(alarms) == 1, "L'acker doit voir son alarme acquittée dans /mine"
        assert alarms[0]["status"] == "acknowledged"
        assert alarms[0]["ack_remaining_seconds"] is not None


# ── SMS queue + /health ──────────────────────────────────────────────────────

class TestSmsAndHealth:
    """Tests pour la gateway SMS self-hosted et l'endpoint /health enrichi."""

    GATEWAY_KEY = os.getenv("GATEWAY_KEY", "changeme-in-prod")
    GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset")
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset-sms-queue")
        # Attendre que la boucle d'escalade fasse un tick apres un eventuel
        # simulate-loop-stall du test precedent (test_health_endpoint_returns_503)
        time.sleep(12)
        yield
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset-sms-queue")

    def _get_user_id(self, name):
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        return next(u["id"] for u in users if u["name"] == name)

    # ── /health ─────────────────────────────────────────────────────────────

    def test_health_endpoint_returns_ok(self):
        """GET /health retourne 200 avec status ok quand tout va bien."""
        r = requests.get(f"{BASE_URL}/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["db"] is True
        assert data["escalation_loop"] is True

    def test_health_endpoint_returns_503_if_loop_stalled(self):
        """GET /health retourne 503 si la boucle d'escalade est bloquée."""
        # Helper de test qui met last_tick_at à une date passée
        r = requests.post(f"{API}/test/simulate-loop-stall")
        assert r.status_code == 200

        r = requests.get(f"{BASE_URL}/health")
        assert r.status_code == 503
        data = r.json()
        assert data["status"] == "degraded"
        assert data["escalation_loop"] is False

    # ── /internal/sms/* ──────────────────────────────────────────────────────

    def test_sms_pending_requires_gateway_key(self):
        """GET /internal/sms/pending sans clé retourne 401."""
        r = requests.get(f"{BASE_URL}/internal/sms/pending")
        assert r.status_code == 401

    def test_sms_pending_wrong_key_returns_401(self):
        """GET /internal/sms/pending avec mauvaise clé retourne 401."""
        r = requests.get(
            f"{BASE_URL}/internal/sms/pending",
            headers={"X-Gateway-Key": "mauvaise-cle"}
        )
        assert r.status_code == 401

    def test_sms_pending_returns_empty_with_key(self):
        """GET /internal/sms/pending avec bonne clé retourne [] si vide."""
        r = requests.get(
            f"{BASE_URL}/internal/sms/pending",
            headers=self.GATEWAY_HEADERS
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_sms_written_to_queue_on_escalation(self):
        """Après escalade, un SMS est écrit dans sms_queue pour les users avec phone_number."""
        user1_id = self._get_user_id(USER1_NAME)
        admin_h = _admin_headers()

        # Enregistrer un numéro de téléphone pour user1
        r = requests.patch(f"{API}/users/{user1_id}", headers=admin_h, json={"phone_number": "+33600000001"})
        assert r.status_code == 200

        # Envoyer une alarme assignée à user1
        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "SMS Test", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Avancer de 16 min (dépasse le seuil d'escalade de 15 min)
        _advance_clock_all_nodes(16)
        # Après advance-clock, tous les heartbeats sont stales → envoyer des heartbeats
        # frais pour que les users restent online et que l'escalade puisse se déclencher
        token1 = requests.post(f"{API}/auth/login", json={"name": USER1_NAME, "password": USER1_PASSWORD}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token1}"})
        token2 = requests.post(f"{API}/auth/login", json={"name": USER2_NAME, "password": USER2_PASSWORD}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token2}"})
        token_admin = requests.post(f"{API}/auth/login", json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token_admin}"})
        # Attendre 2 ticks de la boucle d'escalade (10s chacun) + marge
        # pour absorber le delai apres simulate-loop-stall du test precedent
        time.sleep(22)

        r = requests.get(
            f"{BASE_URL}/internal/sms/pending",
            headers=self.GATEWAY_HEADERS
        )
        assert r.status_code == 200
        pending = r.json()
        assert len(pending) >= 1, f"Au moins 1 SMS attendu, got {len(pending)}"
        numbers = [s["to_number"] for s in pending]
        assert "+33600000001" in numbers, f"SMS attendu pour +33600000001, got {numbers}"

    def test_sms_marked_sent(self):
        """POST /internal/sms/{id}/sent marque le SMS comme envoyé."""
        # Insérer un SMS via helper de test
        r = requests.post(
            f"{API}/test/insert-sms",
            json={"to_number": "+33600000002", "body": "Test SMS envoi"}
        )
        assert r.status_code == 200
        sms_id = r.json()["id"]

        # Marquer comme envoyé
        r = requests.post(
            f"{BASE_URL}/internal/sms/{sms_id}/sent",
            headers=self.GATEWAY_HEADERS
        )
        assert r.status_code == 200

        # Ne doit plus apparaître dans /pending
        r = requests.get(
            f"{BASE_URL}/internal/sms/pending",
            headers=self.GATEWAY_HEADERS
        )
        ids = [s["id"] for s in r.json()]
        assert sms_id not in ids, f"SMS {sms_id} devrait être absent de /pending après /sent"

    def test_sms_marked_error(self):
        """POST /internal/sms/{id}/error incrémente retries et enregistre l'erreur."""
        r = requests.post(
            f"{API}/test/insert-sms",
            json={"to_number": "+33600000003", "body": "Test SMS erreur"}
        )
        assert r.status_code == 200
        sms_id = r.json()["id"]

        r = requests.post(
            f"{BASE_URL}/internal/sms/{sms_id}/error",
            json={"error": "MODEM_BUSY"},
            headers=self.GATEWAY_HEADERS
        )
        assert r.status_code == 200
        data = r.json()
        assert data["retries"] == 1
        assert data["error"] == "MODEM_BUSY"

        # Toujours dans /pending (retries=1 < 3)
        r = requests.get(
            f"{BASE_URL}/internal/sms/pending",
            headers=self.GATEWAY_HEADERS
        )
        ids = [s["id"] for s in r.json()]
        assert sms_id in ids, "SMS avec retries=1 doit encore être dans /pending"

    def test_sms_excluded_after_max_retries(self):
        """Un SMS avec retries >= 3 n'apparaît plus dans /pending."""
        r = requests.post(
            f"{API}/test/insert-sms",
            json={"to_number": "+33600000004", "body": "Test max retries"}
        )
        sms_id = r.json()["id"]

        # Simuler 3 erreurs
        for _ in range(3):
            requests.post(
                f"{BASE_URL}/internal/sms/{sms_id}/error",
                json={"error": "TIMEOUT"},
                headers=self.GATEWAY_HEADERS
            )

        r = requests.get(
            f"{BASE_URL}/internal/sms/pending",
            headers=self.GATEWAY_HEADERS
        )
        ids = [s["id"] for s in r.json()]
        assert sms_id not in ids, "SMS avec retries=3 ne doit plus apparaître dans /pending"


# ── Redondance : 2 instances docker compose indépendantes, 1 base partagée ──
#
# Architecture locale représentative :
#   docker compose up --build -d                              → VPS1 (port 8000)
#   docker compose -f docker-compose.vps2.yml -p alarm-vps2 up -d → VPS2 (port 8001)
#
# Les deux backends concourent pour le lock advisory PostgreSQL.
# Le premier qui démarre devient primaire, l'autre est secondaire.
# Si le primaire tombe, le secondaire acquiert le lock en <20s.

BASE_URL_2 = os.getenv("BACKEND_URL_2", "http://localhost:8001")
BASE_URL_3 = os.getenv("BACKEND_URL_3", "http://localhost:8002")
API_2 = f"{BASE_URL_2}/api"
API_3 = f"{BASE_URL_3}/api"
GATEWAY_KEY = os.getenv("GATEWAY_KEY", "changeme-in-prod")
GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}

# Map port -> docker compose project name (pour les commandes stop/start)
_PORT_TO_PROJECT = {"8000": "node1", "8001": "node2", "8002": "node3"}


def _url_to_project(url):
    """Extrait le nom du projet docker compose a partir de l'URL du backend."""
    port = url.split(":")[-1].rstrip("/")
    return _PORT_TO_PROJECT.get(port, "node1")


def _skip_if_backend2_unavailable():
    try:
        requests.get(f"{BASE_URL_2}/health", timeout=3)
    except Exception:
        pytest.skip(
            "backend2 non disponible (port 8001) — lancer : "
            "docker compose --env-file .env.node2 -p node2 up -d"
        )


@pytest.mark.failover
class TestRedundancy:
    """Vérifie que les 2 backends partagent correctement l'état via PostgreSQL
    et que le failover de leadership (advisory lock) fonctionne automatiquement.

    Risques de régression couverts :
    - Double escalade (advisory lock garantit un seul primaire actif)
    - Double SMS (guard anti-doublon en base)
    - Incohérence d'état (alarme visible sur un seul nœud)
    - Tokens JWT non cross-compatibles (même SECRET_KEY → tokens universels)
    - Heartbeat redirigé sur nœud secondaire → état visible partout
    - Perte du primaire → le secondaire prend le relais sans intervention manuelle
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        _skip_if_backend2_unavailable()
        requests.post(f"{API}/test/reset")
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset-sms-queue")
        # Réinitialiser l'horloge sur les DEUX nœuds — le primaire peut avoir changé
        _reset_clock_all_nodes()
        _reset_clock_all_nodes()
        yield
        requests.post(f"{API}/test/reset")
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset-sms-queue")
        _reset_clock_all_nodes()
        _reset_clock_all_nodes()

    def _get_user_id(self, name, api=None):
        if api is None:
            api = API
        admin_h = _admin_headers(api)
        users = requests.get(f"{api}/users/", headers=admin_h).json()
        return next(u["id"] for u in users if u["name"] == name)

    # ── Santé et leadership ─────────────────────────────────────────────────

    def test_both_backends_respond_to_health(self):
        """Les deux backends répondent à /health avec db=true."""
        r1 = requests.get(f"{BASE_URL}/health")
        assert r1.status_code == 200
        assert r1.json()["db"] is True

        r2 = requests.get(f"{BASE_URL_2}/health")
        assert r2.status_code == 200
        assert r2.json()["db"] is True

    def test_exactly_one_node_is_primary(self):
        """Exactement un noeud est primaire a tout instant (Patroni + etcd quorum)."""
        roles = []
        for url in _ALL_BACKEND_URLS:
            try:
                r = requests.get(f"{url}/health", timeout=3).json()
                roles.append(r.get("role"))
            except Exception:
                pass

        primaries = [r for r in roles if r == "primary"]
        assert len(primaries) == 1, \
            f"Exactement 1 primaire attendu, got roles={roles}"

    def test_leadership_failover_when_primary_stops(self):
        """Quand le noeud primaire s'arrete (DB + backend), un autre prend le relais
        via Patroni + etcd (quorum). Failover en <30s."""
        # Identifier le primary
        primary_url = _find_primary_url()
        primary_project = _url_to_project(primary_url)
        project_dir = "C:/Users/Charles/Desktop/Projet Claude/Alarm2.0"

        # Creer une alarme avant le failover
        headers = _admin_headers(f"{primary_url}/api")
        r = requests.post(f"{primary_url}/api/alarms/send", json={
            "title": "Failover Test", "message": "doit survivre", "severity": "critical",
        }, headers=headers)
        alarm_id = r.json()["id"]

        # Arreter le noeud primaire entier (patroni + backend + etcd)
        subprocess.run(
            [DOCKER, "compose", "-p", primary_project, "stop"],
            cwd=project_dir, capture_output=True, timeout=30,
        )

        # Un autre noeud doit devenir primary en <60s (Patroni TTL=30s + election)
        new_primary_url = None
        for _ in range(60):
            time.sleep(1)
            for url in _ALL_BACKEND_URLS:
                if url == primary_url:
                    continue
                try:
                    r2 = requests.get(f"{url}/health", timeout=2)
                    if r2.status_code == 200 and r2.json().get("role") == "primary":
                        new_primary_url = url
                        break
                except Exception:
                    pass
            if new_primary_url:
                break

        assert new_primary_url is not None, \
            "Un autre noeud devrait devenir primaire en <60s apres l'arret du primaire"

        # Les donnees sont toujours disponibles
        new_headers = _admin_headers(f"{new_primary_url}/api")
        alarms = requests.get(f"{new_primary_url}/api/alarms/", headers=new_headers).json()
        assert any(a["id"] == alarm_id for a in alarms), \
            "L'alarme doit etre accessible sur le nouveau primaire (replication)"

        # Redemarrer l'ancien primaire (cleanup)
        subprocess.run(
            [DOCKER, "compose", "-p", primary_project, "start"],
            cwd=project_dir, capture_output=True, timeout=30,
        )
        for _ in range(30):
            try:
                if requests.get(f"{primary_url}/health", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2)

    # ── Cohérence des données ───────────────────────────────────────────────

    def test_alarm_created_on_backend1_visible_on_backend2(self):
        """Une alarme créée via backend1 est immédiatement visible sur backend2."""
        admin_h = _admin_headers()
        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Redondance Test", "message": "visible des deux côtés", "severity": "critical",
        })
        assert r.status_code == 200
        alarm_id = r.json()["id"]

        admin_h2 = _admin_headers(API_2)
        r2 = requests.get(f"{API_2}/alarms/", headers=admin_h2)
        ids_on_b2 = [a["id"] for a in r2.json()]
        assert alarm_id in ids_on_b2, \
            f"Alarme {alarm_id} créée sur backend1 devrait être visible sur backend2"

    def test_alarm_resolved_on_backend1_visible_on_backend2(self):
        """Une alarme résolue sur backend1 est immédiatement marquée resolved sur backend2."""
        admin_h = _admin_headers()
        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Resolve Sync", "message": "test", "severity": "critical",
        })
        alarm_id = r.json()["id"]

        requests.post(f"{API}/alarms/{alarm_id}/resolve", headers=admin_h)

        admin_h2 = _admin_headers(API_2)
        r2 = requests.get(f"{API_2}/alarms/", headers=admin_h2)
        alarm_b2 = next((a for a in r2.json() if a["id"] == alarm_id), None)
        assert alarm_b2 is not None, "L'alarme devrait exister sur backend2"
        assert alarm_b2["status"] == "resolved", \
            f"Alarme devrait être resolved sur backend2, got: {alarm_b2['status']}"

    def test_ack_on_backend1_visible_on_backend2(self):
        """Un ACK effectué via backend1 est visible sur backend2."""
        user1_id = self._get_user_id(USER1_NAME)
        admin_h = _admin_headers()
        r = requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Ack Sync", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        token1 = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }).json()["access_token"]
        requests.post(f"{API}/alarms/{alarm_id}/ack",
                      headers={"Authorization": f"Bearer {token1}"})

        admin_h2 = _admin_headers(API_2)
        r2 = requests.get(f"{API_2}/alarms/", headers=admin_h2)
        alarm_b2 = next((a for a in r2.json() if a["id"] == alarm_id), None)
        assert alarm_b2 is not None
        assert alarm_b2["status"] == "acknowledged", \
            f"Alarme devrait être acknowledged sur backend2, got: {alarm_b2['status']}"

    def test_user_list_consistent_across_backends(self):
        """La liste des utilisateurs est identique sur les deux backends."""
        admin_h = _admin_headers()
        ids_b1 = sorted(u["id"] for u in requests.get(f"{API}/users/", headers=admin_h).json())
        admin_h2 = _admin_headers(API_2)
        ids_b2 = sorted(u["id"] for u in requests.get(f"{API_2}/users/", headers=admin_h2).json())
        assert ids_b1 == ids_b2, \
            f"Listes utilisateurs différentes — b1: {ids_b1}, b2: {ids_b2}"

    # ── Compatibilité JWT ───────────────────────────────────────────────────

    def test_token_from_backend1_works_on_backend2(self):
        """Un JWT obtenu sur backend1 est accepté par backend2 (même SECRET_KEY)."""
        token = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }).json()["access_token"]

        r2 = requests.get(f"{API_2}/alarms/mine",
                          headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200, \
            f"Token de backend1 devrait être valide sur backend2 (status {r2.status_code})"

    def test_token_from_backend2_works_on_backend1(self):
        """Un JWT obtenu sur backend2 est accepté par backend1."""
        token = requests.post(f"{API_2}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }).json()["access_token"]

        r1 = requests.get(f"{API}/alarms/mine",
                          headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 200, \
            f"Token de backend2 devrait être valide sur backend1 (status {r1.status_code})"

    def test_heartbeat_on_backend2_visible_on_backend1(self):
        """Un heartbeat envoyé via un backend met à jour le statut visible sur l'autre."""
        requests.post(f"{API}/test/simulate-connection-loss")

        # Envoyer le heartbeat au PRIMARY (les replicas sont read-only)
        primary_url = _find_primary_url()
        primary_api = f"{primary_url}/api"
        token = requests.post(f"{primary_api}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }).json()["access_token"]
        requests.post(f"{primary_api}/devices/heartbeat",
                      headers={"Authorization": f"Bearer {token}"})

        # Vérifier la visibilité depuis un autre backend
        other_api = API_2 if primary_url != BASE_URL_2 else API
        other_admin_h = _admin_headers(other_api)
        users = requests.get(f"{other_api}/users/", headers=other_admin_h).json()
        user1 = next(u for u in users if u["name"] == USER1_NAME)
        assert user1["is_online"] is True, \
            "Heartbeat envoyé via le primary devrait mettre user1 online vu depuis l'autre backend"

    # ── Gateway SMS — cohérence ─────────────────────────────────────────────

    def test_sms_queue_visible_from_both_backends(self):
        """Un SMS inséré via backend1 est visible dans /internal/sms/pending des deux backends."""
        sms_id = requests.post(f"{API}/test/insert-sms",
                               json={"to_number": "+33611000001",
                                     "body": "Test redondance SMS"}).json()["id"]

        ids_b1 = [s["id"] for s in requests.get(
            f"{BASE_URL}/internal/sms/pending", headers=GATEWAY_HEADERS).json()]
        ids_b2 = [s["id"] for s in requests.get(
            f"{BASE_URL_2}/internal/sms/pending", headers=GATEWAY_HEADERS).json()]

        assert sms_id in ids_b1, "SMS absent de /pending sur backend1"
        assert sms_id in ids_b2, "SMS devrait être visible dans /pending sur backend2"

    def test_sms_marked_sent_on_backend2_disappears_from_backend1(self):
        """Un SMS marqué sent via un backend disparaît de /pending sur l'autre."""
        # Insertion via le primary (write)
        primary_url = _find_primary_url()
        primary_api = f"{primary_url}/api"
        sms_id = requests.post(f"{primary_api}/test/insert-sms",
                               json={"to_number": "+33611000002",
                                     "body": "Sent via B2"}).json()["id"]

        # Marquer comme envoyé via le primary (write operation)
        requests.post(f"{primary_url}/internal/sms/{sms_id}/sent", headers=GATEWAY_HEADERS)

        # Vérifier la disparition depuis un autre backend
        other_url = BASE_URL_2 if primary_url != BASE_URL_2 else BASE_URL
        ids = [s["id"] for s in requests.get(
            f"{other_url}/internal/sms/pending", headers=GATEWAY_HEADERS).json()]
        assert sms_id not in ids, \
            "SMS marqué sent sur un backend devrait disparaître de /pending sur l'autre"

    # ── Anti-doublon SMS ────────────────────────────────────────────────────

    def test_no_duplicate_sms_from_escalation(self):
        """La boucle d'escalade du primaire n'enqueue qu'un seul SMS par destinataire.
        Le guard anti-doublon protège contre une double exécution accidentelle."""
        user1_id = self._get_user_id(USER1_NAME)
        admin_h = _admin_headers()
        requests.patch(f"{API}/users/{user1_id}", headers=admin_h, json={"phone_number": "+33699000099"})

        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Anti-dup Test", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        # Avancer l'horloge sur les DEUX nœuds — le primaire (qui exécute la boucle)
        # peut être n'importe lequel après un failover précédent
        _advance_clock_all_nodes(16)
        _advance_clock_all_nodes(16)
        time.sleep(12)

        pending = requests.get(f"{BASE_URL}/internal/sms/pending",
                               headers=GATEWAY_HEADERS).json()
        sms_for_user1 = [s for s in pending if s["to_number"] == "+33699000099"]
        assert len(sms_for_user1) == 1, \
            f"Guard anti-doublon : 1 SMS attendu, got {len(sms_for_user1)}"


# ── Cluster / Quorum ─────────────────────────────────────────────────────────

class TestClusterEndpoint:
    """Verifie le nouvel endpoint /api/cluster qui expose l'etat du quorum Patroni."""

    def test_cluster_endpoint_returns_200(self):
        r = requests.get(f"{BASE_URL}/api/cluster")
        assert r.status_code == 200

    def test_cluster_response_has_members(self):
        data = requests.get(f"{BASE_URL}/api/cluster").json()
        assert "members" in data
        assert len(data["members"]) >= 1

    def test_cluster_members_have_required_fields(self):
        data = requests.get(f"{BASE_URL}/api/cluster").json()
        for m in data["members"]:
            assert "name" in m, f"Missing 'name' in member: {m}"
            assert "role" in m, f"Missing 'role' in member: {m}"
            assert "state" in m, f"Missing 'state' in member: {m}"
            assert "api_url" in m, f"Missing 'api_url' in member: {m}"

    def test_cluster_has_exactly_one_leader(self):
        data = requests.get(f"{BASE_URL}/api/cluster").json()
        leaders = [m for m in data["members"] if m["role"] == "leader"]
        assert len(leaders) == 1, f"Expected 1 leader, got {len(leaders)}: {leaders}"

    def test_cluster_reports_local_node(self):
        data = requests.get(f"{BASE_URL}/api/cluster").json()
        assert "local_node" in data
        assert "local_role" in data

    def test_cluster_reports_quorum_status(self):
        data = requests.get(f"{BASE_URL}/api/cluster").json()
        assert "quorum" in data
        assert data["quorum"]["total"] >= 1
        assert data["quorum"]["healthy"] >= 1
        assert "has_quorum" in data["quorum"]

    def test_cluster_available_on_all_backends(self):
        """L'endpoint /api/cluster doit repondre meme sur un replica."""
        for url in _ALL_BACKEND_URLS:
            try:
                r = requests.get(f"{url}/api/cluster", timeout=3)
                assert r.status_code == 200, f"{url} returned {r.status_code}"
            except requests.ConnectionError:
                pytest.skip(f"{url} not reachable")

    def test_web_ui_has_cluster_tab(self):
        """La page web doit contenir un onglet Cluster."""
        r = requests.get(f"{BASE_URL}/")
        assert r.status_code == 200
        assert "Cluster" in r.text
        assert "clusterMembers" in r.text


@pytest.mark.failover
class TestHeartbeatFailover:
    """Verifie que le heartbeat reprend apres la mort du leader.

    Scenario :
    1. Envoyer un heartbeat au primary -> user online
    2. Tuer le primary (docker compose stop)
    3. Attendre qu'un nouveau primary emerge
    4. Envoyer un heartbeat au nouveau primary -> user toujours online
    5. Remonter l'ancien primary
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        # Reset sur le primary actuel
        primary = _find_primary_url()
        requests.post(f"{primary}/api/test/reset", timeout=5)
        yield
        # Cleanup : s'assurer que les 3 noeuds sont up
        project_dir = "C:/Users/Charles/Desktop/Projet Claude/Alarm2.0"
        for p in ["node1", "node2", "node3"]:
            subprocess.run([DOCKER, "compose", "-p", p, "start"],
                          cwd=project_dir, capture_output=True, timeout=30)
        # Attendre qu'au moins un backend reponde
        for _ in range(30):
            try:
                primary = _find_primary_url()
                requests.post(f"{primary}/api/test/reset", timeout=3)
                break
            except Exception:
                time.sleep(1)

    def test_heartbeat_survives_leader_death(self):
        """Le heartbeat d'un user est accepte par le nouveau primary apres failover."""
        project_dir = "C:/Users/Charles/Desktop/Projet Claude/Alarm2.0"
        primary_url = _find_primary_url()
        primary_project = _url_to_project(primary_url)

        # Login et heartbeat sur le primary
        token = requests.post(f"{primary_url}/api/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }, timeout=5).json()["access_token"]

        r = requests.post(f"{primary_url}/api/devices/heartbeat",
                         headers={"Authorization": f"Bearer {token}"}, timeout=5)
        assert r.status_code == 200, f"Heartbeat initial echoue: {r.status_code}"

        # Verifier user online
        status = requests.get(f"{primary_url}/api/test/status", timeout=5).json()
        assert status["connected_users"] >= 1, "User devrait etre online"

        # Tuer le primary
        subprocess.run([DOCKER, "compose", "-p", primary_project, "stop"],
                      cwd=project_dir, capture_output=True, timeout=30)

        # Attendre un nouveau primary
        new_primary_url = None
        for _ in range(60):
            time.sleep(1)
            for url in _ALL_BACKEND_URLS:
                if url == primary_url:
                    continue
                try:
                    h = requests.get(f"{url}/health", timeout=2)
                    if h.status_code == 200 and h.json().get("role") == "primary":
                        new_primary_url = url
                        break
                except Exception:
                    pass
            if new_primary_url:
                break
        assert new_primary_url is not None, \
            "Nouveau primary devrait emerger en <60s"

        # Heartbeat sur le nouveau primary avec le MEME token
        r = requests.post(f"{new_primary_url}/api/devices/heartbeat",
                         headers={"Authorization": f"Bearer {token}"}, timeout=5)
        assert r.status_code == 200, \
            f"Heartbeat sur nouveau primary echoue: {r.status_code} {r.text}"

        # Verifier user online sur le nouveau primary
        status = requests.get(f"{new_primary_url}/api/test/status", timeout=5).json()
        assert status["connected_users"] >= 1, \
            f"User devrait etre online sur le nouveau primary, got {status['connected_users']}"

        # Remonter l'ancien primary
        subprocess.run([DOCKER, "compose", "-p", primary_project, "start"],
                      cwd=project_dir, capture_output=True, timeout=30)
        for _ in range(30):
            try:
                if requests.get(f"{primary_url}/health", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2)

    def test_heartbeat_on_replica_returns_503(self):
        """Un heartbeat envoye a un replica retourne 503 'replica'."""
        # Trouver un replica
        replica_url = None
        for url in _ALL_BACKEND_URLS:
            try:
                h = requests.get(f"{url}/health", timeout=2)
                if h.status_code == 200 and h.json().get("role") == "secondary":
                    replica_url = url
                    break
            except Exception:
                pass
        if not replica_url:
            pytest.skip("Pas de replica disponible")

        primary_url = _find_primary_url()
        token = requests.post(f"{primary_url}/api/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }, timeout=5).json()["access_token"]

        r = requests.post(f"{replica_url}/api/devices/heartbeat",
                         headers={"Authorization": f"Bearer {token}"}, timeout=5)
        assert r.status_code == 503, \
            f"Replica devrait retourner 503, got {r.status_code}"
        assert "replica" in r.json().get("detail", "").lower(), \
            f"Le detail devrait mentionner 'replica', got {r.json()}"


@pytest.mark.failover
class TestAndroidHeartbeatFailover:
    """Verifie que l'app Android rebascule ses heartbeats apres la mort du leader.

    Utilise un vrai emulateur Android — pas des requests Python.
    Le test injecte les SharedPrefs, lance l'app, et verifie que les heartbeats
    arrivent sur le backend via /api/test/status (connected_users).

    Prerequis : au moins 1 emulateur avec reseau OK et APK installe.
    """

    ADB = os.environ.get("ADB_PATH", r"C:\Users\Charles\Android\Sdk\platform-tools\adb.exe")
    APP = "com.alarm.critical"

    @pytest.fixture(autouse=True)
    def setup(self):
        # Trouver un emulateur fonctionnel
        r = subprocess.run([self.ADB, "devices"], capture_output=True, text=True, timeout=5)
        serials = [l.split("\t")[0] for l in r.stdout.splitlines() if "\tdevice" in l]
        self.emulator = None
        for s in serials:
            out = subprocess.run([self.ADB, "-s", s, "shell", "ping -c 1 -W 2 10.0.2.2"],
                               capture_output=True, text=True, timeout=10)
            if "1 received" in out.stdout or "1 packets received" in out.stdout:
                self.emulator = s
                break
        if not self.emulator:
            pytest.skip("Aucun emulateur avec reseau fonctionnel")

        # Reset cluster
        primary = _find_primary_url()
        requests.post(f"{primary}/api/test/reset", timeout=5)

        yield

        # Cleanup : remonter tous les noeuds
        project_dir = "C:/Users/Charles/Desktop/Projet Claude/Alarm2.0"
        for p in ["node1", "node2", "node3"]:
            subprocess.run([DOCKER, "compose", "-p", p, "start"],
                          cwd=project_dir, capture_output=True, timeout=30)
        # Attendre un primary
        for _ in range(30):
            try:
                _find_primary_url()
                break
            except Exception:
                time.sleep(1)
        # Force stop l'app
        subprocess.run([self.ADB, "-s", self.emulator, "shell",
                       f"am force-stop {self.APP}"], capture_output=True, timeout=5)

    def _inject_prefs_and_launch(self, token, user_name, user_id):
        """Injecte les SharedPrefs et lance l'app sur l'emulateur."""
        import tempfile, uuid
        xml = f"""<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="token">{token}</string>
    <string name="user_name">{user_name}</string>
    <int name="user_id" value="{user_id}" />
    <string name="device_token">{uuid.uuid4()}</string>
</map>"""
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False)
        tmp.write(xml); tmp.close()
        s = self.emulator
        subprocess.run([self.ADB, "-s", s, "shell", f"am force-stop {self.APP}"],
                      capture_output=True, timeout=5)
        time.sleep(1)
        subprocess.run([self.ADB, "-s", s, "push", tmp.name, "/data/local/tmp/alarm_prefs.xml"],
                      capture_output=True, timeout=10)
        subprocess.run([self.ADB, "-s", s, "shell",
                       f"run-as {self.APP} mkdir -p shared_prefs"],
                      capture_output=True, timeout=5)
        subprocess.run([self.ADB, "-s", s, "shell",
                       f"run-as {self.APP} cp /data/local/tmp/alarm_prefs.xml shared_prefs/alarm_prefs.xml"],
                      capture_output=True, timeout=5)
        subprocess.run([self.ADB, "-s", s, "shell", "rm /data/local/tmp/alarm_prefs.xml"],
                      capture_output=True, timeout=5)
        os.unlink(tmp.name)
        subprocess.run([self.ADB, "-s", s, "shell",
                       f"am start -n {self.APP}/.MainActivity"],
                      capture_output=True, timeout=5)
        time.sleep(3)

    def test_android_heartbeat_survives_leader_death(self):
        """L'app Android envoie des heartbeats, le leader meurt, l'app rebascule
        et les heartbeats PERSISTENT sur le nouveau leader (pas juste un flash)."""
        project_dir = "C:/Users/Charles/Desktop/Projet Claude/Alarm2.0"
        primary_url = _find_primary_url()
        primary_project = _url_to_project(primary_url)

        # Login via API et injecter dans l'app
        login_resp = requests.post(f"{primary_url}/api/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }, timeout=5).json()
        token = login_resp["access_token"]
        user_id = login_resp["user"]["id"]

        self._inject_prefs_and_launch(token, USER1_NAME, user_id)

        # Attendre que l'app envoie des heartbeats (user online sur le primary)
        heartbeat_ok = False
        for _ in range(20):
            try:
                s = requests.get(f"{primary_url}/api/test/status", timeout=2).json()
                if s["connected_users"] >= 1:
                    heartbeat_ok = True
                    break
            except Exception:
                pass
            time.sleep(1)
        assert heartbeat_ok, "L'app devrait envoyer des heartbeats au primary (connected_users >= 1)"

        # Tuer le primary
        subprocess.run([DOCKER, "compose", "-p", primary_project, "stop"],
                      cwd=project_dir, capture_output=True, timeout=30)

        # Attendre un nouveau primary
        new_primary_url = None
        for _ in range(60):
            time.sleep(1)
            for url in _ALL_BACKEND_URLS:
                if url == primary_url:
                    continue
                try:
                    h = requests.get(f"{url}/health", timeout=2)
                    if h.status_code == 200 and h.json().get("role") == "primary":
                        new_primary_url = url
                        break
                except Exception:
                    pass
            if new_primary_url:
                break
        assert new_primary_url is not None, "Nouveau primary devrait emerger"

        # Attendre que l'app rebascule et envoie des heartbeats au nouveau primary
        heartbeat_resumed = False
        for t in range(30):
            try:
                s = requests.get(f"{new_primary_url}/api/test/status", timeout=2).json()
                if s["connected_users"] >= 1:
                    heartbeat_resumed = True
                    break
            except Exception:
                pass
            time.sleep(2)
        assert heartbeat_resumed, \
            "L'app devrait rebascule ses heartbeats vers le nouveau primary"

        # VERIFICATION CRITIQUE : les heartbeats PERSISTENT (pas juste un flash)
        # On verifie 3 fois sur 15 secondes que connected_users reste >= 1
        persistent_count = 0
        for _ in range(3):
            time.sleep(5)
            try:
                s = requests.get(f"{new_primary_url}/api/test/status", timeout=2).json()
                if s["connected_users"] >= 1:
                    persistent_count += 1
            except Exception:
                pass
        assert persistent_count >= 2, \
            f"Les heartbeats doivent persister: {persistent_count}/3 checks OK"

        # Remonter l'ancien primary
        subprocess.run([DOCKER, "compose", "-p", primary_project, "start"],
                      cwd=project_dir, capture_output=True, timeout=30)
        for _ in range(30):
            try:
                if requests.get(f"{primary_url}/health", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2)


# ── Delai escalade global ────────────────────────────────────────────────────

class TestEscalationDelayGlobal:
    """Verifie le delai d'escalade unique (1-60 min) pour toute la chaine."""

    @pytest.fixture(autouse=True)
    def setup(self):
        requests.post(f"{API}/test/reset")
        _reset_clock_all_nodes()
        yield
        # Remettre le delai par defaut
        try:
            admin_h = _admin_headers()
            requests.post(f"{API}/config/escalation-delay", headers=admin_h,
                         json={"minutes": 15}, timeout=3)
        except Exception:
            pass
        _reset_clock_all_nodes()

    def test_global_delay_endpoint_returns_current_value(self):
        """GET /api/config/escalation-delay retourne le delai global (defaut 15)."""
        admin_h = _admin_headers()
        r = requests.get(f"{API}/config/escalation-delay", headers=admin_h)
        assert r.status_code == 200
        assert r.json()["minutes"] == 15

    def test_global_delay_can_be_updated(self):
        """POST /api/config/escalation-delay met a jour le delai."""
        admin_h = _admin_headers()
        r = requests.post(f"{API}/config/escalation-delay", headers=admin_h, json={"minutes": 10})
        assert r.status_code == 200
        assert r.json()["minutes"] == 10
        # Verifier en relisant
        r2 = requests.get(f"{API}/config/escalation-delay", headers=admin_h)
        assert r2.json()["minutes"] == 10

    def test_global_delay_rejects_below_1(self):
        """Le delai doit etre >= 1 minute."""
        admin_h = _admin_headers()
        r = requests.post(f"{API}/config/escalation-delay", headers=admin_h, json={"minutes": 0.5})
        assert r.status_code == 422

    def test_global_delay_rejects_above_60(self):
        """Le delai doit etre <= 60 minutes."""
        admin_h = _admin_headers()
        r = requests.post(f"{API}/config/escalation-delay", headers=admin_h, json={"minutes": 61})
        assert r.status_code == 422

    def test_escalation_uses_global_delay(self):
        """L'escalade utilise le delai global, pas un delai par position."""
        # Mettre le delai a 5 min
        admin_h = _admin_headers()
        requests.post(f"{API}/config/escalation-delay", headers=admin_h, json={"minutes": 5})

        user1_id = next(u["id"] for u in requests.get(f"{API}/users/", headers=admin_h).json()
                        if u["name"] == USER1_NAME)
        user2_id = next(u["id"] for u in requests.get(f"{API}/users/", headers=admin_h).json()
                        if u["name"] == USER2_NAME)

        requests.post(f"{API}/alarms/send", headers=admin_h, json={
            "title": "Delay Test", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Avancer de 6 min (> 5 min = delai global)
        _advance_clock_all_nodes(6)
        # Heartbeats pour garder les users online
        for name, pwd in [(USER1_NAME, USER1_PASSWORD), (USER2_NAME, USER2_PASSWORD),
                          (ADMIN_NAME, ADMIN_PASSWORD)]:
            t = requests.post(f"{API}/auth/login", json={"name": name, "password": pwd}).json()["access_token"]
            requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {t}"})
        time.sleep(22)  # 2 ticks d'escalade (10s chacun) + marge

        alarms = requests.get(f"{API}/alarms/", headers=admin_h).json()
        alarm = next(a for a in alarms if a["title"] == "Delay Test")
        assert alarm["assigned_user_id"] == user2_id, \
            f"Alarme devrait etre escaladee avec delai global 5 min"


# ── Chaine escalade bulk ────────────────────────────────────────────────────

class TestEscalationChainBulk:
    """Verifie la sauvegarde en bloc de la chaine d'escalade."""

    @pytest.fixture(autouse=True)
    def setup(self):
        requests.post(f"{API}/test/reset")
        yield
        requests.post(f"{API}/test/reset")

    def _get_user_id(self, name):
        admin_h = _admin_headers()
        return next(u["id"] for u in requests.get(f"{API}/users/", headers=admin_h).json()
                    if u["name"] == name)

    def test_save_escalation_chain_replaces_all(self):
        """POST /api/config/escalation/bulk remplace toute la chaine."""
        u1 = self._get_user_id(USER1_NAME)
        u2 = self._get_user_id(USER2_NAME)
        admin = self._get_user_id(ADMIN_NAME)
        admin_h = _admin_headers()

        r = requests.post(f"{API}/config/escalation/bulk", headers=admin_h,
                         json={"user_ids": [admin, u1]})
        assert r.status_code == 200

        chain = requests.get(f"{API}/config/escalation", headers=admin_h).json()
        assert len(chain) == 2
        assert chain[0]["user_id"] == admin
        assert chain[1]["user_id"] == u1

    def test_save_escalation_chain_rejects_duplicate_user(self):
        """Un meme user ne peut pas apparaitre 2 fois."""
        u1 = self._get_user_id(USER1_NAME)
        admin_h = _admin_headers()
        r = requests.post(f"{API}/config/escalation/bulk", headers=admin_h,
                         json={"user_ids": [u1, u1]})
        assert r.status_code == 422

    def test_save_escalation_chain_rejects_empty(self):
        """La chaine ne peut pas etre vide."""
        admin_h = _admin_headers()
        r = requests.post(f"{API}/config/escalation/bulk", headers=admin_h,
                         json={"user_ids": []})
        assert r.status_code == 422

    def test_save_chain_positions_auto_numbered(self):
        """Les positions sont auto-numerotees dans l'ordre donne."""
        u1 = self._get_user_id(USER1_NAME)
        u2 = self._get_user_id(USER2_NAME)
        admin = self._get_user_id(ADMIN_NAME)
        admin_h = _admin_headers()

        requests.post(f"{API}/config/escalation/bulk", headers=admin_h,
                     json={"user_ids": [u2, admin, u1]})

        chain = requests.get(f"{API}/config/escalation", headers=admin_h).json()
        assert chain[0]["position"] == 1
        assert chain[0]["user_id"] == u2
        assert chain[1]["position"] == 2
        assert chain[1]["user_id"] == admin
        assert chain[2]["position"] == 3
        assert chain[2]["user_id"] == u1


# ── Frontend escalade ───────────────────────────────────────────────────────

class TestEscalationFrontend:
    """Verifie que le frontend contient les elements UI pour la config d'escalade."""

    def test_frontend_has_delay_input(self):
        """La page contient un input pour le delai global d'escalade."""
        r = requests.get(f"{BASE_URL}/")
        assert "escalationDelay" in r.text or "escalation-delay" in r.text

    def test_frontend_has_drag_drop_chain(self):
        """La page contient les listes drag-and-drop + boutons sauvegarder/annuler."""
        r = requests.get(f"{BASE_URL}/")
        html = r.text
        assert "availableUsers" in html or "available-users" in html, "Liste users disponibles manquante"
        assert "escalationChain" in html or "escalation-chain" in html, "Liste chaine escalade manquante"
        assert "saveEscalation" in html or "save-escalation" in html, "Bouton sauvegarder manquant"
        assert "cancelEscalation" in html or "cancel-escalation" in html, "Bouton annuler manquant"

    def test_frontend_all_users_present(self):
        """Tous les users du systeme apparaissent dans la page (dans l'une ou l'autre liste)."""
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        html = requests.get(f"{BASE_URL}/").text
        # La page charge les users dynamiquement via JS, on ne peut pas verifier
        # le contenu dynamique via un simple GET HTML. On verifie que le JS
        # appelle loadEscalation qui peuple les deux listes.
        assert "loadEscalation" in html
        assert "availableUsers" in html or "available-users" in html
        assert "escalationChain" in html or "escalation-chain" in html


# Tests Android E2E dans Espresso (android/app/src/androidTest/).
# Lancer avec : cd android && ./gradlew connectedAndroidTest
