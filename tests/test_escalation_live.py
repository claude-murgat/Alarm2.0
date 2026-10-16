"""INV-136 : GET /api/alarms/active/escalation, etat temps reel de l'escalade.

Prerequis : backend live (docker compose --env-file .env.dev -p dev up -d).
Chaine par defaut apres /api/test/reset : user1 (1) -> user2 (2) -> admin (3).
"""
import os
import time
from datetime import datetime, timedelta

import pytest
import requests

BASE_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
API = f"{BASE_URL}/api"
GATEWAY_HEADERS = {"X-Gateway-Key": os.getenv("GATEWAY_KEY", "changeme-in-prod")}
PHONE_USER1 = "+33600000001"


def _login(name, password):
    r = requests.post(f"{API}/auth/login", json={"name": name, "password": password}, timeout=10)
    r.raise_for_status()
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["user"]["id"]


def _live(headers):
    r = requests.get(f"{API}/alarms/active/escalation", headers=headers, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def _step(live, user_id):
    return next(s for s in live["steps"] if s["user_id"] == user_id)


def _dt(s):
    return datetime.fromisoformat(s)


def _wait(predicate, headers, timeout=25.0):
    deadline = time.monotonic() + timeout
    while True:
        live = _live(headers)
        if predicate(live) or time.monotonic() >= deadline:
            return live
        time.sleep(0.5)


@pytest.fixture(autouse=True)
def clean():
    requests.post(f"{API}/test/reset-clock", params={"peer": "false"}, timeout=5)
    requests.post(f"{API}/test/reset", params={"peer": "false"}, timeout=10)
    requests.post(f"{API}/test/reset-sms-queue", timeout=5)
    requests.post(f"{API}/test/reset-call-queue", timeout=5)
    yield
    requests.post(f"{API}/test/reset-clock", params={"peer": "false"}, timeout=5)
    requests.post(f"{API}/test/reset", params={"peer": "false"}, timeout=10)


@pytest.fixture
def admin():
    return _login("admin", "admin123")[0]


def _send_alarm(admin):
    r = requests.post(f"{API}/alarms/send", headers=admin,
                      json={"title": "INV-136", "message": "test", "severity": "critical"}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_requires_auth():
    assert requests.get(f"{API}/alarms/active/escalation", timeout=10).status_code == 401


def test_no_alarm_lists_chain_as_upcoming(admin):
    live = _live(admin)
    assert live["alarm"] is None
    assert live["next_escalation_at"] is None
    assert [s["position"] for s in live["steps"]] == [1, 2, 3]
    assert {s["role"] for s in live["steps"]} == {"upcoming"}


def test_new_alarm_targets_position_1(admin):
    _, user1 = _login("user1", "user123")
    requests.patch(f"{API}/users/{user1}", headers=admin, json={"phone_number": None},
                   timeout=10).raise_for_status()
    alarm_id = _send_alarm(admin)
    live = _live(admin)

    assert live["alarm"]["id"] == alarm_id
    assert live["alarm"]["ack_expired"] is False
    first = live["steps"][0]
    assert (first["user_id"], first["role"]) == (user1, "current")
    assert first["notified_at"] is not None
    assert _dt(first["contact_at"]) - _dt(first["notified_at"]) == timedelta(minutes=live["sms_call_delay_minutes"])
    assert first["sms"] == "no_phone" and first["call"] == "no_phone"
    assert [s["role"] for s in live["steps"][1:]] == ["upcoming", "upcoming"]
    assert _dt(live["next_escalation_at"]) - _dt(live["alarm"]["created_at"]) == \
        timedelta(minutes=live["escalation_delay_minutes"])


def test_escalation_is_cumulative(admin):
    _send_alarm(admin)
    requests.post(f"{API}/test/trigger-escalation", timeout=10).raise_for_status()
    roles = [s["role"] for s in _live(admin)["steps"]]
    assert roles == ["notified", "current", "upcoming"]


def test_sms_and_call_progress_then_call_result(admin):
    _, user1 = _login("user1", "user123")
    requests.patch(f"{API}/users/{user1}", headers=admin, json={"phone_number": PHONE_USER1},
                   timeout=10).raise_for_status()
    _send_alarm(admin)
    assert _step(_live(admin), user1)["sms"] == "scheduled"

    requests.post(f"{API}/test/advance-clock", params={"minutes": 3, "peer": "false"}, timeout=5)
    live = _wait(lambda l: _step(l, user1)["call"] == "pending", admin)
    assert _step(live, user1)["sms"] == "pending"
    assert _step(live, user1)["call"] == "pending"

    calls = requests.get(f"{BASE_URL}/internal/calls/pending", headers=GATEWAY_HEADERS, timeout=10).json()
    call_id = next(c["id"] for c in calls if c["to_number"] == PHONE_USER1)
    requests.post(f"{BASE_URL}/internal/calls/{call_id}/result", headers=GATEWAY_HEADERS,
                  json={"result": "no_answer"}, timeout=10).raise_for_status()
    assert _step(_live(admin), user1)["call"] == "no_answer"


def test_ack_marks_acker_and_stops_countdown(admin):
    user1_h, user1 = _login("user1", "user123")
    alarm_id = _send_alarm(admin)
    requests.post(f"{API}/alarms/{alarm_id}/ack", headers=user1_h, timeout=10).raise_for_status()

    live = _live(admin)
    assert live["alarm"]["status"] == "acknowledged"
    assert live["alarm"]["acknowledged_by"] == user1
    assert live["next_escalation_at"] is None
    assert _step(live, user1)["acked"] is True


def test_response_is_stable_without_event(admin):
    """Sinon le WebSocket (INV-135) notifierait toutes les 3 s."""
    _send_alarm(admin)
    first = requests.get(f"{API}/alarms/active/escalation", headers=admin, timeout=10).content
    time.sleep(1.5)
    assert requests.get(f"{API}/alarms/active/escalation", headers=admin, timeout=10).content == first
