"""
Tier 2 integration : INV-147 — la resolution d'une alarme envoie un push
`alarm_resolved` a chaque utilisateur notifie.

Bug prod du 2026-10-21 (alarme 886) : alarme resolue depuis le web. Le telephone
de admin (Pixel 9a, verrouille) a continue de sonner jusqu'au deverrouillage ;
celui de timo a coupe le son mais garde l'ecran plein ecran. Le backend n'envoyait
aucun push a la resolution : un telephone en Doze ne l'apprenait qu'a son prochain
poll reussi.
"""
import pytest

pytestmark = pytest.mark.integration

GATEWAY_KEY = "test-gateway-key-INV-147"


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]["id"]


@pytest.fixture
def setup(client):
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    admin_h, _ = _login(client, "admin", "admin123")
    users = {}
    for name in ("user1", "user2"):
        h, uid = _login(client, name, "user123")
        r = client.post("/api/devices/fcm-token", headers=h,
                        json={"token": f"tok-{name}-inv147", "device_id": f"dev-{name}-inv147"})
        assert r.status_code == 200, r.text
        users[name] = uid
    yield admin_h, users
    client.post("/api/test/reset", params={"peer": "false"})


def _resolved_pushes(client, alarm_id):
    return {f["user_id"] for f in client.get("/api/test/last-fcm").json()
            if f["data"].get("type") == "alarm_resolved" and f["data"].get("alarm_id") == str(alarm_id)}


def test_web_resolve_pushes_every_notified_user(client, setup):
    admin_h, users = setup
    sent = client.post("/api/alarms/send", headers=admin_h, json={
        "title": "INV-147", "message": "resolution poussee", "severity": "critical",
        "assigned_user_id": None,
    })
    assert sent.status_code == 200, sent.text
    alarm_id = sent.json()["id"]
    # Escalade user1 -> user2 : les deux sont notifies (cumulative).
    assert client.post("/api/test/trigger-escalation").status_code == 200
    client.post("/api/test/reset-fcm")

    r = client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_h)
    assert r.status_code == 200, r.text

    pushed = _resolved_pushes(client, alarm_id)
    assert {users["user1"], users["user2"]} <= pushed, (
        f"chaque notifie doit recevoir un push alarm_resolved : {pushed}")


def test_gateway_auto_resolve_pushes_notified_user(client, setup, monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", GATEWAY_KEY)
    admin_h, users = setup
    headers = {"X-Gateway-Key": GATEWAY_KEY}
    gw = {"gateway_id": "onsite-inv147"}
    assert client.post("/internal/alarms/report-state", headers=headers,
                       json={**gw, "state": "open"}).status_code == 200
    alarm_id = client.get("/api/alarms/active", headers=admin_h).json()[0]["id"]
    client.post("/api/test/reset-fcm")

    r = client.post("/internal/alarms/report-state", headers=headers, json={**gw, "state": "closed"})
    assert r.status_code == 200 and r.json()["alarm_active"] is False, r.text

    assert users["user1"] in _resolved_pushes(client, alarm_id), (
        "la resolution automatique gateway doit aussi pousser alarm_resolved")
