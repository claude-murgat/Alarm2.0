"""
Tier 2 integration : INV-144 — la boucle d'escalade attend que l'appel de l'assigne soit
passe avant d'escalader, au plus delai + 3 min (puis escalade marquee « sans appel »).

Prod 2026-10-20 (alarme 852) : delai d'escalade et delai SMS/appel a 2 min ; l'escalade est
partie a 10:13:55, le SMS et l'appel de timo n'ont ete crees qu'a 10:13:57 (appel passe a
10:15:28). Le proprietaire exige que SMS et appel precedent toujours l'escalade.

Revision 2026-10-20 (alarme 853) : SMS en echec (AT_ERROR), appel en `error` (modem muet) a
13:15:14 compte comme passe -> escalade a 13:15:22, SMS parti a 13:16:03. Desormais un appel
`error` (jamais emis, INV-127) est rejoue, et le SMS doit etre parti lui aussi.
"""
import time

import pytest

pytestmark = pytest.mark.integration

GW_KEY = "test-gateway-key-INV-144"
GW = {"X-Gateway-Key": GW_KEY}


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]["id"]


@pytest.fixture
def setup(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", GW_KEY)  # lu a chaque requete /internal/*
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200
    admin_h, _ = _login(client, "admin", "admin123")
    _, uid1 = _login(client, "user1", "user123")
    r = client.patch(f"/api/users/{uid1}", headers=admin_h, json={"phone_number": "+33600000144"})
    assert r.status_code == 200, r.text
    assert client.post("/api/config/system", headers=admin_h,
                       json={"key": "escalation_tick_seconds", "value": "1"}).status_code == 200
    db = SessionLocal()
    try:
        cfg = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
        if cfg:
            cfg.value = "ops@example.com"
        else:
            db.add(SystemConfig(key="alert_email", value="ops@example.com"))
        db.commit()
    finally:
        db.close()
    yield client, admin_h, uid1
    client.patch(f"/api/users/{uid1}", headers=admin_h, json={"phone_number": None})
    client.post("/api/config/system", headers=admin_h, json={"key": "escalation_tick_seconds", "value": "10"})
    client.post("/api/test/reset-clock", params={"peer": "false"})
    client.post("/api/test/reset", params={"peer": "false"})


def _alarm(client, h):
    return client.get("/api/alarms/active", headers=h).json()[0]


def _wait(client, h, pred, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        a = _alarm(client, h)
        if pred(a):
            return a
        time.sleep(0.3)
    return _alarm(client, h)


def _send(client, h, uid1):
    sent = client.post("/api/alarms/send", headers=h, json={
        "site": "izeron", "title": "INV-144", "message": "m", "severity": "critical", "assigned_user_id": None})
    assert sent.status_code == 200, sent.text
    assert sent.json()["assigned_user_id"] == uid1
    return sent.json()["id"]


def _advance(client, minutes):
    client.post("/api/test/advance-clock", params={"seconds": int(minutes * 60), "peer": "false"})


def _pending_call(client):
    """Attend que la boucle ait cree l'appel de user1 (preuve qu'elle a tourne)."""
    deadline = time.time() + 20  # le passage du tick a 1 s n'agit qu'apres l'attente en cours (<= 10 s)
    while time.time() < deadline:
        calls = client.get("/internal/calls/pending", headers=GW).json()
        call = next((c for c in calls if c["to_number"] == "+33600000144"), None)
        if call:
            time.sleep(2.5)  # >= 2 ticks de 1 s apres la creation de l'appel
            return call
        time.sleep(0.3)
    raise AssertionError("la boucle n'a pas cree l'appel de l'assigne")


def _send_sms(client):
    """Joue la gateway : envoie le SMS en attente de user1."""
    sms = next(s for s in client.get("/internal/sms/pending", headers=GW).json()
               if s["to_number"] == "+33600000144")
    assert client.post(f"/internal/sms/{sms['id']}/sent", headers=GW).status_code == 200


def _audit(client, h, alarm_id):
    r = client.get("/api/audit/", headers=h, params={"alarm_id": alarm_id, "page_size": 200})
    assert r.status_code == 200, r.text
    return [e["event_type"] for e in r.json()]


def test_escalation_waits_for_the_call_then_goes(setup):
    client, h, uid1 = setup
    alarm_id = _send(client, h, uid1)
    _advance(client, 15.5)  # delai par defaut 15 min ; SMS/appel crees (delai 2 min)
    call = _pending_call(client)
    assert _alarm(client, h)["escalation_count"] == 0, "escalade partie avant l'appel de l'assigne"

    _send_sms(client)
    assert client.post(f"/internal/calls/{call['id']}/result", headers=GW,
                       json={"result": "no_answer"}).status_code == 200

    went = _wait(client, h, lambda a: a["escalation_count"] >= 1, 10)
    assert went["escalation_count"] == 1 and went["assigned_user_id"] != uid1
    assert "escalation_without_call" not in _audit(client, h, alarm_id)


def test_escalation_goes_anyway_three_minutes_after_delay(setup, monkeypatch):
    client, h, uid1 = setup
    # Tous les emails de la boucle, pas seulement le dernier : en avancant l'horloge de 18 min,
    # l'alerte « astreinte perdue » (user1 sans heartbeat) peut partir juste apres celle-ci.
    import backend.app.escalation as esc
    subjects, real_send = [], esc.send_alert_email
    def _capture(subject, body, to):
        subjects.append(subject)
        return real_send(subject=subject, body=body, to=to)
    monkeypatch.setattr(esc, "send_alert_email", _capture)
    alarm_id = _send(client, h, uid1)
    _advance(client, 17.5)  # delai + 2,5 min : toujours retenue
    _pending_call(client)
    assert _alarm(client, h)["escalation_count"] == 0
    _advance(client, 1.0)   # delai + 3,5 min : escalade forcee
    went = _wait(client, h, lambda a: a["escalation_count"] >= 1, 10)
    assert went["escalation_count"] == 1 and went["assigned_user_id"] != uid1
    assert "escalation_without_call" in _audit(client, h, alarm_id)
    assert any("Escalade sans appel" in s for s in subjects), subjects


def test_call_error_is_replayed_and_holds_escalation(setup):
    """Alarme 853 : un appel `error` n'a jamais sonne -> pas passe, rejoue par la gateway."""
    client, h, uid1 = setup
    _send(client, h, uid1)
    _advance(client, 15.5)
    call = _pending_call(client)
    _send_sms(client)
    assert client.post(f"/internal/calls/{call['id']}/result", headers=GW,
                       json={"result": "error"}).status_code == 200

    time.sleep(2.5)  # >= 2 ticks
    assert _alarm(client, h)["escalation_count"] == 0, "escalade sur un appel jamais emis"
    again = [c for c in client.get("/internal/calls/pending", headers=GW).json() if c["id"] == call["id"]]
    assert again and again[0]["retries"] == 1, "l'appel en error n'est pas rejoue"

    assert client.post(f"/internal/calls/{call['id']}/result", headers=GW,
                       json={"result": "no_answer"}).status_code == 200
    went = _wait(client, h, lambda a: a["escalation_count"] >= 1, 10)
    assert went["escalation_count"] == 1


def test_escalation_waits_for_the_sms(setup):
    """Alarme 853 : SMS en echec, appel passe -> on attend que le SMS parte."""
    client, h, uid1 = setup
    _send(client, h, uid1)
    _advance(client, 15.5)
    call = _pending_call(client)
    sms = next(s for s in client.get("/internal/sms/pending", headers=GW).json()
               if s["to_number"] == "+33600000144")
    assert client.post(f"/internal/sms/{sms['id']}/error", headers=GW,
                       json={"error": "AT_ERROR"}).status_code == 200
    assert client.post(f"/internal/calls/{call['id']}/result", headers=GW,
                       json={"result": "no_answer"}).status_code == 200

    time.sleep(2.5)  # >= 2 ticks
    assert _alarm(client, h)["escalation_count"] == 0, "escalade avant le SMS de l'assigne"

    _send_sms(client)
    went = _wait(client, h, lambda a: a["escalation_count"] >= 1, 10)
    assert went["escalation_count"] == 1
