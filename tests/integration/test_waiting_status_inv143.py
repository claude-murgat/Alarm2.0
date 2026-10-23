"""
Tier 2 integration : INV-143 — l'API dit si un utilisateur a un telephone enregistre
(`has_device`), pour que le web affiche « En attente » (joignable par push) au lieu de
« Hors ligne » pour un utilisateur hors garde : il n'envoie pas de heartbeat par design
(INV-ANDROID-600) mais sera bien reveille par le push a l'escalade.
"""
import pytest

pytestmark = pytest.mark.integration


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin(client):
    from backend.app.database import SessionLocal
    from backend.app.models import DeviceToken, User
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    db = SessionLocal()
    try:
        ids = [u.id for u in db.query(User).filter(User.name.in_(["user1", "user2"]))]
        db.query(DeviceToken).filter(DeviceToken.user_id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    yield _login(client, "admin", "admin123")
    client.post("/api/test/reset", params={"peer": "false"})


def _user(client, h, name):
    return next(u for u in client.get("/api/users/", headers=h).json() if u["name"] == name)


def test_has_device_false_without_phone_true_after_registration(client, admin):
    assert _user(client, admin, "user2")["has_device"] is False
    u2 = _login(client, "user2", "user123")
    r = client.post("/api/devices/fcm-token", headers=u2,
                    json={"token": "tok-user2-inv143", "device_id": "dev-user2-inv143"})
    assert r.status_code == 200, r.text
    assert _user(client, admin, "user2")["has_device"] is True
    assert _user(client, admin, "user1")["has_device"] is False


def test_escalation_steps_carry_has_device(client, admin):
    u2 = _login(client, "user2", "user123")
    client.post("/api/devices/fcm-token", headers=u2,
                json={"token": "tok-user2-inv143", "device_id": "dev-user2-inv143"})
    sent = client.post("/api/alarms/send", headers=admin, json={
        "site": "izeron", "title": "INV-143", "message": "m", "severity": "critical", "assigned_user_id": None})
    assert sent.status_code == 200, sent.text
    steps = {s["name"]: s for s in client.get("/api/alarms/active/escalation", headers=admin).json()["steps"]}
    assert steps["user2"]["has_device"] is True
    assert steps["user1"]["has_device"] is False
