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

BASE_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
API = f"{BASE_URL}/api"

# Noms sans espaces
ADMIN_NAME = "admin"
ADMIN_PASSWORD = "admin123"
USER1_NAME = "user1"
USER1_PASSWORD = "user123"
USER2_NAME = "user2"
USER2_PASSWORD = "user123"


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
        r = requests.get(f"{API}/users/")
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
        users = requests.get(f"{API}/users/").json()
        for u in users:
            if u["name"] == "testcaseuser":
                requests.delete(f"{API}/users/{u['id']}")

        r = requests.post(f"{API}/auth/register", json={
            "name": "TestCaseUser",
            "password": "test123",
        })
        assert r.status_code == 200
        assert r.json()["name"] == "testcaseuser"

        # Nettoyer
        requests.delete(f"{API}/users/{r.json()['id']}")


# ── Alarme unique ────────────────────────────────────────────────────────────

class TestSingleAlarm:
    """Une seule alarme active à la fois."""

    @pytest.fixture(autouse=True)
    def setup(self):
        requests.post(f"{API}/alarms/reset")
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        self.token = r.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.user1_id = r.json()["user"]["id"]

    def test_send_alarm(self):
        """Envoyer une alarme crée bien une alarme active."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Panne serveur",
            "message": "Le serveur ne répond plus",
            "severity": "critical",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_only_one_alarm_at_a_time(self):
        """Envoyer une 2e alarme alors qu'une est active doit être refusé (409)."""
        requests.post(f"{API}/alarms/send", json={
            "title": "Alarme 1", "message": "msg", "severity": "critical",
        })
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Alarme 2", "message": "msg2", "severity": "high",
        })
        assert r.status_code == 409

    def test_can_send_after_resolve(self):
        """Après résolution, on peut envoyer une nouvelle alarme."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Alarme A", "message": "m", "severity": "critical",
        })
        alarm_id = r.json()["id"]
        requests.post(f"{API}/alarms/{alarm_id}/resolve")

        r = requests.post(f"{API}/alarms/send", json={
            "title": "Alarme B", "message": "m", "severity": "critical",
        })
        assert r.status_code == 200
        assert r.json()["title"] == "Alarme B"

    def test_alarm_received_by_user(self):
        """L'utilisateur reçoit l'alarme active via /mine."""
        requests.post(f"{API}/alarms/send", json={
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
        requests.post(f"{API}/test/reset-clock")   # Reset clock FIRST
        requests.post(f"{API}/test/reset")          # Then reset users (heartbeats use clock_now)
        requests.post(f"{API}/alarms/reset")
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        self.token = r.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.user1_id = r.json()["user"]["id"]
        yield
        requests.post(f"{API}/test/reset-clock")

    def test_acknowledge_alarm(self):
        r = requests.post(f"{API}/alarms/send", json={
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
        r = requests.post(f"{API}/alarms/send", json={
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
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Ack Name Test", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        r = requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers)
        assert r.status_code == 200
        assert r.json()["acknowledged_by_name"] == USER1_NAME

    def _find_alarm_by_title(self, title, retries=5):
        """Find an alarm by title with retry logic to handle background loop timing."""
        for attempt in range(retries):
            r = requests.get(f"{API}/alarms/")
            alarm = next((a for a in r.json() if a["title"] == title), None)
            if alarm is not None:
                return alarm
            time.sleep(2)
        return None

    def _ensure_clean_state(self):
        """Ensure no stale alarms exist before proceeding."""
        requests.post(f"{API}/alarms/reset")
        # Verify
        r = requests.get(f"{API}/alarms/")
        if r.json():
            time.sleep(1)
            requests.post(f"{API}/alarms/reset")

    def test_ack_expiry_reactivates_alarm(self):
        """#1 — Après expiration de la suspension (30min), l'alarme redevient active."""
        self._ensure_clean_state()
        r = requests.post(f"{API}/alarms/send", json={
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
        requests.post(f"{API}/test/advance-clock", params={"minutes": 31})
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
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Ack+Esc Test", "message": "m", "severity": "critical",
            "assigned_user_id": self.user1_id,
        })
        alarm_id = r.json()["id"]

        requests.post(f"{API}/alarms/{alarm_id}/ack", headers=self.headers)

        # Avancer de 31 min → réactivation par la boucle d'escalade
        requests.post(f"{API}/test/advance-clock", params={"minutes": 31})
        time.sleep(12)

        # L'alarme doit ne plus être acknowledged (réactivée ou déjà escaladée)
        alarm = self._find_alarm_by_title("Ack+Esc Test")
        assert alarm is not None, "L'alarme 'Ack+Esc Test' a disparu"
        assert alarm["status"] != "acknowledged", \
            f"L'alarme devrait être réactivée, mais est encore '{alarm['status']}'"

    def _get_user_id(self, name):
        users = requests.get(f"{API}/users/").json()
        return next(u["id"] for u in users if u["name"] == name)


# ── Escalade (via trigger manuel — pas de sleep) ────────────────────────────

class TestEscalation:
    """Tests d'escalade déterministes via /test/trigger-escalation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        requests.post(f"{API}/test/reset")  # Remet tout le monde online
        requests.post(f"{API}/alarms/reset")
        requests.post(f"{API}/config/escalation", json={
            "position": 1, "user_id": self._get_user_id(USER1_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", json={
            "position": 2, "user_id": self._get_user_id(USER2_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", json={
            "position": 3, "user_id": self._get_user_id(ADMIN_NAME), "delay_minutes": 15
        })

    def _get_user_id(self, name):
        users = requests.get(f"{API}/users/").json()
        return next(u["id"] for u in users if u["name"] == name)

    def test_escalation_user1_to_user2(self):
        user1_id = self._get_user_id(USER1_NAME)
        user2_id = self._get_user_id(USER2_NAME)

        r = requests.post(f"{API}/alarms/send", json={
            "title": "Escalade", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        r = requests.post(f"{API}/test/trigger-escalation")
        assert r.status_code == 200

        r = requests.get(f"{API}/alarms/")
        alarm = next(a for a in r.json() if a["id"] == alarm_id)
        assert alarm["assigned_user_id"] == user2_id
        assert alarm["escalation_count"] == 1
        assert alarm["status"] == "escalated"

    def test_escalation_chain_user2_to_admin(self):
        user1_id = self._get_user_id(USER1_NAME)
        admin_id = self._get_user_id(ADMIN_NAME)

        requests.post(f"{API}/alarms/send", json={
            "title": "Double escalade", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        requests.post(f"{API}/test/trigger-escalation")
        requests.post(f"{API}/test/trigger-escalation")

        r = requests.get(f"{API}/alarms/")
        alarm = r.json()[0]
        assert alarm["assigned_user_id"] == admin_id
        assert alarm["escalation_count"] == 2

    def test_no_escalation_if_acknowledged(self):
        user1_id = self._get_user_id(USER1_NAME)

        r = requests.post(f"{API}/alarms/send", json={
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

        r = requests.get(f"{API}/alarms/")
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

        requests.post(f"{API}/alarms/send", json={
            "title": "Wrap around", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Escalade 3 fois : user1 → user2 → admin → user1 (wrap)
        requests.post(f"{API}/test/trigger-escalation")  # → user2
        requests.post(f"{API}/test/trigger-escalation")  # → admin
        requests.post(f"{API}/test/trigger-escalation")  # → user1 (wrap)

        r = requests.get(f"{API}/alarms/")
        alarm = r.json()[0]
        assert alarm["assigned_user_id"] == user1_id, \
            f"L'escalade devrait reboucler sur user1 (id={user1_id}), " \
            f"mais elle est sur user_id={alarm['assigned_user_id']}"
        assert alarm["escalation_count"] == 3

    def test_escalation_wrap_continues_cycling(self):
        """Le rebouclage doit continuer indéfiniment (2 tours complets)."""
        user1_id = self._get_user_id(USER1_NAME)
        user2_id = self._get_user_id(USER2_NAME)

        requests.post(f"{API}/alarms/send", json={
            "title": "Multi wrap", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # 4 escalades : user1 → user2 → admin → user1 → user2
        for _ in range(4):
            requests.post(f"{API}/test/trigger-escalation")

        r = requests.get(f"{API}/alarms/")
        alarm = r.json()[0]
        assert alarm["assigned_user_id"] == user2_id, \
            f"Après 4 escalades, devrait être sur user2 (id={user2_id})"
        assert alarm["escalation_count"] == 4


class TestEscalationWithClock:
    """Tests d'escalade avec horloge injectable — vérifie le respect des délais."""

    @pytest.fixture(autouse=True)
    def setup(self):
        requests.post(f"{API}/test/reset-clock")   # Reset clock FIRST
        requests.post(f"{API}/test/reset")          # Then reset users (heartbeats use clock_now)
        requests.post(f"{API}/alarms/reset")
        requests.post(f"{API}/config/escalation", json={
            "position": 1, "user_id": self._get_user_id(USER1_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", json={
            "position": 2, "user_id": self._get_user_id(USER2_NAME), "delay_minutes": 15
        })
        yield
        requests.post(f"{API}/test/reset-clock")

    def _get_user_id(self, name):
        users = requests.get(f"{API}/users/").json()
        return next(u["id"] for u in users if u["name"] == name)

    def _find_alarm_by_title(self, title, retries=3):
        """Find an alarm by title with retry logic to handle background loop timing."""
        for attempt in range(retries):
            r = requests.get(f"{API}/alarms/")
            alarm = next((a for a in r.json() if a["title"] == title), None)
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
        """À 14 min, l'alarme ne doit PAS être escaladée."""
        user1_id = self._get_user_id(USER1_NAME)

        r = requests.post(f"{API}/alarms/send", json={
            "title": "Timing no-esc", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        # Avancer de 14 min → pas encore 15
        requests.post(f"{API}/test/advance-clock", params={"minutes": 14})
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

        r = requests.post(f"{API}/alarms/send", json={
            "title": "Timing esc-16", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        # Avancer de 16 min → dépasse le délai de 15 min
        requests.post(f"{API}/test/advance-clock", params={"minutes": 16})
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

        r = requests.post(f"{API}/alarms/send", json={
            "title": "Boundary-15", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        requests.post(f"{API}/test/advance-clock", params={"minutes": 15})
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

        r = requests.get(f"{API}/users/")
        user = next(u for u in r.json() if u["name"] == USER2_NAME)
        assert user["is_online"] is False

    def test_heartbeat_recent_shows_seconds(self):
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        })
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        requests.post(f"{API}/devices/heartbeat", headers=headers)
        time.sleep(1)

        r = requests.get(f"{API}/users/")
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
        requests.post(f"{API}/alarms/reset")
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
        requests.post(f"{API}/alarms/reset")
        requests.post(f"{API}/test/reset")  # Remet tout le monde online
        requests.post(f"{API}/config/escalation", json={
            "position": 1, "user_id": self._get_user_id(USER1_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", json={
            "position": 2, "user_id": self._get_user_id(USER2_NAME), "delay_minutes": 15
        })
        requests.post(f"{API}/config/escalation", json={
            "position": 3, "user_id": self._get_user_id(ADMIN_NAME), "delay_minutes": 15
        })

    def _get_user_id(self, name):
        users = requests.get(f"{API}/users/").json()
        return next(u["id"] for u in users if u["name"] == name)

    def test_escalation_skips_offline_user(self):
        """Si user2 est offline, l'escalade passe directement à admin."""
        user1_id = self._get_user_id(USER1_NAME)
        user2_id = self._get_user_id(USER2_NAME)
        admin_id = self._get_user_id(ADMIN_NAME)

        # Mettre tout le monde offline
        requests.post(f"{API}/test/simulate-connection-loss")
        # Remettre user1 et admin online (user2 reste offline)
        r = requests.post(f"{API}/auth/login", json={"name": USER1_NAME, "password": USER1_PASSWORD})
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
        r = requests.post(f"{API}/auth/login", json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD})
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {r.json()['access_token']}"})

        # Vérifier que user2 est bien offline avant de continuer
        users = requests.get(f"{API}/users/").json()
        user2 = next(u for u in users if u["name"] == USER2_NAME)
        assert user2["is_online"] is False, \
            f"user2 devrait être offline mais is_online={user2['is_online']}"

        # Envoyer alarme à user1
        requests.post(f"{API}/alarms/send", json={
            "title": "Skip offline", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Escalader → devrait sauter user2 (offline) et aller à admin
        requests.post(f"{API}/test/trigger-escalation")

        r = requests.get(f"{API}/alarms/")
        alarm = r.json()[0]
        assert alarm["assigned_user_id"] == admin_id, \
            f"Devrait sauter user2 (offline) → admin, mais assignée à {alarm['assigned_user_id']}"

    def test_escalation_all_offline_wraps_to_first_online(self):
        """Si tous sauf user1 sont offline, on reste sur user1 (pas de boucle infinie)."""
        user1_id = self._get_user_id(USER1_NAME)

        # Mettre tout le monde offline sauf user1
        requests.post(f"{API}/test/simulate-connection-loss")
        r = requests.post(f"{API}/auth/login", json={"name": USER1_NAME, "password": USER1_PASSWORD})
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {r.json()['access_token']}"})

        requests.post(f"{API}/alarms/send", json={
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
        requests.post(f"{API}/alarms/reset")

    def _get_user_id(self, name):
        users = requests.get(f"{API}/users/").json()
        return next((u["id"] for u in users if u["name"] == name), None)

    def test_alarm_reassigned_when_user_deleted(self):
        """Quand l'utilisateur assigné est supprimé, l'alarme doit être
        réassignée au premier de la chaîne d'escalade."""
        # Supprimer le tempuser s'il existe déjà
        existing_id = self._get_user_id("tempuser")
        if existing_id:
            requests.delete(f"{API}/users/{existing_id}")

        # Créer un utilisateur temporaire
        r = requests.post(f"{API}/auth/register", json={
            "name": "tempuser", "password": "temp123"
        })
        temp_id = r.json()["id"]

        # Envoyer alarme à tempuser
        requests.post(f"{API}/alarms/send", json={
            "title": "Delete test", "message": "m", "severity": "critical",
            "assigned_user_id": temp_id,
        })

        # Supprimer tempuser
        requests.delete(f"{API}/users/{temp_id}")

        # L'alarme doit être réassignée
        r = requests.get(f"{API}/alarms/active")
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
        requests.post(f"{API}/alarms/reset")
        # Reset email config to default
        requests.post(f"{API}/config/system", json={
            "key": "alert_email", "value": "direction_technique@charlesmurgat.com"
        })

    def test_alarm_with_empty_chain_sends_email(self):
        """Quand la chaîne est vide et une alarme est envoyée,
        un email doit être envoyé à l'adresse configurée."""
        # Supprimer toutes les règles d'escalade
        esc = requests.get(f"{API}/config/escalation").json()
        for e in esc:
            requests.delete(f"{API}/config/escalation/{e['id']}")

        # Vérifier que la chaîne est vide
        assert len(requests.get(f"{API}/config/escalation").json()) == 0

        # Envoyer une alarme
        r = requests.post(f"{API}/alarms/send", json={
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
        r = requests.post(f"{API}/config/system", json={
            "key": "alert_email", "value": "custom@example.com"
        })
        assert r.status_code == 200

        r = requests.get(f"{API}/config/system")
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
        r = requests.post(f"{API}/alarms/99999/resolve")
        assert r.status_code == 404

    def test_login_empty_fields(self):
        r = requests.post(f"{API}/auth/login", json={"name": "", "password": ""})
        assert r.status_code in (401, 422)

    def test_send_alarm_missing_fields(self):
        r = requests.post(f"{API}/alarms/send", json={})
        assert r.status_code == 422

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
        # Nettoyer et créer des données
        requests.post(f"{API}/alarms/reset")
        users_before = requests.get(f"{API}/users/").json()
        user_count_before = len(users_before)

        # Créer une alarme
        requests.post(f"{API}/alarms/send", json={
            "title": "Persistence Test", "message": "Survive restart", "severity": "critical",
        })

        # Vérifier avant restart
        alarms = requests.get(f"{API}/alarms/").json()
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
        users_after = requests.get(f"{API}/users/").json()
        assert len(users_after) == user_count_before, \
            f"Utilisateurs perdus : {user_count_before} avant → {len(users_after)} après"

        alarms_after = requests.get(f"{API}/alarms/").json()
        persist_alarm_after = next((a for a in alarms_after if a["title"] == "Persistence Test"), None)
        assert persist_alarm_after is not None, \
            "L'alarme 'Persistence Test' a disparu après le restart"


# ── #3 Email via Mailhog ─────────────────────────────────────────────────────

MAILHOG_URL = os.getenv("MAILHOG_URL", "http://localhost:8025")


class TestEmailViaMailhog:
    """#3 — Vérifier que les emails sont réellement envoyés via SMTP (Mailhog)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        requests.post(f"{API}/alarms/reset")
        # Vider la boîte Mailhog
        try:
            requests.delete(f"{MAILHOG_URL}/api/v1/messages", timeout=3)
        except Exception:
            pytest.skip("Mailhog non disponible")

    def test_empty_chain_sends_real_email(self):
        """Quand la chaîne est vide, un email SMTP doit arriver dans Mailhog."""
        # Supprimer toutes les règles d'escalade
        esc = requests.get(f"{API}/config/escalation").json()
        for e in esc:
            requests.delete(f"{API}/config/escalation/{e['id']}")

        # Envoyer alarme → déclenche l'email
        requests.post(f"{API}/alarms/send", json={
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
        requests.post(f"{API}/alarms/reset")
        requests.post(f"{API}/test/reset-clock")
        requests.post(f"{API}/test/reset")

    def _login(self, name):
        r = requests.post(f"{API}/auth/login", json={"name": name, "password": "user123"})
        return r.json()["access_token"]

    def _get_user_id(self, name):
        users = requests.get(f"{API}/users/").json()
        return next(u["id"] for u in users if u["name"] == name)

    def test_escalated_alarm_still_visible_to_first_user(self):
        """Après escalade user1→user2, user1 doit TOUJOURS voir l'alarme dans /mine."""
        token1 = self._login("user1")
        token2 = self._login("user2")
        h1 = {"Authorization": f"Bearer {token1}"}
        h2 = {"Authorization": f"Bearer {token2}"}

        # Envoyer alarme (va à user1)
        requests.post(f"{API}/alarms/send", json={
            "title": "Cumul Test", "message": "test", "severity": "critical",
        })

        # Vérifier que user1 la voit
        r = requests.get(f"{API}/alarms/mine", headers=h1)
        assert len(r.json()) == 1

        # Escalader vers user2
        requests.post(f"{API}/test/advance-clock?minutes=16")
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

        requests.post(f"{API}/alarms/send", json={
            "title": "Ack Cumul", "message": "test", "severity": "critical",
        })

        # Escalader
        requests.post(f"{API}/test/advance-clock?minutes=16")
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
        requests.post(f"{API}/alarms/send", json={
            "title": "Notif List", "message": "test", "severity": "critical",
        })

        # Avant escalade : seul user1 est notifié
        alarms = requests.get(f"{API}/alarms/").json()
        alarm = next(a for a in alarms if a["title"] == "Notif List")
        assert "notified_user_ids" in alarm, "Le champ notified_user_ids doit exister"
        user1_id = self._get_user_id("user1")
        assert user1_id in alarm["notified_user_ids"]

        # Après escalade : user1 ET user2 (trigger manuel pour fiabilité)
        requests.post(f"{API}/test/trigger-escalation")

        alarms = requests.get(f"{API}/alarms/").json()
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
        requests.post(f"{API}/test/reset-clock")
        requests.post(f"{API}/test/reset")  # Met tout le monde online
        requests.post(f"{API}/alarms/reset")
        # Attendre un cycle du background task avec l'état propre
        time.sleep(12)
        # Re-nettoyer si le background task a créé quelque chose entre temps
        requests.post(f"{API}/alarms/reset")

    def _get_user_id(self, name):
        users = requests.get(f"{API}/users/").json()
        return next(u["id"] for u in users if u["name"] == name)

    def test_oncall_offline_15min_creates_alarm(self):
        """User1 (pos 1) perd son heartbeat. Après 15 min, alarme auto créée."""
        user1_id = self._get_user_id("user1")

        # Mettre user1 offline
        requests.post(f"{API}/test/simulate-connection-loss")
        # Remettre les autres online (seul user1 reste offline)
        token2 = requests.post(f"{API}/auth/login", json={"name": "user2", "password": "user123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token2}"})
        token_admin = requests.post(f"{API}/auth/login", json={"name": "admin", "password": "admin123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token_admin}"})

        # Avancer le temps de 15 min
        requests.post(f"{API}/test/advance-clock?minutes=16")
        time.sleep(11)  # Attendre un tick du watchdog/escalation loop

        # Une alarme automatique doit avoir été créée
        alarms = requests.get(f"{API}/alarms/active").json()
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

        # Mettre user1 offline
        requests.post(f"{API}/test/simulate-connection-loss")
        token2 = requests.post(f"{API}/auth/login", json={"name": "user2", "password": "user123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token2}"})

        # Avancer 16 min → alarme créée
        requests.post(f"{API}/test/advance-clock?minutes=16")
        time.sleep(11)

        alarms = requests.get(f"{API}/alarms/active").json()
        assert any("astreinte" in a["title"].lower() for a in alarms)

        # User1 revient en ligne (heartbeat)
        token1 = requests.post(f"{API}/auth/login", json={"name": "user1", "password": "user123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token1}"})

        # Attendre un tick
        time.sleep(11)

        # L'alarme d'astreinte doit être automatiquement résolue
        alarms = requests.get(f"{API}/alarms/active").json()
        oncall_alarms = [a for a in alarms if "astreinte" in a["title"].lower()]
        assert len(oncall_alarms) == 0, \
            "L'alarme d'astreinte devrait être résolue après reconnexion de user1"

    def test_oncall_alarm_escalates_normally(self):
        """L'alarme d'astreinte suit l'escalade normale (user2 → admin → ...)."""
        # Tout le monde offline sauf user2 et admin
        requests.post(f"{API}/test/simulate-connection-loss")
        token2 = requests.post(f"{API}/auth/login", json={"name": "user2", "password": "user123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token2}"})
        token_admin = requests.post(f"{API}/auth/login", json={"name": "admin", "password": "admin123"}).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token_admin}"})

        # 16 min → alarme d'astreinte créée (assignée à user2)
        requests.post(f"{API}/test/advance-clock?minutes=16")
        time.sleep(11)

        alarms = requests.get(f"{API}/alarms/active").json()
        oncall = next((a for a in alarms if "astreinte" in a["title"].lower()), None)
        assert oncall is not None

        user2_id = self._get_user_id("user2")
        admin_id = self._get_user_id("admin")

        # Escalader l'alarme d'astreinte via trigger manuel
        r = requests.post(f"{API}/test/trigger-escalation")
        assert r.json()["escalated"] >= 1, "Le trigger devrait avoir escaladé l'alarme d'astreinte"

        alarms = requests.get(f"{API}/alarms/active").json()
        oncall = next((a for a in alarms if "astreinte" in a["title"].lower()), None)
        assert oncall is not None
        assert oncall["escalation_count"] >= 1, "L'alarme d'astreinte devrait avoir été escaladée"

    def test_no_oncall_alarm_if_not_position1(self):
        """User2 (pos 2) offline ne déclenche PAS d'alarme d'astreinte.
        Seul le #1 (astreinte contractuelle) est surveillé."""
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
        requests.post(f"{API}/test/advance-clock?minutes=20")
        time.sleep(11)

        # Aucune alarme d'astreinte ne doit être créée
        alarms = requests.get(f"{API}/alarms/active").json()
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
        requests.post(f"{API}/test/advance-clock?minutes=16")
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
        requests.post(f"{API}/alarms/reset")
        requests.post(f"{API}/test/reset")

    def test_alarm_response_contains_notified_user_names(self):
        """La réponse de /alarms/mine contient les noms des utilisateurs notifiés."""
        token1 = requests.post(f"{API}/auth/login", json={
            "name": "user1", "password": "user123"
        }).json()["access_token"]

        requests.post(f"{API}/alarms/send", json={
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
        requests.post(f"{API}/test/reset-clock")
        requests.post(f"{API}/test/reset")
        requests.post(f"{API}/alarms/reset")

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
        requests.post(f"{API}/test/reset-clock")

    def test_acked_alarm_visible_to_other_notified_user(self):
        """User2 doit voir une alarme acquittée par user1 avec status acknowledged."""
        # Créer alarme assignée à user1
        r = requests.post(f"{API}/alarms/send", json={
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
        r = requests.post(f"{API}/alarms/send", json={
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
        requests.post(f"{API}/test/advance-clock", params={"minutes": 10})

        # Vérifier ack_remaining_seconds ~1200
        r = requests.get(f"{API}/alarms/mine", headers=self.headers1)
        alarms = [a for a in r.json() if a["id"] == alarm_id]
        assert len(alarms) == 1
        remaining2 = alarms[0]["ack_remaining_seconds"]
        assert 1150 <= remaining2 <= 1210, f"Après 10 min, devrait être ~1200, got {remaining2}"

    def test_acked_alarm_visible_to_acker_too(self):
        """L'utilisateur qui a acquitté doit aussi voir l'alarme dans /mine."""
        r = requests.post(f"{API}/alarms/send", json={
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
        requests.post(f"{API}/test/reset-clock")
        requests.post(f"{API}/test/reset")
        requests.post(f"{API}/alarms/reset")
        requests.post(f"{API}/test/reset-sms-queue")
        yield
        requests.post(f"{API}/test/reset-clock")
        requests.post(f"{API}/test/reset-sms-queue")

    def _get_user_id(self, name):
        users = requests.get(f"{API}/users/").json()
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

        # Enregistrer un numéro de téléphone pour user1
        r = requests.patch(f"{API}/users/{user1_id}", json={"phone_number": "+33600000001"})
        assert r.status_code == 200

        # Envoyer une alarme assignée à user1
        requests.post(f"{API}/alarms/send", json={
            "title": "SMS Test", "message": "m", "severity": "critical",
            "assigned_user_id": user1_id,
        })

        # Avancer de 16 min (dépasse le seuil d'escalade de 15 min)
        requests.post(f"{API}/test/advance-clock", params={"minutes": 16})
        time.sleep(12)  # Attendre un tick de la boucle d'escalade

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
API_2 = f"{BASE_URL_2}/api"
GATEWAY_KEY = os.getenv("GATEWAY_KEY", "changeme-in-prod")
GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}


def _skip_if_backend2_unavailable():
    try:
        requests.get(f"{BASE_URL_2}/health", timeout=3)
    except Exception:
        pytest.skip(
            "backend2 non disponible (port 8001) — lancer : "
            "docker compose -f docker-compose.vps2.yml -p alarm-vps2 up -d"
        )


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
        requests.post(f"{API}/alarms/reset")
        requests.post(f"{API}/test/reset-sms-queue")
        # Réinitialiser l'horloge sur les DEUX nœuds — le primaire peut avoir changé
        requests.post(f"{API}/test/reset-clock")
        requests.post(f"{API_2}/test/reset-clock")
        yield
        requests.post(f"{API}/test/reset")
        requests.post(f"{API}/alarms/reset")
        requests.post(f"{API}/test/reset-sms-queue")
        requests.post(f"{API}/test/reset-clock")
        requests.post(f"{API_2}/test/reset-clock")

    def _get_user_id(self, name, api=None):
        if api is None:
            api = API
        users = requests.get(f"{api}/users/").json()
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
        """Exactement un nœud est primaire à tout instant (advisory lock exclusif)."""
        r1 = requests.get(f"{BASE_URL}/health").json()
        r2 = requests.get(f"{BASE_URL_2}/health").json()

        roles = [r1.get("role"), r2.get("role")]
        primaries = [r for r in roles if r == "primary"]
        secondaries = [r for r in roles if r == "secondary"]

        assert len(primaries) == 1, \
            f"Exactement 1 primaire attendu, got roles={roles}"
        assert len(secondaries) == 1, \
            f"Exactement 1 secondaire attendu, got roles={roles}"

    def test_leadership_failover_when_primary_stops(self):
        """Quand le nœud primaire s'arrête, le secondaire acquiert le lock en <30s.
        Le lock PostgreSQL est libéré dès que la connexion TCP se ferme — pas besoin
        d'attendre un timeout. Le secondaire sonde toutes les 10s."""
        # Identifier quel nœud est primaire
        h1 = requests.get(f"{BASE_URL}/health").json()
        h2 = requests.get(f"{BASE_URL_2}/health").json()

        if h1.get("role") == "primary":
            primary_url = BASE_URL
            primary_cmd_extra = []   # compose principal = pas de -f ni -p extra
            secondary_url = BASE_URL_2
        else:
            primary_url = BASE_URL_2
            primary_cmd_extra = ["-f", "docker-compose.vps2.yml", "-p", "alarm-vps2"]
            secondary_url = BASE_URL

        project_dir = "C:/Users/Charles/Desktop/Projet Claude/Alarm2.0"

        # Créer une alarme avant le failover
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Failover Test", "message": "doit survivre", "severity": "critical",
        })
        alarm_id = r.json()["id"]

        # Arrêter le primaire (stop = graceful shutdown, connexion DB fermée → lock libéré)
        subprocess.run(
            [DOCKER, "compose"] + primary_cmd_extra + ["stop", "backend"],
            cwd=project_dir, capture_output=True, timeout=30,
        )

        # Le secondaire doit acquérir le lock dans les 25s (cycle = 10s)
        secondary_became_primary = False
        for _ in range(25):
            time.sleep(1)
            try:
                r2 = requests.get(f"{secondary_url}/health", timeout=2)
                if r2.status_code == 200 and r2.json().get("role") == "primary":
                    secondary_became_primary = True
                    break
            except Exception:
                pass

        assert secondary_became_primary, \
            "Le nœud secondaire devrait devenir primaire en <25s après l'arrêt du primaire"

        # Les données sont toujours disponibles depuis le nouveau primaire
        alarms = requests.get(f"{secondary_url}/api/alarms/").json()
        assert any(a["id"] == alarm_id for a in alarms), \
            "L'alarme doit être accessible sur le nouveau primaire (DB partagée)"

        # Redémarrer l'ancien primaire (cleanup — il deviendra secondaire)
        subprocess.run(
            [DOCKER, "compose"] + primary_cmd_extra + ["start", "backend"],
            cwd=project_dir, capture_output=True, timeout=30,
        )
        # Attendre qu'il soit prêt
        for _ in range(20):
            try:
                if requests.get(f"{primary_url}/health", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(2)

    # ── Cohérence des données ───────────────────────────────────────────────

    def test_alarm_created_on_backend1_visible_on_backend2(self):
        """Une alarme créée via backend1 est immédiatement visible sur backend2."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Redondance Test", "message": "visible des deux côtés", "severity": "critical",
        })
        assert r.status_code == 200
        alarm_id = r.json()["id"]

        r2 = requests.get(f"{API_2}/alarms/")
        ids_on_b2 = [a["id"] for a in r2.json()]
        assert alarm_id in ids_on_b2, \
            f"Alarme {alarm_id} créée sur backend1 devrait être visible sur backend2"

    def test_alarm_resolved_on_backend1_visible_on_backend2(self):
        """Une alarme résolue sur backend1 est immédiatement marquée resolved sur backend2."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Resolve Sync", "message": "test", "severity": "critical",
        })
        alarm_id = r.json()["id"]

        requests.post(f"{API}/alarms/{alarm_id}/resolve")

        r2 = requests.get(f"{API_2}/alarms/")
        alarm_b2 = next((a for a in r2.json() if a["id"] == alarm_id), None)
        assert alarm_b2 is not None, "L'alarme devrait exister sur backend2"
        assert alarm_b2["status"] == "resolved", \
            f"Alarme devrait être resolved sur backend2, got: {alarm_b2['status']}"

    def test_ack_on_backend1_visible_on_backend2(self):
        """Un ACK effectué via backend1 est visible sur backend2."""
        user1_id = self._get_user_id(USER1_NAME)
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Ack Sync", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        alarm_id = r.json()["id"]

        token1 = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }).json()["access_token"]
        requests.post(f"{API}/alarms/{alarm_id}/ack",
                      headers={"Authorization": f"Bearer {token1}"})

        r2 = requests.get(f"{API_2}/alarms/")
        alarm_b2 = next((a for a in r2.json() if a["id"] == alarm_id), None)
        assert alarm_b2 is not None
        assert alarm_b2["status"] == "acknowledged", \
            f"Alarme devrait être acknowledged sur backend2, got: {alarm_b2['status']}"

    def test_user_list_consistent_across_backends(self):
        """La liste des utilisateurs est identique sur les deux backends."""
        ids_b1 = sorted(u["id"] for u in requests.get(f"{API}/users/").json())
        ids_b2 = sorted(u["id"] for u in requests.get(f"{API_2}/users/").json())
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
        """Un heartbeat envoyé via backend2 met à jour le statut visible sur backend1."""
        requests.post(f"{API}/test/simulate-connection-loss")

        token = requests.post(f"{API_2}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }).json()["access_token"]
        requests.post(f"{API_2}/devices/heartbeat",
                      headers={"Authorization": f"Bearer {token}"})

        users_b1 = requests.get(f"{API}/users/").json()
        user1 = next(u for u in users_b1 if u["name"] == USER1_NAME)
        assert user1["is_online"] is True, \
            "Heartbeat envoyé via backend2 devrait mettre user1 online vu depuis backend1"

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
        """Un SMS marqué sent via backend2 disparaît de /pending sur backend1."""
        sms_id = requests.post(f"{API}/test/insert-sms",
                               json={"to_number": "+33611000002",
                                     "body": "Sent via B2"}).json()["id"]

        requests.post(f"{BASE_URL_2}/internal/sms/{sms_id}/sent", headers=GATEWAY_HEADERS)

        ids_b1 = [s["id"] for s in requests.get(
            f"{BASE_URL}/internal/sms/pending", headers=GATEWAY_HEADERS).json()]
        assert sms_id not in ids_b1, \
            "SMS marqué sent sur backend2 devrait disparaître de /pending sur backend1"

    # ── Anti-doublon SMS ────────────────────────────────────────────────────

    def test_no_duplicate_sms_from_escalation(self):
        """La boucle d'escalade du primaire n'enqueue qu'un seul SMS par destinataire.
        Le guard anti-doublon protège contre une double exécution accidentelle."""
        user1_id = self._get_user_id(USER1_NAME)
        requests.patch(f"{API}/users/{user1_id}", json={"phone_number": "+33699000099"})

        requests.post(f"{API}/alarms/send", json={
            "title": "Anti-dup Test", "message": "test", "severity": "critical",
            "assigned_user_id": user1_id,
        })
        # Avancer l'horloge sur les DEUX nœuds — le primaire (qui exécute la boucle)
        # peut être n'importe lequel après un failover précédent
        requests.post(f"{API}/test/advance-clock", params={"minutes": 16})
        requests.post(f"{API_2}/test/advance-clock", params={"minutes": 16})
        time.sleep(12)

        pending = requests.get(f"{BASE_URL}/internal/sms/pending",
                               headers=GATEWAY_HEADERS).json()
        sms_for_user1 = [s for s in pending if s["to_number"] == "+33699000099"]
        assert len(sms_for_user1) == 1, \
            f"Guard anti-doublon : 1 SMS attendu, got {len(sms_for_user1)}"


# ── Réplication PostgreSQL streaming ────────────────────────────────────────
#
# Prérequis (une seule fois si la DB existait avant) :
#   docker compose down -v          ← efface pgdata (repart de zéro)
#   docker compose up --build -d    ← crée primaire avec primary_init.sh
#   docker compose -f docker-compose.vps2.yml -p alarm-vps2 up -d
#                                   ← init standby via pg_basebackup + démarre
#
# Architecture locale représentative :
#   VPS1 (port 5432) — PostgreSQL PRIMARY  → réplique vers →
#   VPS2 (port 5433) — PostgreSQL STANDBY  (hot standby = lecture seule)

STANDBY_CONTAINER = os.getenv("STANDBY_CONTAINER", "alarm-vps2-db-standby-1")
VPS2_PROJECT_DIR = "C:/Users/Charles/Desktop/Projet Claude/Alarm2.0"


def _skip_if_standby_unavailable():
    """Skip si db-standby n'est pas démarré."""
    result = subprocess.run(
        [DOCKER, "exec", STANDBY_CONTAINER, "pg_isready", "-U", "alarm", "-q"],
        capture_output=True, timeout=5
    )
    if result.returncode != 0:
        pytest.skip(
            "db-standby non disponible — lancer :\n"
            "  docker compose down -v && docker compose up --build -d\n"
            "  docker compose -f docker-compose.vps2.yml -p alarm-vps2 up -d"
        )


def _psql_standby(sql: str) -> str:
    """Exécute une requête SQL sur db-standby via docker exec psql.
    Retourne le résultat sous forme de chaîne (stripped)."""
    result = subprocess.run(
        [DOCKER, "exec", STANDBY_CONTAINER,
         "psql", "-U", "alarm", "-d", "alarm_db",
         "-t",   # tuples only (no headers)
         "-A",   # no column alignment (clean output)
         "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip()


def _psql_primary(sql: str) -> str:
    """Exécute une requête SQL sur le primaire (VPS1) via docker exec psql."""
    result = subprocess.run(
        [DOCKER, "exec", "alarm20-db-1",
         "psql", "-U", "alarm", "-d", "alarm_db",
         "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )
    return result.stdout.strip()


class TestDatabaseReplication:
    """Vérifie que la réplication streaming PostgreSQL fonctionne entre VPS1 et VPS2.

    Scénarios testés :
    1. Standby est en mode recovery (hot standby) au démarrage
    2. Les données écrites sur le primaire sont répliquées sur le standby en <5s
    3. Les heartbeats et statuts utilisateurs se répliquent
    4. Le standby refuse les écritures directes (protection split-brain)
    5. La promotion transforme le standby en primaire acceptant les écritures
       → inclut le cleanup automatique pour restaurer l'état initial
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        _skip_if_standby_unavailable()
        requests.post(f"{API}/test/reset")
        requests.post(f"{API}/alarms/reset")
        yield
        requests.post(f"{API}/test/reset")
        requests.post(f"{API}/alarms/reset")

    # ── État du standby ─────────────────────────────────────────────────────

    def test_standby_is_in_recovery_mode(self):
        """Le standby doit être en hot_standby (pg_is_in_recovery = true)."""
        result = _psql_standby("SELECT pg_is_in_recovery()")
        assert result == "t", \
            f"db-standby devrait être en mode recovery, got: {result!r}"

    def test_primary_is_not_in_recovery(self):
        """Le primaire NE doit PAS être en recovery."""
        result = _psql_primary("SELECT pg_is_in_recovery()")
        assert result == "f", \
            f"db (primaire) ne devrait pas être en recovery, got: {result!r}"

    # ── Réplication des données ─────────────────────────────────────────────

    def test_alarm_replicates_to_standby(self):
        """Une alarme créée via le backend est répliquée sur le standby en <5s."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Replication Test", "message": "test", "severity": "critical",
        })
        alarm_id = r.json()["id"]

        # Attendre la réplication (en local : quasi-instantané, 3s large marge)
        time.sleep(3)

        count = _psql_standby(f"SELECT COUNT(*) FROM alarms WHERE id = {alarm_id}")
        assert count == "1", \
            f"Alarme {alarm_id} devrait être répliquée sur le standby (count={count})"

    def test_alarm_resolution_replicates(self):
        """La résolution d'une alarme est répliquée sur le standby."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Resolve Replication", "message": "test", "severity": "critical",
        })
        alarm_id = r.json()["id"]
        requests.post(f"{API}/alarms/{alarm_id}/resolve")

        time.sleep(3)

        status = _psql_standby(f"SELECT status FROM alarms WHERE id = {alarm_id}")
        assert status == "resolved", \
            f"Statut résolu devrait être répliqué, got: {status!r}"

    def test_heartbeat_replicates_to_standby(self):
        """Un heartbeat utilisateur est répliqué sur le standby."""
        token = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD
        }).json()["access_token"]
        requests.post(f"{API}/devices/heartbeat",
                      headers={"Authorization": f"Bearer {token}"})

        time.sleep(3)

        result = _psql_standby("SELECT is_online FROM users WHERE name = 'user1'")
        assert result == "t", \
            f"is_online de user1 devrait être répliqué sur le standby, got: {result!r}"

    def test_replication_lag_is_negligible(self):
        """Le lag de réplication est < 5s pour 5 écritures consécutives."""
        # Créer 5 alarmes rapidement
        ids = []
        for i in range(5):
            r = requests.post(f"{API}/alarms/send", json={
                "title": f"Lag Test {i}", "message": "test", "severity": "critical",
            })
            # Résoudre aussitôt pour éviter la contrainte alarme unique
            requests.post(f"{API}/alarms/{r.json()['id']}/resolve")
            ids.append(r.json()["id"])

        time.sleep(5)  # 5s de marge

        count_primary = int(_psql_primary("SELECT COUNT(*) FROM alarms"))
        count_standby = int(_psql_standby("SELECT COUNT(*) FROM alarms"))

        assert count_standby == count_primary, \
            f"Standby ({count_standby}) devrait avoir autant d'alarmes que le primaire ({count_primary})"

    # ── Protection write-reject ─────────────────────────────────────────────

    def test_standby_rejects_direct_writes(self):
        """Le standby refuse les écritures directes — protection contre le split-brain."""
        result = subprocess.run(
            [DOCKER, "exec", STANDBY_CONTAINER,
             "psql", "-U", "alarm", "-d", "alarm_db", "-t", "-A", "-c",
             "INSERT INTO alarms (title, message, severity, notified_user_ids) "
             "VALUES ('Direct Write', 'test', 'critical', '')"],
            capture_output=True, text=True, timeout=10,
        )
        error_output = (result.stdout + result.stderr).lower()
        assert result.returncode != 0, \
            "L'écriture directe sur le standby devrait échouer (read-only)"
        assert "read-only" in error_output or "recovery" in error_output, \
            f"Erreur attendue (read-only/recovery), got: {error_output[:200]}"

    # ── Promotion et failover ───────────────────────────────────────────────

    def test_promotion_promotes_standby_to_primary(self):
        """Après pg_ctl promote, le standby devient primaire et accepte les écritures.
        Cleanup automatique : restaure le standby à son état initial après le test."""
        # 1. Arrêter le primaire
        subprocess.run(
            [DOCKER, "compose", "stop", "db"],
            cwd=VPS2_PROJECT_DIR, capture_output=True, timeout=30,
        )

        try:
            # 2. Promouvoir le standby
            result = subprocess.run(
                [DOCKER, "exec", STANDBY_CONTAINER,
                 "su-exec", "postgres",
                 "pg_ctl", "promote", "-D", "/var/lib/postgresql/data"],
                capture_output=True, text=True, timeout=15,
            )
            assert result.returncode == 0, \
                f"pg_ctl promote a échoué : {result.stderr}"

            # 3. Attendre que la promotion soit effective
            time.sleep(3)

            # 4. Vérifier que le standby n'est plus en recovery
            is_recovery = _psql_standby("SELECT pg_is_in_recovery()")
            assert is_recovery == "f", \
                f"Après promotion, pg_is_in_recovery devrait être false, got: {is_recovery!r}"

            # 5. Vérifier qu'on peut écrire sur le standby promu
            write_result = subprocess.run(
                [DOCKER, "exec", STANDBY_CONTAINER,
                 "psql", "-U", "alarm", "-d", "alarm_db", "-t", "-A", "-c",
                 "INSERT INTO alarms (title, message, severity, notified_user_ids) "
                 "VALUES ('Post-Promotion Write', 'promoted', 'critical', '') "
                 "RETURNING id"],
                capture_output=True, text=True, timeout=10,
            )
            assert write_result.returncode == 0, \
                f"Écriture après promotion devrait réussir, stderr: {write_result.stderr}"
            # La sortie contient l'id retourné + "INSERT 0 1" — vérifier qu'on a bien un entier
            first_line = write_result.stdout.strip().splitlines()[0]
            assert first_line.strip().isdigit(), \
                f"RETURNING id devrait retourner un entier, got: {write_result.stdout.strip()!r}"

        finally:
            # ── Cleanup : restaurer le standby à son état initial ──────────
            # 6. Redémarrer le primaire
            subprocess.run(
                [DOCKER, "compose", "start", "db"],
                cwd=VPS2_PROJECT_DIR, capture_output=True, timeout=30,
            )

            # 7. Attendre que le primaire soit prêt
            for _ in range(30):
                try:
                    if requests.get(f"{BASE_URL}/health", timeout=2).status_code == 200:
                        break
                except Exception:
                    pass
                time.sleep(2)

            # 8. Arrêter et supprimer le standby + son volume pour repartir proprement
            subprocess.run(
                [DOCKER, "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2",
                 "stop", "db-standby"],
                cwd=VPS2_PROJECT_DIR, capture_output=True, timeout=30,
            )
            subprocess.run(
                [DOCKER, "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2",
                 "rm", "-f", "db-standby", "db-standby-init"],
                cwd=VPS2_PROJECT_DIR, capture_output=True, timeout=15,
            )
            subprocess.run(
                [DOCKER, "volume", "rm", "alarm-vps2_pgdata_standby"],
                capture_output=True, timeout=10,
            )

            # 9. Relancer VPS2 — le standby se ré-initialise via pg_basebackup
            subprocess.run(
                [DOCKER, "compose", "-f", "docker-compose.vps2.yml", "-p", "alarm-vps2",
                 "up", "-d"],
                cwd=VPS2_PROJECT_DIR, capture_output=True, timeout=120,
            )

            # 10. Attendre que le standby et le backend VPS2 soient prêts
            for _ in range(40):
                result = subprocess.run(
                    [DOCKER, "exec", STANDBY_CONTAINER, "pg_isready", "-U", "alarm", "-q"],
                    capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    break
                time.sleep(3)


# Tests Android E2E dans Espresso (android/app/src/androidTest/).
# Lancer avec : cd android && ./gradlew connectedAndroidTest
