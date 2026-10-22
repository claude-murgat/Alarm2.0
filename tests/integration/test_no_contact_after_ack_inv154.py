"""
Tier 2 integration : INV-154 — alarme acquittee ou resolue : plus aucun SMS ni
appel pour elle.

Prod 2026-10-22, alarme 923 : acquittee 12:14:51, resolue 12:15:05 (UTC) ; la
gateway (modem instable, AT_ERROR) a rejoue le SMS a 12:16:15 et passe l'appel a
12:16:28. Vraie boucle d'escalade (tick 1 s) + horloge avancee, gateway jouee ici.
"""
import time

import pytest

pytestmark = pytest.mark.integration

GW_KEY = "test-gateway-key-INV-154"
GW = {"X-Gateway-Key": GW_KEY}
PHONE = "+33600000154"


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]["id"]


@pytest.fixture
def setup(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", GW_KEY)  # lu a chaque requete /internal/*
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200
    admin_h, _ = _login(client, "admin", "admin123")
    user1_h, uid1 = _login(client, "user1", "user123")
    assert client.patch(f"/api/users/{uid1}", headers=admin_h, json={"phone_number": PHONE}).status_code == 200
    assert client.post("/api/config/system", headers=admin_h,
                       json={"key": "escalation_tick_seconds", "value": "1"}).status_code == 200
    yield client, admin_h, user1_h
    client.patch(f"/api/users/{uid1}", headers=admin_h, json={"phone_number": None})
    client.post("/api/config/system", headers=admin_h, json={"key": "escalation_tick_seconds", "value": "10"})
    client.post("/api/test/reset-clock", params={"peer": "false"})
    client.post("/api/test/reset", params={"peer": "false"})


def _pending(client, kind):
    return [x for x in client.get(f"/internal/{kind}/pending", headers=GW).json() if x["to_number"] == PHONE]


def _wait_for(cond, seconds=20):
    """Le passage du tick a 1 s n'agit qu'apres l'attente en cours (<= 10 s)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.3)
    return cond()


def _alarm_with_contacts(client, admin_h, title="INV-154"):
    """Alarme vers user1, SMS + appel crees par la boucle (delai SMS/appel 2 min)."""
    sent = client.post("/api/alarms/send", headers=admin_h, json={
        "title": title, "message": "m", "severity": "critical", "assigned_user_id": None})
    assert sent.status_code == 200, sent.text
    client.post("/api/test/advance-clock", params={"seconds": 150, "peer": "false"})
    assert _wait_for(lambda: _pending(client, "sms") and _pending(client, "calls")), "SMS/appel non crees"
    sms, call = _pending(client, "sms")[0], _pending(client, "calls")[0]
    # Comme en prod : un premier essai rate (modem instable), les deux restent en file
    client.post(f"/internal/sms/{sms['id']}/error", headers=GW, json={"error": "AT_ERROR: "})
    client.post(f"/internal/calls/{call['id']}/result", headers=GW, json={"result": "error"})
    assert _pending(client, "sms") and _pending(client, "calls"), "setup : SMS/appel doivent rester en file"
    return sent.json()["id"], sms["id"], call["id"]


def _rows(sms_id, call_id):
    from backend.app.database import SessionLocal
    from backend.app.models import SmsQueue, CallQueue
    db = SessionLocal()
    try:
        s, c = db.get(SmsQueue, sms_id), db.get(CallQueue, call_id)
        return (s.retries, s.error, s.sent_at), (c.retries, c.result, c.called_at)
    finally:
        db.close()


@pytest.mark.parametrize("finish", ["ack", "resolve"])
def test_no_sms_nor_call_after_ack_or_resolve(setup, finish):
    client, admin_h, user1_h = setup
    alarm_id, sms_id, call_id = _alarm_with_contacts(client, admin_h)

    if finish == "ack":
        r = client.post(f"/api/alarms/{alarm_id}/ack", headers=user1_h)
    else:
        r = client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_h)
    assert r.status_code == 200, r.text

    assert _pending(client, "sms") == [], "SMS remis a la gateway apres " + finish
    assert _pending(client, "calls") == [], "appel remis a la gateway apres " + finish
    assert _wait_for(lambda: _rows(sms_id, call_id)[0][0] >= 3), "SMS non retire de la file"
    (s_retries, s_error, s_sent), (c_retries, c_result, c_called) = _rows(sms_id, call_id)
    assert s_sent is None and s_error.startswith("annulé"), s_error
    assert c_called is None and c_result == "cancelled", c_result


def test_next_alarm_still_gets_its_sms_and_call(setup):
    """Garde-fou : le SMS annule de l'alarme precedente (meme titre, meme numero)
    ne doit pas bloquer celui de la suivante (anti-doublon INV-062)."""
    client, admin_h, _ = setup
    first, sms_id, call_id = _alarm_with_contacts(client, admin_h)
    assert client.post(f"/api/alarms/{first}/resolve", headers=admin_h).status_code == 200
    assert _wait_for(lambda: _rows(sms_id, call_id)[0][0] >= 3)

    _, sms2, call2 = _alarm_with_contacts(client, admin_h)
    assert (sms2, call2) != (sms_id, call_id), "la nouvelle alarme doit avoir ses propres SMS/appel"


def test_sms_without_alarm_still_delivered(setup):
    """Un SMS hors alarme (envoi de test) n'est pas concerne."""
    client, admin_h, _ = setup
    alarm_id, _, _ = _alarm_with_contacts(client, admin_h)
    client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_h)
    r = client.post("/api/test/insert-sms", json={"to_number": "+33600000999", "body": "hors alarme"})
    assert r.status_code == 200, r.text
    time.sleep(2.5)  # >= 2 ticks : le nettoyage ne doit pas le toucher
    pending = client.get("/internal/sms/pending", headers=GW).json()
    assert any(s["to_number"] == "+33600000999" for s in pending), "SMS hors alarme bloque"
