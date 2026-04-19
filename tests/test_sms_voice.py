"""
Tests E2E pour le pipeline SMS/Call timer-based et acquittement par telephone.

Phase RED : ces tests echouent tant que le backend n'implemente pas :
- CallQueue model + endpoints /internal/calls/*
- Timer-based SMS/call (sms_sent/call_sent dans AlarmNotification)
- Endpoint POST /internal/alarms/active/ack-by-phone
- SystemConfig sms_call_delay_minutes

Lancer avec : python -m pytest tests/test_sms_voice.py -v
Prerequis : backend actif sur http://localhost:8000 (cluster 3 noeuds)
"""

import os
import time
import requests
import pytest

_ALL_BACKEND_URLS = ["http://localhost:8000", "http://localhost:8001", "http://localhost:8002"]


def _test_endpoint_urls():
    """Backends qui doivent recevoir les ordres /api/test/* (reset, clock).

    En CI single-node : BACKEND_URL set → cibler ce seul backend (les 3 ports
    historiques n'existent pas en CI et le `except: pass` masquait l'echec).
    En dev 3-noeuds : BACKEND_URL absent → liste historique.
    Cf meme pattern dans test_e2e.py:_test_endpoint_urls().
    """
    env_url = os.getenv("BACKEND_URL")
    if env_url:
        return [env_url.rstrip("/")]
    return _ALL_BACKEND_URLS


def _find_primary_url():
    for url in _test_endpoint_urls():
        try:
            r = requests.get(f"{url}/health", timeout=2)
            if r.status_code == 200 and r.json().get("role") == "primary":
                return url
        except Exception:
            continue
    return os.getenv("BACKEND_URL", "http://localhost:8000")


BASE_URL = os.getenv("BACKEND_URL", None) or _find_primary_url()
API = f"{BASE_URL}/api"

ADMIN_NAME = "admin"
ADMIN_PASSWORD = "admin123"
USER1_NAME = "user1"
USER1_PASSWORD = "user123"
USER2_NAME = "user2"
USER2_PASSWORD = "user123"

GATEWAY_KEY = os.getenv("GATEWAY_KEY", "changeme-in-prod")
GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}

TICK_WAIT = 22  # 2 ticks escalation loop (10s chacun) + marge


def _reset_clock_all_nodes():
    for url in _test_endpoint_urls():
        try:
            requests.post(f"{url}/api/test/reset-clock", params={"peer": "false"}, timeout=2)
        except Exception:
            pass


def _advance_clock_all_nodes(minutes):
    for url in _test_endpoint_urls():
        try:
            requests.post(f"{url}/api/test/advance-clock",
                         params={"minutes": minutes, "peer": "false"}, timeout=2)
        except Exception:
            pass


def _login(name, password):
    r = requests.post(f"{API}/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"Login failed for {name}: {r.text}"
    data = r.json()
    return data["access_token"], {"Authorization": f"Bearer {data['access_token']}"}, data["user"]["id"]


def _admin_headers():
    _, headers, _ = _login(ADMIN_NAME, ADMIN_PASSWORD)
    return headers


def _refresh_all_heartbeats():
    """Envoie un heartbeat pour chaque utilisateur afin qu'ils restent 'online'."""
    for name, pwd in [(USER1_NAME, USER1_PASSWORD), (USER2_NAME, USER2_PASSWORD), (ADMIN_NAME, ADMIN_PASSWORD)]:
        try:
            token = requests.post(f"{API}/auth/login", json={"name": name, "password": pwd}).json()["access_token"]
            requests.post(f"{API}/devices/heartbeat", headers={"Authorization": f"Bearer {token}"})
        except Exception:
            pass


def _get_user_id(name):
    admin_h = _admin_headers()
    users = requests.get(f"{API}/users/", headers=admin_h).json()
    return next(u["id"] for u in users if u["name"] == name)


def _send_alarm(title="Test Alarm", assigned_user_id=None):
    """Cree une alarme et retourne son ID."""
    admin_h = _admin_headers()
    payload = {"title": title, "message": "test", "severity": "critical"}
    if assigned_user_id:
        payload["assigned_user_id"] = assigned_user_id
    r = requests.post(f"{API}/alarms/send", headers=admin_h, json=payload)
    assert r.status_code == 200, f"Send alarm failed: {r.text}"
    return r.json()["id"]


def _get_pending_sms():
    r = requests.get(f"{BASE_URL}/internal/sms/pending", headers=GATEWAY_HEADERS)
    assert r.status_code == 200
    return r.json()


def _get_pending_calls():
    r = requests.get(f"{BASE_URL}/internal/calls/pending", headers=GATEWAY_HEADERS)
    assert r.status_code == 200
    return r.json()


def _register_phone(user_id, phone_number):
    admin_h = _admin_headers()
    r = requests.patch(f"{API}/users/{user_id}", headers=admin_h, json={"phone_number": phone_number})
    assert r.status_code == 200


# =============================================================================
# TestSmsCallTimer — Timer per-user pour SMS/appel
# =============================================================================

class TestSmsCallTimer:
    """Verifie que les SMS/calls sont envoyes apres un delai configurable
    (pas a l'escalade, mais N min apres notification)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset", params={"peer": "false"})
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset-sms-queue")
        requests.post(f"{API}/test/reset-call-queue")
        # Enregistrer phones
        self.user1_id = _get_user_id(USER1_NAME)
        self.user2_id = _get_user_id(USER2_NAME)
        self.admin_id = _get_user_id(ADMIN_NAME)
        _register_phone(self.user1_id, "+33600000001")
        _register_phone(self.user2_id, "+33600000002")
        time.sleep(12)  # Attendre tick escalation loop
        yield
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset-sms-queue")
        requests.post(f"{API}/test/reset-call-queue")

    def test_sms_not_enqueued_before_delay(self):
        """Alarme + 1min < delai default 2min → pas de SMS."""
        alarm_id = _send_alarm("SMS Timer Test", self.user1_id)
        _advance_clock_all_nodes(1)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        pending = _get_pending_sms()
        sms_for_user1 = [s for s in pending if s["to_number"] == "+33600000001"]
        assert len(sms_for_user1) == 0, \
            f"Aucun SMS attendu avant le delai, got {len(sms_for_user1)}"

    def test_sms_and_call_enqueued_after_delay(self):
        """Alarme + 3min (> delai default 2min) → 1 SMS + 1 call pour user1."""
        alarm_id = _send_alarm("SMS+Call Timer Test", self.user1_id)
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        sms = _get_pending_sms()
        sms_user1 = [s for s in sms if s["to_number"] == "+33600000001"]
        assert len(sms_user1) >= 1, f"1 SMS attendu pour user1, got {len(sms_user1)}"

        calls = _get_pending_calls()
        calls_user1 = [c for c in calls if c["to_number"] == "+33600000001"]
        assert len(calls_user1) >= 1, f"1 call attendu pour user1, got {len(calls_user1)}"

    def test_sms_call_delay_configurable(self):
        """Delai configurable a 5min : +3min → rien, +6min → SMS present."""
        admin_h = _admin_headers()
        # Configurer le delai a 5 min
        requests.post(f"{API}/config/sms-call-delay", headers=admin_h,
                     json={"delay_minutes": 5})

        alarm_id = _send_alarm("Delay Config Test", self.user1_id)

        # +3min : pas de SMS (< 5min)
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)
        sms = _get_pending_sms()
        sms_user1 = [s for s in sms if s["to_number"] == "+33600000001"]
        assert len(sms_user1) == 0, f"Pas de SMS a +3min (delai 5min), got {len(sms_user1)}"

        # +6min total : SMS present (> 5min)
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)
        sms = _get_pending_sms()
        sms_user1 = [s for s in sms if s["to_number"] == "+33600000001"]
        assert len(sms_user1) >= 1, f"SMS attendu a +6min (delai 5min), got {len(sms_user1)}"

        # Remettre le delai par defaut
        requests.post(f"{API}/config/sms-call-delay", headers=admin_h,
                     json={"delay_minutes": 2})

    def test_sms_not_enqueued_if_already_acked(self):
        """Ack immediat + 3min → pas de SMS (alarme plus active)."""
        alarm_id = _send_alarm("Ack Before SMS Test", self.user1_id)

        # Acquitter immediatement — en tant que user1 qui est dans les notifies
        _, user1_h, _ = _login(USER1_NAME, USER1_PASSWORD)
        # D'abord verifier que l'alarme est visible
        r = requests.get(f"{API}/alarms/mine", headers=user1_h)
        mine = [a for a in r.json() if a["id"] == alarm_id]
        assert len(mine) == 1, f"Alarme {alarm_id} doit etre visible pour user1, got {r.json()}"
        r = requests.post(f"{API}/alarms/{alarm_id}/ack", headers=user1_h)
        assert r.status_code == 200, f"Ack failed: {r.status_code} {r.text}"

        # Avancer de 3min
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        sms = _get_pending_sms()
        sms_user1 = [s for s in sms if s["to_number"] == "+33600000001"]
        assert len(sms_user1) == 0, \
            f"Pas de SMS apres ack immediat, got {len(sms_user1)}"

    def test_sms_call_per_user_timer(self):
        """user1 recoit SMS a t+2min. Apres escalade (t+15), user2 recoit SMS a t+17."""
        alarm_id = _send_alarm("Per-User Timer Test", self.user1_id)

        # t+3min : user1 doit avoir un SMS
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)
        sms = _get_pending_sms()
        sms_user1 = [s for s in sms if s["to_number"] == "+33600000001"]
        assert len(sms_user1) >= 1, f"SMS user1 attendu a +3min"

        # user2 ne doit PAS avoir de SMS (pas encore escalade)
        sms_user2 = [s for s in sms if s["to_number"] == "+33600000002"]
        assert len(sms_user2) == 0, f"Pas de SMS user2 avant escalade"

        # t+16min : escalade (15min) → user2 notifie
        _advance_clock_all_nodes(13)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        # t+19min : user2 devrait avoir SMS (2min apres notification a t+16)
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        sms = _get_pending_sms()
        sms_user2 = [s for s in sms if s["to_number"] == "+33600000002"]
        assert len(sms_user2) >= 1, f"SMS user2 attendu apres escalade + delai"

    def test_no_duplicate_sms_on_multiple_ticks(self):
        """Plusieurs ticks de la boucle → exactement 1 SMS par user (pas de doublon)."""
        alarm_id = _send_alarm("No Dup SMS Test", self.user1_id)
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        # Premier check
        sms = _get_pending_sms()
        sms_user1 = [s for s in sms if s["to_number"] == "+33600000001"]
        count_first = len(sms_user1)
        assert count_first >= 1, "Au moins 1 SMS"

        # Attendre encore 2 ticks
        time.sleep(TICK_WAIT)
        sms = _get_pending_sms()
        sms_user1 = [s for s in sms if s["to_number"] == "+33600000001"]
        assert len(sms_user1) == count_first, \
            f"Pas de SMS doublon : expected {count_first}, got {len(sms_user1)}"

    def test_no_duplicate_call_on_multiple_ticks(self):
        """Plusieurs ticks de la boucle → exactement 1 call par user."""
        alarm_id = _send_alarm("No Dup Call Test", self.user1_id)
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        calls = _get_pending_calls()
        calls_user1 = [c for c in calls if c["to_number"] == "+33600000001"]
        count_first = len(calls_user1)
        assert count_first >= 1, "Au moins 1 call"

        time.sleep(TICK_WAIT)
        calls = _get_pending_calls()
        calls_user1 = [c for c in calls if c["to_number"] == "+33600000001"]
        assert len(calls_user1) == count_first, \
            f"Pas de call doublon : expected {count_first}, got {len(calls_user1)}"


# =============================================================================
# TestCallQueueEndpoints — Endpoints /internal/calls/*
# =============================================================================

class TestCallQueueEndpoints:
    """Verifie les endpoints de la call queue (meme pattern que sms_queue)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset", params={"peer": "false"})
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset-call-queue")
        time.sleep(12)
        yield
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset-call-queue")

    def test_get_pending_calls(self):
        """GET /internal/calls/pending retourne les appels non traites."""
        # Creer une alarme d'abord pour avoir un alarm_id valide
        user1_id = _get_user_id(USER1_NAME)
        alarm_id = _send_alarm("Call Pending Test", user1_id)

        # Inserer un call via helper
        r = requests.post(f"{API}/test/insert-call", json={
            "to_number": "+33600000001",
            "alarm_id": alarm_id,
            "user_id": user1_id,
            "tts_message": "Alarme critique. Appuyez 1 pour acquitter."
        })
        assert r.status_code == 200, f"insert-call failed: {r.status_code} {r.text}"
        call_id = r.json()["id"]

        calls = _get_pending_calls()
        assert len(calls) >= 1
        ids = [c["id"] for c in calls]
        assert call_id in ids

    def test_get_pending_calls_requires_gateway_key(self):
        """GET /internal/calls/pending sans X-Gateway-Key → 401."""
        r = requests.get(f"{BASE_URL}/internal/calls/pending")
        assert r.status_code == 401

    def test_post_call_result_ack_dtmf(self):
        """POST result 'ack_dtmf' → alarme acquittee."""
        user1_id = _get_user_id(USER1_NAME)

        # Creer une alarme active
        alarm_id = _send_alarm("DTMF Ack Test", user1_id)

        # Inserer un call pour cette alarme
        r = requests.post(f"{API}/test/insert-call", json={
            "to_number": "+33600000001",
            "alarm_id": alarm_id,
            "user_id": user1_id,
            "tts_message": "Alarme critique."
        })
        call_id = r.json()["id"]

        # Poster le resultat ack_dtmf
        r = requests.post(
            f"{BASE_URL}/internal/calls/{call_id}/result",
            json={"result": "ack_dtmf"},
            headers=GATEWAY_HEADERS
        )
        assert r.status_code == 200

        # Verifier que l'alarme est acquittee
        _, user1_h, _ = _login(USER1_NAME, USER1_PASSWORD)
        r = requests.get(f"{API}/alarms/mine", headers=user1_h)
        alarms = r.json()
        alarm = next((a for a in alarms if a["id"] == alarm_id), None)
        assert alarm is not None, "Alarme doit etre visible"
        assert alarm["acknowledged_at"] is not None, "Alarme doit etre acquittee"

    def test_post_call_result_no_answer(self):
        """POST result 'no_answer' → alarme inchangee, retries incremente."""
        user1_id = _get_user_id(USER1_NAME)
        alarm_id = _send_alarm("No Answer Test", user1_id)

        r = requests.post(f"{API}/test/insert-call", json={
            "to_number": "+33600000001",
            "alarm_id": alarm_id,
            "user_id": user1_id,
            "tts_message": "Alarme critique."
        })
        call_id = r.json()["id"]

        r = requests.post(
            f"{BASE_URL}/internal/calls/{call_id}/result",
            json={"result": "no_answer"},
            headers=GATEWAY_HEADERS
        )
        assert r.status_code == 200
        data = r.json()
        assert data["retries"] == 1

        # L'alarme doit toujours etre active (pas acquittee)
        _, user1_h, _ = _login(USER1_NAME, USER1_PASSWORD)
        r = requests.get(f"{API}/alarms/mine", headers=user1_h)
        alarm = next((a for a in r.json() if a["id"] == alarm_id), None)
        assert alarm is not None
        assert alarm["acknowledged_at"] is None, "Alarme ne doit pas etre acquittee"

    def test_post_call_result_escalate(self):
        """POST result 'escalate' → escalade forcee vers le prochain user."""
        user1_id = _get_user_id(USER1_NAME)
        user2_id = _get_user_id(USER2_NAME)
        alarm_id = _send_alarm("Escalate Call Test", user1_id)

        r = requests.post(f"{API}/test/insert-call", json={
            "to_number": "+33600000001",
            "alarm_id": alarm_id,
            "user_id": user1_id,
            "tts_message": "Alarme critique."
        })
        call_id = r.json()["id"]

        r = requests.post(
            f"{BASE_URL}/internal/calls/{call_id}/result",
            json={"result": "escalate"},
            headers=GATEWAY_HEADERS
        )
        assert r.status_code == 200

        # Attendre que l'escalade soit traitee
        time.sleep(TICK_WAIT)

        # Verifier que l'alarme est maintenant assignee a user2
        admin_h = _admin_headers()
        r = requests.get(f"{API}/alarms/", headers=admin_h, params={"days": 1})
        alarm = next((a for a in r.json() if a["id"] == alarm_id), None)
        assert alarm is not None
        assert alarm["assigned_user_id"] == user2_id, \
            f"Alarme devrait etre escaladee vers user2 ({user2_id}), got {alarm['assigned_user_id']}"


# =============================================================================
# TestAckByPhone — Acquittement par numero de telephone
# =============================================================================

class TestAckByPhone:
    """Verifie l'endpoint POST /internal/alarms/active/ack-by-phone."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset", params={"peer": "false"})
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset-sms-queue")
        requests.post(f"{API}/test/reset-call-queue")
        self.user1_id = _get_user_id(USER1_NAME)
        _register_phone(self.user1_id, "+33600000001")
        time.sleep(12)
        yield
        _reset_clock_all_nodes()

    def test_ack_by_phone_known_number(self):
        """Numero connu + alarme active → 200 + alarme acquittee."""
        alarm_id = _send_alarm("Phone Ack Test", self.user1_id)

        r = requests.post(
            f"{BASE_URL}/internal/alarms/active/ack-by-phone",
            json={"phone_number": "+33600000001"},
            headers=GATEWAY_HEADERS
        )
        assert r.status_code == 200

        # Verifier ack
        _, user1_h, _ = _login(USER1_NAME, USER1_PASSWORD)
        r = requests.get(f"{API}/alarms/mine", headers=user1_h)
        alarm = next((a for a in r.json() if a["id"] == alarm_id), None)
        assert alarm is not None
        assert alarm["acknowledged_at"] is not None, "Alarme doit etre acquittee par telephone"

    def test_ack_by_phone_unknown_number(self):
        """Numero inconnu → 404."""
        _send_alarm("Phone Ack Unknown Test", self.user1_id)

        r = requests.post(
            f"{BASE_URL}/internal/alarms/active/ack-by-phone",
            json={"phone_number": "+33699999999"},
            headers=GATEWAY_HEADERS
        )
        assert r.status_code == 404

    def test_ack_by_phone_no_active_alarm(self):
        """Pas d'alarme active → 404."""
        r = requests.post(
            f"{BASE_URL}/internal/alarms/active/ack-by-phone",
            json={"phone_number": "+33600000001"},
            headers=GATEWAY_HEADERS
        )
        assert r.status_code == 404

    def test_ack_by_phone_already_acked(self):
        """Alarme deja acquittee → 200 (idempotent)."""
        alarm_id = _send_alarm("Phone Ack Idempotent Test", self.user1_id)

        # Premier ack
        r = requests.post(
            f"{BASE_URL}/internal/alarms/active/ack-by-phone",
            json={"phone_number": "+33600000001"},
            headers=GATEWAY_HEADERS
        )
        assert r.status_code == 200

        # Deuxieme ack — doit retourner 200 (idempotent, pas d'erreur)
        r = requests.post(
            f"{BASE_URL}/internal/alarms/active/ack-by-phone",
            json={"phone_number": "+33600000001"},
            headers=GATEWAY_HEADERS
        )
        assert r.status_code == 200


# =============================================================================
# TestSmsCallIntegration — Integration complete timeline
# =============================================================================

class TestSmsCallIntegration:
    """Tests d'integration : timeline complete FCM→SMS→Call avec escalade."""

    @pytest.fixture(autouse=True)
    def setup(self):
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset", params={"peer": "false"})
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset-sms-queue")
        requests.post(f"{API}/test/reset-call-queue")
        self.user1_id = _get_user_id(USER1_NAME)
        self.user2_id = _get_user_id(USER2_NAME)
        _register_phone(self.user1_id, "+33600000001")
        _register_phone(self.user2_id, "+33600000002")
        time.sleep(12)
        yield
        _reset_clock_all_nodes()
        requests.post(f"{API}/test/reset-sms-queue")
        requests.post(f"{API}/test/reset-call-queue")

    def test_full_timeline_sms_call(self):
        """Timeline complete : user1 notifie → SMS a t+2min → escalade a t+15 →
        user2 notifie → user2 SMS a t+17min."""
        alarm_id = _send_alarm("Full Timeline Test", self.user1_id)

        # t+3min : user1 SMS
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)
        sms = _get_pending_sms()
        assert any(s["to_number"] == "+33600000001" for s in sms), "user1 SMS a t+3"

        # t+16min : escalade (user2 notifie)
        _advance_clock_all_nodes(13)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        # t+19min : user2 SMS
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)
        sms = _get_pending_sms()
        assert any(s["to_number"] == "+33600000002" for s in sms), "user2 SMS a t+19"

        # Calls doivent aussi etre presentes
        calls = _get_pending_calls()
        assert any(c["to_number"] == "+33600000001" for c in calls), "user1 call"
        assert any(c["to_number"] == "+33600000002" for c in calls), "user2 call"

    def test_ack_stops_future_sms_calls(self):
        """Ack avant le delai SMS → pas de SMS/call envoye."""
        alarm_id = _send_alarm("Ack Stops SMS Test", self.user1_id)

        # Ack immediatement
        _, user1_h, _ = _login(USER1_NAME, USER1_PASSWORD)
        r = requests.post(f"{API}/alarms/{alarm_id}/ack", headers=user1_h)
        assert r.status_code == 200

        # Avancer au-dela du delai
        _advance_clock_all_nodes(5)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        sms = _get_pending_sms()
        sms_user1 = [s for s in sms if s["to_number"] == "+33600000001"]
        assert len(sms_user1) == 0, "Pas de SMS apres ack"

        calls = _get_pending_calls()
        calls_user1 = [c for c in calls if c["to_number"] == "+33600000001"]
        assert len(calls_user1) == 0, "Pas de call apres ack"

    def test_sms_call_after_ack_expiry(self):
        """Ack expire (30min) → alarme reactivee → nouveau cycle SMS/call."""
        alarm_id = _send_alarm("Ack Expiry SMS Test", self.user1_id)

        # Ack
        _, user1_h, _ = _login(USER1_NAME, USER1_PASSWORD)
        r = requests.post(f"{API}/alarms/{alarm_id}/ack", headers=user1_h)
        assert r.status_code == 200

        # Avancer au-dela du delai SMS mais avant expiry ack (30min)
        _advance_clock_all_nodes(5)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        # Pas de SMS pendant la suspension
        requests.post(f"{API}/test/reset-sms-queue")
        requests.post(f"{API}/test/reset-call-queue")

        # Avancer au-dela de l'expiry ack (30min total)
        _advance_clock_all_nodes(26)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        # L'alarme est reactivee → nouveau delai SMS
        _advance_clock_all_nodes(3)
        _refresh_all_heartbeats()
        time.sleep(TICK_WAIT)

        sms = _get_pending_sms()
        sms_user1 = [s for s in sms if s["to_number"] == "+33600000001"]
        assert len(sms_user1) >= 1, \
            f"SMS attendu apres reactivation post-expiry, got {len(sms_user1)}"
