"""
Tier 2 integration : INV-159, l'appel vocal annonce le site.

`/internal/calls/pending` donne a la gateway le `site` de l'alarme de chaque appel, pour
que Jessica joue `alarme_izeron.wav` ou `alarme_beaufort.wav`. Alarme sans site (astreinte) :
`site` vide -> `alarme.wav`, meme si l'appel part par le modem de `default_site` (INV-156).

Vraie boucle d'escalade (tick 1 s) + horloge avancee pour la creation des appels.
"""
import time

import pytest

pytestmark = pytest.mark.integration

GW_KEY = "test-gateway-key-INV-159"
GW = {"X-Gateway-Key": GW_KEY}
PHONE = "+33600000159"


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]["id"]


@pytest.fixture
def setup(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", GW_KEY)
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200
    admin_h, _ = _login(client, "admin", "admin123")
    _, uid1 = _login(client, "user1", "user123")
    assert client.patch(f"/api/users/{uid1}", headers=admin_h, json={"phone_number": PHONE}).status_code == 200
    assert client.post("/api/config/system", headers=admin_h,
                       json={"key": "escalation_tick_seconds", "value": "1"}).status_code == 200
    yield client, admin_h, uid1
    client.patch(f"/api/users/{uid1}", headers=admin_h, json={"phone_number": None})
    client.post("/api/config/system", headers=admin_h, json={"key": "escalation_tick_seconds", "value": "10"})
    client.post("/api/test/reset-clock", params={"peer": "false"})
    client.post("/api/test/reset", params={"peer": "false"})


def _oncall_alarm(uid):
    """Alarme d'astreinte (perte de heartbeat) : sans site (INV-155)."""
    from backend.app.clock import now as clock_now
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm, AlarmNotification
    db = SessionLocal()
    try:
        t0 = clock_now()
        alarm = Alarm(title="Astreinte injoignable", message="m", severity="critical",
                      assigned_user_id=uid, is_oncall_alarm=True, source="oncall",
                      original_created_at=t0, created_at=t0)
        db.add(alarm)
        db.flush()
        db.add(AlarmNotification(alarm_id=alarm.id, user_id=uid, notified_at=t0))
        db.commit()
        return alarm.id
    finally:
        db.close()


def _pending_calls(client):
    r = client.get("/internal/calls/pending", headers=GW, params={"site": "izeron,beaufort"})
    assert r.status_code == 200, r.text
    return [c for c in r.json() if c["to_number"] == PHONE]


def test_pending_calls_carry_the_alarm_site(setup):
    client, admin_h, uid1 = setup
    ids = {}
    for site in ("izeron", "beaufort"):
        r = client.post("/api/alarms/send", headers=admin_h,
                        json={"title": "Pompe", "message": "m", "severity": "critical", "site": site})
        assert r.status_code == 200, r.text
        ids[r.json()["id"]] = site
    ids[_oncall_alarm(uid1)] = None

    client.post("/api/test/advance-clock", params={"seconds": 150, "peer": "false"})
    deadline = time.time() + 20
    while len(_pending_calls(client)) < 3 and time.time() < deadline:
        time.sleep(0.3)

    calls = _pending_calls(client)
    assert {c["alarm_id"]: c.get("site", "absent") for c in calls} == ids
