"""
Tier 2 integration tests : INV-138 — actions de l'onglet Tests utilisables en prod.

En prod, /api/test/* est coupe (INV-076). Les boutons de l'onglet Tests passent par
de vraies routes, admin uniquement, qui ne touchent que l'admin qui clique et ne
detruisent rien (decision proprietaire 2026-10-19) :
  - POST /api/alarms/escalate-now           : palier d'escalade immediat
  - POST /api/devices/me/go-offline         : moi hors ligne, heartbeats refuses 5 min
  - POST /api/devices/me/heartbeat-pause    : bascule, heartbeats refuses (max 5 min)
"""
import pytest

pytestmark = pytest.mark.integration


def _headers(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"login {name} failed: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin(client):
    return _headers(client, "admin", "admin123")


@pytest.fixture
def user1(client):
    return _headers(client, "user1", "user123")


@pytest.fixture(autouse=True)
def _clean(client):
    # Chaine par defaut user1 -> user2 -> admin, aucune alarme, aucune pause.
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200
    yield
    client.post("/api/test/reset-clock", params={"peer": "false"})
    client.post("/api/test/reset", params={"peer": "false"})


def _user(client, headers, name):
    users = client.get("/api/devices/", headers=headers).json()
    return next(u for u in users if u["name"] == name)


@pytest.mark.parametrize("path", [
    "/api/alarms/escalate-now",
    "/api/devices/me/go-offline",
    "/api/devices/me/heartbeat-pause",
])
def test_non_admin_forbidden(client, user1, path):
    assert client.post(path, headers=user1).status_code == 403


def test_escalate_now_moves_active_alarm_to_next_user(client, admin):
    sent = client.post("/api/alarms/send", headers=admin, json={
        "site": "izeron", "title": "Alarme de test", "message": "INV-138", "severity": "critical",
        "assigned_user_id": None,
    })
    assert sent.status_code == 200, sent.text
    first = sent.json()["assigned_user_id"]

    r = client.post("/api/alarms/escalate-now", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["escalated"] == 1

    alarm = client.get("/api/alarms/active", headers=admin).json()[0]
    assert alarm["assigned_user_id"] != first
    assert alarm["escalation_count"] == 1
    assert alarm["status"] == "escalated"


def test_escalate_now_without_active_alarm_escalates_nothing(client, admin):
    r = client.post("/api/alarms/escalate-now", headers=admin)
    assert r.status_code == 200
    assert r.json()["escalated"] == 0


def test_go_offline_only_affects_me(client, admin, user1):
    assert client.post("/api/devices/me/go-offline", headers=admin).status_code == 200

    assert _user(client, admin, "admin")["is_online"] is False
    assert client.post("/api/devices/heartbeat", headers=admin).status_code == 503
    # Les autres utilisateurs ne sont pas touches.
    assert client.post("/api/devices/heartbeat", headers=user1).status_code == 200
    assert _user(client, admin, "user1")["is_online"] is True


def test_heartbeat_pause_toggles(client, admin, user1):
    r = client.post("/api/devices/me/heartbeat-pause", headers=admin)
    assert r.status_code == 200 and r.json()["paused"] is True
    assert client.post("/api/devices/heartbeat", headers=admin).status_code == 503
    assert client.post("/api/devices/heartbeat", headers=user1).status_code == 200

    r = client.post("/api/devices/me/heartbeat-pause", headers=admin)
    assert r.status_code == 200 and r.json()["paused"] is False
    assert client.post("/api/devices/heartbeat", headers=admin).status_code == 200


def test_heartbeat_pause_ends_by_itself_after_5_min(client, admin):
    assert client.post("/api/devices/me/heartbeat-pause", headers=admin).json()["paused"] is True
    client.post("/api/test/advance-clock", params={"seconds": 4 * 60, "peer": "false"})
    assert client.post("/api/devices/heartbeat", headers=admin).status_code == 503
    client.post("/api/test/advance-clock", params={"seconds": 2 * 60, "peer": "false"})
    assert client.post("/api/devices/heartbeat", headers=admin).status_code == 200
