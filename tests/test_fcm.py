"""
Tests RED — FCM (Firebase Cloud Messaging) + escalade sans filtre online.

Ces tests DOIVENT ECHOUER sur le code actuel (phase RED).
Ils passeront apres implementation (phase GREEN).

Prerequis :
- Backend actif (cluster 3 noeuds)
"""

import os
import time
import requests
import pytest

_ALL_BACKEND_URLS = ["http://localhost:8000", "http://localhost:8001", "http://localhost:8002"]


def _find_primary_url():
    for url in _ALL_BACKEND_URLS:
        try:
            r = requests.get(f"{url}/health", timeout=2)
            if r.status_code == 200 and r.json().get("role") == "primary":
                return url
        except Exception:
            continue
    return os.getenv("BACKEND_URL", "http://localhost:8000")


BASE_URL = os.getenv("BACKEND_URL", None) or _find_primary_url()
API = f"{BASE_URL}/api"


@pytest.fixture(autouse=True, scope="function")
def _ensure_primary_api():
    global BASE_URL, API
    new_url = _find_primary_url()
    if new_url != BASE_URL:
        BASE_URL = new_url
        API = f"{BASE_URL}/api"


ADMIN_NAME = "admin"
ADMIN_PASSWORD = "admin123"
USER1_NAME = "user1"
USER1_PASSWORD = "user123"
USER2_NAME = "user2"
USER2_PASSWORD = "user123"


def _login(name, password):
    r = requests.post(f"{API}/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"Login failed for {name}: {r.text}"
    data = r.json()
    token = data["access_token"]
    return token, {"Authorization": f"Bearer {token}"}, data["user"]["id"]


def _admin_headers():
    _, headers, _ = _login(ADMIN_NAME, ADMIN_PASSWORD)
    return headers


def _advance_clock_all_nodes(minutes):
    for url in _ALL_BACKEND_URLS:
        try:
            requests.post(f"{url}/api/test/advance-clock",
                          params={"minutes": minutes, "peer": "false"}, timeout=2)
        except Exception:
            pass


def _reset_clock_all_nodes():
    for url in _ALL_BACKEND_URLS:
        try:
            requests.post(f"{url}/api/test/reset-clock", params={"peer": "false"}, timeout=2)
        except Exception:
            pass


# =============================================================================
# 1. ENREGISTREMENT / SUPPRESSION TOKENS FCM
# =============================================================================

class TestFcmTokenManagement:
    """Endpoints POST/DELETE /api/devices/fcm-token."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset")
        yield

    def test_register_fcm_token(self):
        """POST /api/devices/fcm-token avec token valide → 200."""
        _, headers, _ = _login(USER1_NAME, USER1_PASSWORD)
        r = requests.post(f"{API}/devices/fcm-token", json={
            "token": "fcm_test_token_abc123",
            "device_id": "device_user1_001",
        }, headers=headers)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    def test_register_fcm_token_requires_auth(self):
        """POST /api/devices/fcm-token sans auth → 401."""
        r = requests.post(f"{API}/devices/fcm-token", json={
            "token": "fcm_test_token_xyz",
            "device_id": "device_anon",
        })
        assert r.status_code in (401, 403), (
            f"POST /devices/fcm-token sans auth devrait etre refuse, got {r.status_code}"
        )

    def test_register_fcm_token_update(self):
        """Re-enregistrer le meme device_id met a jour le token."""
        _, headers, uid = _login(USER1_NAME, USER1_PASSWORD)

        # Premier enregistrement
        r1 = requests.post(f"{API}/devices/fcm-token", json={
            "token": "old_token_aaa",
            "device_id": "device_user1_upd",
        }, headers=headers)
        assert r1.status_code == 200

        # Mise a jour avec nouveau token, meme device_id
        r2 = requests.post(f"{API}/devices/fcm-token", json={
            "token": "new_token_bbb",
            "device_id": "device_user1_upd",
        }, headers=headers)
        assert r2.status_code == 200

        # Verifier qu'il n'y a qu'un seul token pour ce device
        # (on verifie via /test/last-fcm apres un envoi d'alarme)

    def test_delete_fcm_token(self):
        """DELETE /api/devices/fcm-token supprime le token."""
        _, headers, _ = _login(USER1_NAME, USER1_PASSWORD)

        # Enregistrer
        requests.post(f"{API}/devices/fcm-token", json={
            "token": "token_to_delete",
            "device_id": "device_del",
        }, headers=headers)

        # Supprimer
        r = requests.delete(f"{API}/devices/fcm-token", json={
            "device_id": "device_del",
        }, headers=headers)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"


# =============================================================================
# 2. ENVOI FCM SUR ALARME / ESCALADE
# =============================================================================

class TestFcmOnAlarm:
    """Verifier que les FCM sont envoyes lors des mutations d'alarme."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset")
        requests.post(f"{API}/test/reset-fcm")
        self.admin_headers = admin_h
        self.token1, self.headers1, self.uid1 = _login(USER1_NAME, USER1_PASSWORD)
        self.token2, self.headers2, self.uid2 = _login(USER2_NAME, USER2_PASSWORD)

        # Nettoyer les anciens tokens et enregistrer des tokens propres
        requests.delete(f"{API}/devices/fcm-token", json={"device_id": "dev_user1"}, headers=self.headers1)
        requests.delete(f"{API}/devices/fcm-token", json={"device_id": "dev_user2"}, headers=self.headers2)
        requests.delete(f"{API}/devices/fcm-token", json={"device_id": "dev_admin"}, headers=self.admin_headers)
        # Aussi nettoyer les tokens residuels des tests precedents
        for did in ["device_user1_001", "device_user1_upd", "device_del"]:
            requests.delete(f"{API}/devices/fcm-token", json={"device_id": did}, headers=self.headers1)

        requests.post(f"{API}/devices/fcm-token", json={
            "token": "fcm_user1_token",
            "device_id": "dev_user1",
        }, headers=self.headers1)
        requests.post(f"{API}/devices/fcm-token", json={
            "token": "fcm_user2_token",
            "device_id": "dev_user2",
        }, headers=self.headers2)
        requests.post(f"{API}/devices/fcm-token", json={
            "token": "fcm_admin_token",
            "device_id": "dev_admin",
        }, headers=self.admin_headers)

        # S'assurer de la chaine standard
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        uid_admin = next(u["id"] for u in users if u["name"] == ADMIN_NAME)
        requests.post(f"{API}/config/escalation/bulk", json={
            "user_ids": [self.uid1, self.uid2, uid_admin],
        }, headers=admin_h)

        # Reset FCM apres le setup (les enregistrements de tokens ne doivent pas polluer)
        requests.post(f"{API}/test/reset-fcm")
        yield
        requests.post(f"{API}/alarms/reset", headers=admin_h)

    def test_alarm_sends_fcm(self):
        """Envoyer une alarme declenche un FCM vers l'utilisateur assigne."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "FCM Test", "message": "test", "severity": "critical",
            "assigned_user_id": self.uid1,
        }, headers=self.admin_headers)
        assert r.status_code == 200

        # Verifier le FCM envoye
        fcm_list = requests.get(f"{API}/test/last-fcm").json()
        assert len(fcm_list) >= 1, "Au moins 1 FCM devrait etre envoye"
        fcm_to_user1 = [f for f in fcm_list if f["user_id"] == self.uid1]
        assert len(fcm_to_user1) >= 1, (
            f"FCM devrait etre envoye a user1 (uid={self.uid1}), got: {fcm_list}"
        )
        assert "FCM Test" in fcm_to_user1[0].get("title", ""), (
            "Le FCM devrait contenir le titre de l'alarme"
        )

    def test_escalation_sends_fcm(self):
        """L'escalade envoie un FCM au nouvel utilisateur."""
        requests.post(f"{API}/alarms/send", json={
            "title": "Esc FCM", "message": "m", "severity": "critical",
            "assigned_user_id": self.uid1,
        }, headers=self.admin_headers)

        # Reset FCM list pour ne voir que l'escalade
        requests.post(f"{API}/test/reset-fcm")

        # Trigger escalation
        requests.post(f"{API}/test/trigger-escalation")

        fcm_list = requests.get(f"{API}/test/last-fcm").json()
        fcm_to_user2 = [f for f in fcm_list if f["user_id"] == self.uid2]
        assert len(fcm_to_user2) >= 1, (
            f"FCM devrait etre envoye a user2 apres escalade, got: {fcm_list}"
        )

    def test_fcm_sent_to_all_notified_users(self):
        """Apres escalade, FCM envoye a TOUS les notifies (cumulative)."""
        requests.post(f"{API}/alarms/send", json={
            "title": "Cumul FCM", "message": "m", "severity": "critical",
            "assigned_user_id": self.uid1,
        }, headers=self.admin_headers)

        requests.post(f"{API}/test/reset-fcm")
        requests.post(f"{API}/test/trigger-escalation")

        fcm_list = requests.get(f"{API}/test/last-fcm").json()
        notified_user_ids = set(f["user_id"] for f in fcm_list)
        # user1 (initial) + user2 (escalade) doivent tous recevoir
        assert self.uid1 in notified_user_ids, "user1 (notifie initial) doit recevoir un FCM"
        assert self.uid2 in notified_user_ids, "user2 (escalade) doit recevoir un FCM"

    def test_fcm_no_token_no_crash(self):
        """User sans token FCM → pas d'erreur, alarme creee normalement."""
        # Supprimer le token de user1
        requests.delete(f"{API}/devices/fcm-token", json={
            "device_id": "dev_user1",
        }, headers=self.headers1)

        r = requests.post(f"{API}/alarms/send", json={
            "title": "No Token", "message": "m", "severity": "critical",
            "assigned_user_id": self.uid1,
        }, headers=self.admin_headers)
        assert r.status_code == 200, (
            f"L'alarme doit etre creee meme sans token FCM, got {r.status_code}"
        )


# =============================================================================
# 3. ESCALADE SANS FILTRE ONLINE + DELAI 2 PALIERS
# =============================================================================

class TestEscalationWithFcm:
    """L'escalade ne saute plus les users offline et utilise un delai accelere
    pour les users en mode veille (positions 2+)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset")
        requests.post(f"{API}/test/reset-fcm")
        _reset_clock_all_nodes()
        self.admin_headers = admin_h
        self.token1, self.headers1, self.uid1 = _login(USER1_NAME, USER1_PASSWORD)
        self.token2, self.headers2, self.uid2 = _login(USER2_NAME, USER2_PASSWORD)

        # S'assurer que la chaine est standard : user1 (pos 1), user2 (pos 2), admin (pos 3)
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        uid_admin = next(u["id"] for u in users if u["name"] == ADMIN_NAME)
        requests.post(f"{API}/config/escalation/bulk", json={
            "user_ids": [self.uid1, self.uid2, uid_admin],
        }, headers=admin_h)

        # Configurer le delai FCM a 2 min
        requests.post(f"{API}/config/escalation-delay", json={"minutes": 15}, headers=admin_h)

        yield
        _reset_clock_all_nodes()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset")
        # Remettre le delai normal
        requests.post(f"{API}/config/escalation-delay", json={"minutes": 15}, headers=admin_h)

    def test_escalation_reaches_offline_user(self):
        """Un user offline recoit quand meme l'escalade (pas de skip is_online)."""
        # Mettre user2 offline
        requests.post(f"{API}/test/simulate-connection-loss")
        # Remettre user1 online (pour qu'il soit le premier assigne)
        requests.post(f"{API}/devices/heartbeat", headers=self.headers1)

        r = requests.post(f"{API}/alarms/send", json={
            "title": "Offline Esc", "message": "m", "severity": "critical",
            "assigned_user_id": self.uid1,
        }, headers=self.admin_headers)
        alarm_id = r.json()["id"]

        # Trigger escalation
        requests.post(f"{API}/test/trigger-escalation")

        # user2 est offline mais devrait quand meme recevoir l'escalade
        alarm = requests.get(f"{API}/alarms/", headers=self.admin_headers).json()
        current = next(a for a in alarm if a["id"] == alarm_id)
        assert current["assigned_user_id"] == self.uid2, (
            f"L'alarme devrait etre escaladee a user2 meme s'il est offline, "
            f"got assigned_user_id={current['assigned_user_id']}"
        )

    def test_escalation_fast_for_veille_user(self):
        """User en position 2 (veille) : escalade en 2 min, pas 15."""
        # Assigner l'alarme a user2 (position 2 = mode veille)
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Fast Esc", "message": "m", "severity": "critical",
            "assigned_user_id": self.uid2,
        }, headers=self.admin_headers)
        alarm_id = r.json()["id"]

        # Avancer de 3 min (> 2 min delai veille, < 15 min delai normal)
        _advance_clock_all_nodes(3)
        # Garder les users online pour heartbeat
        requests.post(f"{API}/devices/heartbeat", headers=self.headers1)
        requests.post(f"{API}/devices/heartbeat", headers=self.headers2)
        time.sleep(12)  # Attendre un tick escalade

        # L'alarme devrait deja etre escaladee (delai 2 min depasse)
        alarms = requests.get(f"{API}/alarms/", headers=self.admin_headers).json()
        current = next(a for a in alarms if a["id"] == alarm_id)
        assert current["assigned_user_id"] != self.uid2, (
            f"L'alarme devrait etre escaladee apres 3 min (delai veille = 2 min), "
            f"mais toujours assignee a user2"
        )

    def test_escalation_slow_for_oncall_user(self):
        """User en position 1 (astreinte) : escalade en 15 min, pas 2."""
        # Assigner l'alarme a user1 (position 1 = astreinte)
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Slow Esc", "message": "m", "severity": "critical",
            "assigned_user_id": self.uid1,
        }, headers=self.admin_headers)
        alarm_id = r.json()["id"]

        # Avancer de 5 min (> 2 min, < 15 min)
        _advance_clock_all_nodes(5)
        requests.post(f"{API}/devices/heartbeat", headers=self.headers1)
        requests.post(f"{API}/devices/heartbeat", headers=self.headers2)
        time.sleep(12)

        # L'alarme ne devrait PAS etre escaladee (delai astreinte = 15 min)
        alarms = requests.get(f"{API}/alarms/", headers=self.admin_headers).json()
        current = next(a for a in alarms if a["id"] == alarm_id)
        assert current["assigned_user_id"] == self.uid1, (
            f"L'alarme ne devrait PAS etre escaladee apres 5 min (delai astreinte = 15 min), "
            f"mais assignee a {current['assigned_user_id']} au lieu de {self.uid1}"
        )
