"""
Tier 2 integration : multi-site Izeron / Beaufort.

- INV-001 : au plus une alarme active par site (les alarmes sans site forment leur
  propre perimetre).
- INV-155 : chaque alarme porte un site (manuelle : obligatoire et connu ; contact sec :
  site de la passerelle, defaut `default_site` ; astreinte : vide). Titre contact sec et
  SMS nomment le site.
- INV-156 : SMS et appels portent le site de l'alarme (`default_site` si vide) ;
  `/internal/{sms,calls}/pending?site=X` ne renvoie que le site X (defaut : default_site,
  liste separee par des virgules admise).

Vraie boucle d'escalade (tick 1 s) + horloge avancee pour la creation des SMS/appels.
"""
import time

import pytest

pytestmark = pytest.mark.integration

GW_KEY = "test-gateway-key-INV-155"
GW = {"X-Gateway-Key": GW_KEY}
PHONE = "+33600000155"


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


def _send(client, h, site, title="multisite"):
    body = {"title": title, "message": "m", "severity": "critical"}
    if site is not None:
        body["site"] = site
    return client.post("/api/alarms/send", headers=h, json=body)


def _wait_for(cond, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.3)
    return cond()


def _queue_rows():
    """(sms, appels) de PHONE en base : [(site, body/tts)]."""
    from backend.app.database import SessionLocal
    from backend.app.models import SmsQueue, CallQueue
    db = SessionLocal()
    try:
        sms = [(r.site, r.body) for r in db.query(SmsQueue).filter(SmsQueue.to_number == PHONE)]
        calls = [(r.site, r.alarm_id) for r in db.query(CallQueue).filter(CallQueue.to_number == PHONE)]
        return sms, calls
    finally:
        db.close()


def _pending(client, kind, site=None):
    params = {"site": site} if site is not None else None
    r = client.get(f"/internal/{kind}/pending", headers=GW, params=params)
    assert r.status_code == 200, r.text
    return [x for x in r.json() if x["to_number"] == PHONE]


# ── INV-155 / INV-001 : alarme manuelle ─────────────────────────────────────

def test_izeron_and_beaufort_alarms_coexist(setup):
    client, admin_h, _ = setup
    a = _send(client, admin_h, "izeron")
    b = _send(client, admin_h, "beaufort")
    assert a.status_code == 200, a.text
    assert b.status_code == 200, "une alarme Beaufort ne doit pas etre bloquee par Izeron : " + b.text
    assert (a.json()["site"], b.json()["site"]) == ("izeron", "beaufort")
    active = client.get("/api/alarms/active", headers=admin_h).json()
    assert sorted(x["site"] for x in active) == ["beaufort", "izeron"]


def test_second_alarm_same_site_is_409(setup):
    client, admin_h, _ = setup
    assert _send(client, admin_h, "beaufort").status_code == 200
    assert _send(client, admin_h, "beaufort").status_code == 409


@pytest.mark.parametrize("site", [None, "lyon"])
def test_send_without_or_unknown_site_is_422(setup, site):
    client, admin_h, _ = setup
    r = _send(client, admin_h, site)
    assert r.status_code == 422, r.text


# ── INV-155 / INV-001 : contact sec ────────────────────────────────────────

def _report(client, gateway_id, state, site=None):
    body = {"gateway_id": gateway_id, "state": state}
    if site is not None:
        body["site"] = site
    r = client.post("/internal/alarms/report-state", headers=GW, json=body)
    assert r.status_code == 200, r.text


def test_dry_contact_beaufort_while_izeron_active(setup):
    client, admin_h, _ = setup
    assert _send(client, admin_h, "izeron").status_code == 200
    _report(client, "alarm-3", "open", site="beaufort")
    active = {x["site"]: x for x in client.get("/api/alarms/active", headers=admin_h).json()}
    assert "beaufort" in active, "l'alarme contact sec de Beaufort a ete perdue"
    assert active["beaufort"]["title"] == "Déclenchement contact sec — Beaufort"
    assert len(active) == 2


def test_dry_contact_ignored_when_same_site_active(setup):
    client, admin_h, _ = setup
    assert _send(client, admin_h, "beaufort").status_code == 200
    _report(client, "alarm-3", "open", site="beaufort")
    assert [x["site"] for x in client.get("/api/alarms/active", headers=admin_h).json()] == ["beaufort"]


def test_dry_contact_without_site_uses_default_site(setup):
    """Passerelle d'avant INV-155 (pas de champ site) -> default_site (izeron)."""
    client, admin_h, _ = setup
    _report(client, "onsite-2", "open")
    active = client.get("/api/alarms/active", headers=admin_h).json()
    assert [(x["site"], x["title"]) for x in active] == [("izeron", "Déclenchement contact sec — Izeron")]


# ── INV-156 : SMS / appels par site ────────────────────────────────────────

def test_sms_and_calls_carry_site_and_pending_filters_by_site(setup):
    client, admin_h, _ = setup
    iz = _send(client, admin_h, "izeron", title="Pompe")
    bf = _send(client, admin_h, "beaufort", title="Pompe")
    assert iz.status_code == 200 and bf.status_code == 200
    client.post("/api/test/advance-clock", params={"seconds": 150, "peer": "false"})
    assert _wait_for(lambda: len(_queue_rows()[0]) == 2 and len(_queue_rows()[1]) == 2), _queue_rows()

    sms, calls = _queue_rows()
    assert sorted(sms) == [("beaufort", "ALARME Beaufort: Pompe\nRepondez 1 pour acquitter ou "
                                        "https://supervision.charlesmurgat.com"),
                           ("izeron", "ALARME Izeron: Pompe\nRepondez 1 pour acquitter ou "
                                      "https://supervision.charlesmurgat.com")]
    assert sorted(calls) == sorted([("izeron", iz.json()["id"]), ("beaufort", bf.json()["id"])])

    assert [s["body"].split(":")[0] for s in _pending(client, "sms", "izeron")] == ["ALARME Izeron"]
    assert [s["body"].split(":")[0] for s in _pending(client, "sms", "beaufort")] == ["ALARME Beaufort"]
    assert [c["alarm_id"] for c in _pending(client, "calls", "izeron")] == [iz.json()["id"]]
    assert [c["alarm_id"] for c in _pending(client, "calls", "beaufort")] == [bf.json()["id"]]
    # Passerelle d'avant INV-155 (sans parametre) -> default_site
    assert [c["alarm_id"] for c in _pending(client, "calls")] == [iz.json()["id"]]
    assert [s["body"].split(":")[0] for s in _pending(client, "sms")] == ["ALARME Izeron"]
    # Cluster de test : un seul modem joue les deux sites
    assert len(_pending(client, "sms", "izeron,beaufort")) == 2
    assert len(_pending(client, "calls", "izeron,beaufort")) == 2


def test_oncall_alarm_has_no_site_and_contacts_go_to_default_site(setup):
    client, admin_h, uid1 = setup
    from backend.app.clock import now as clock_now
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm, AlarmNotification
    db = SessionLocal()
    try:
        t0 = clock_now()
        alarm = Alarm(title="Astreinte injoignable", message="m", severity="critical",
                      assigned_user_id=uid1, is_oncall_alarm=True, source="oncall",
                      original_created_at=t0, created_at=t0)
        db.add(alarm)
        db.flush()
        db.add(AlarmNotification(alarm_id=alarm.id, user_id=uid1, notified_at=t0))
        db.commit()
        oncall_id = alarm.id
    finally:
        db.close()

    # Perimetre propre (INV-001) : n'empeche pas une alarme Izeron
    assert _send(client, admin_h, "izeron").status_code == 200
    active = {x["id"]: x for x in client.get("/api/alarms/active", headers=admin_h).json()}
    assert active[oncall_id]["site"] is None

    client.post("/api/test/advance-clock", params={"seconds": 150, "peer": "false"})
    assert _wait_for(lambda: any(a == oncall_id for _, a in _queue_rows()[1])), _queue_rows()
    sms, calls = _queue_rows()
    assert ("izeron", oncall_id) in calls
    assert any(site == "izeron" and "Astreinte injoignable" in body for site, body in sms), sms
    assert all(c["alarm_id"] != oncall_id for c in _pending(client, "calls", "beaufort"))
    assert any(c["alarm_id"] == oncall_id for c in _pending(client, "calls"))


def test_known_sites_exposed_for_web_form(setup):
    client, _, _ = setup
    r = client.get("/api/config/sites")
    assert r.status_code == 200, r.text
    assert r.json() == {"sites": ["izeron", "beaufort"], "default_site": "izeron"}
