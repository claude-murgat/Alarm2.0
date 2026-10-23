"""
Tier 2 integration : multi-site Izeron / Beaufort, suite d'INV-155/156.

- INV-157 : agregation OU, desaccord (dissensus) et resolution automatique du contact
  sec site par site. Beaufort ouvert + Izeron ferme n'est pas un desaccord ; la
  fermeture d'un site ne resout que l'alarme de ce site.
- INV-158 : etat du modem par passerelle (`gateway_id`, site), regles d'INV-129 pour
  chaque passerelle ; passerelle sans `gateway_id` -> `legacy`.
- INV-160 : la reponse « 1 » par SMS acquitte l'alarme du site du dernier SMS envoye a
  ce numero ; l'autre site continue. Repli : premiere alarme en cours notifiee.
- INV-155 (reste) : le push d'alarme porte le site (`data["site"]`).

Vraie boucle d'escalade (tick 1 s) + horloge avancee pour la creation des SMS.
"""
import time

import pytest

pytestmark = pytest.mark.integration

GW_KEY = "test-gateway-key-INV-157"
GW = {"X-Gateway-Key": GW_KEY}
PHONE = "+33600000160"


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]["id"]


def _wipe_modem_status():
    from sqlalchemy import text
    from backend.app.database import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM system_config WHERE key LIKE 'modem_status%'"))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def setup(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", GW_KEY)
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200
    _wipe_modem_status()
    admin_h, _ = _login(client, "admin", "admin123")
    user1_h, uid1 = _login(client, "user1", "user123")
    assert client.patch(f"/api/users/{uid1}", headers=admin_h, json={"phone_number": PHONE}).status_code == 200
    assert client.post("/api/config/system", headers=admin_h,
                       json={"key": "escalation_tick_seconds", "value": "1"}).status_code == 200
    yield client, admin_h, user1_h, uid1
    client.patch(f"/api/users/{uid1}", headers=admin_h, json={"phone_number": None})
    client.post("/api/config/system", headers=admin_h, json={"key": "escalation_tick_seconds", "value": "10"})
    client.post("/api/test/reset-clock", params={"peer": "false"})
    client.post("/api/test/reset", params={"peer": "false"})
    _wipe_modem_status()


def _advance(client, seconds):
    assert client.post("/api/test/advance-clock",
                       params={"seconds": seconds, "peer": "false"}).status_code == 200


def _active_by_site(client, h):
    return {a["site"]: a for a in client.get("/api/alarms/active", headers=h).json()}


def _wait_for(cond, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.3)
    return cond()


# ── INV-157 : contact sec site par site ────────────────────────────────────

def _report(client, gateway_id, state, site=None):
    body = {"gateway_id": gateway_id, "state": state}
    if site is not None:
        body["site"] = site
    r = client.post("/internal/alarms/report-state", headers=GW, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _dissensus_since(alarm_id):
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm
    db = SessionLocal()
    try:
        return db.query(Alarm).filter(Alarm.id == alarm_id).first().sensor_dissensus_since
    finally:
        db.close()


def test_beaufort_open_izeron_closed_is_not_a_dissensus(setup):
    client, admin_h, _, _ = setup
    _report(client, "alarm-3", "open", site="beaufort")
    r = _report(client, "alarm-1", "closed", site="izeron")
    assert r == {"alarm_active": False, "dissensus": False}, (
        "Izeron ferme ne doit voir ni l'alarme ni le contact de Beaufort : " + str(r))
    active = _active_by_site(client, admin_h)
    assert list(active) == ["beaufort"], f"aucune alarme Izeron ne doit naitre : {list(active)}"
    assert _dissensus_since(active["beaufort"]["id"]) is None

    # Desaccord entre les passerelles d'un meme site : toujours detecte
    r = _report(client, "alarm-4", "closed", site="beaufort")
    assert r == {"alarm_active": True, "dissensus": True}, r
    assert _dissensus_since(active["beaufort"]["id"]) is not None


def test_closing_a_site_resolves_only_its_own_alarm(setup):
    client, admin_h, _, _ = setup
    _report(client, "onsite-2", "open")  # passerelle d'avant INV-155 : default_site (izeron)
    _report(client, "alarm-3", "open", site="beaufort")
    assert sorted(_active_by_site(client, admin_h)) == ["beaufort", "izeron"]

    r = _report(client, "onsite-2", "closed")
    assert r["alarm_active"] is False, r
    assert list(_active_by_site(client, admin_h)) == ["beaufort"], (
        "Izeron ferme : son alarme est resolue, celle de Beaufort continue")

    _report(client, "alarm-1", "closed", site="izeron")
    assert list(_active_by_site(client, admin_h)) == ["beaufort"], (
        "la fermeture a Izeron ne resout jamais l'alarme de Beaufort")
    r = _report(client, "alarm-3", "closed", site="beaufort")
    assert r["alarm_active"] is False, r
    assert _active_by_site(client, admin_h) == {}


# ── INV-158 : etat du modem par passerelle ─────────────────────────────────

LTE = {"registered": True, "tech": "LTE", "rsrp": -95.0, "csq": 20}
NO_NET = {"registered": False, "tech": None, "rsrp": None, "csq": 0}


def _modem(client, **body):
    assert client.post("/internal/alarms/modem-status", headers=GW, json=body).status_code == 200


def _modem_status(client, h):
    r = client.get("/api/system/modem-status", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_modem_status_listed_per_gateway_with_its_site(setup):
    client, _, user1_h, _ = setup
    _modem(client, gateway_id="alarm-1", site="izeron", **LTE)
    _modem(client, gateway_id="alarm-3", site="beaufort", **NO_NET)
    _modem(client, gateway_id="alarm-1", site="izeron", **LTE)  # rapport suivant : remplace

    body = _modem_status(client, user1_h)
    entries = {g["gateway_id"]: g for g in body["gateways"]}
    assert sorted(entries) == ["alarm-1", "alarm-3"], body
    assert {k: v for k, v in entries["alarm-1"].items() if k != "updated_at"} == {
        "gateway_id": "alarm-1", "site": "izeron", "state": "ok", "bars": 3, "tech": "LTE", "rsrp": -95.0}
    assert {k: v for k, v in entries["alarm-3"].items() if k != "updated_at"} == {
        "gateway_id": "alarm-3", "site": "beaufort", "state": "no_network", "bars": 0, "tech": None, "rsrp": None}
    assert entries["alarm-1"]["updated_at"] and entries["alarm-3"]["updated_at"]
    # App d'avant INV-158 (lit un objet) : la passerelle la plus mal en point
    assert (body["gateway_id"], body["site"], body["state"], body["bars"]) == ("alarm-3", "beaufort", "no_network", 0)


def test_each_gateway_goes_unknown_on_its_own(setup):
    client, _, user1_h, _ = setup
    _modem(client, gateway_id="alarm-1", site="izeron", **LTE)
    _advance(client, 120)
    _modem(client, gateway_id="alarm-3", site="beaufort", **LTE)
    _advance(client, 70)  # alarm-1 muette depuis 190 s, alarm-3 depuis 70 s

    body = _modem_status(client, user1_h)
    assert {g["gateway_id"]: (g["state"], g["bars"]) for g in body["gateways"]} == {
        "alarm-1": ("unknown", 0), "alarm-3": ("ok", 3)}, body
    assert (body["gateway_id"], body["state"]) == ("alarm-1", "unknown")


def test_gateway_without_gateway_id_is_legacy(setup):
    client, _, user1_h, _ = setup
    assert _modem_status(client, user1_h) == {
        "gateway_id": None, "site": None, "state": "unknown", "bars": 0, "tech": None,
        "rsrp": None, "updated_at": None, "gateways": []}

    _modem(client, **LTE)  # passerelle d'avant INV-155 : ni gateway_id ni site
    gws = _modem_status(client, user1_h)["gateways"]
    assert [(g["gateway_id"], g["site"], g["state"]) for g in gws] == [("legacy", "izeron", "ok")]

    _modem(client, site="beaufort", **LTE)  # passerelle INV-155 : site sans gateway_id
    gws = _modem_status(client, user1_h)["gateways"]
    assert [(g["gateway_id"], g["site"]) for g in gws] == [("legacy", "beaufort")]

    # Muette > 180 s : c'est l'ancienne passerelle, mise a jour depuis -> plus listee
    _advance(client, 181)
    body = _modem_status(client, user1_h)
    assert (body["gateways"], body["state"]) == ([], "unknown"), body


# ── INV-160 : reponse SMS « 1 » -> alarme du dernier SMS recu ──────────────

def _send(client, h, site):
    r = client.post("/api/alarms/send", headers=h,
                    json={"site": site, "title": "Pompe", "message": "m", "severity": "critical"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _sms_ids_by_site():
    from backend.app.database import SessionLocal
    from backend.app.models import SmsQueue
    db = SessionLocal()
    try:
        return {r.site: r.id for r in db.query(SmsQueue).filter(SmsQueue.to_number == PHONE)}
    finally:
        db.close()


def _two_alarms_with_sms_sent(client, admin_h, last_site):
    """Alarme Izeron puis Beaufort (user1 notifie des deux), SMS envoyes : `last_site` en dernier."""
    ids = {"izeron": _send(client, admin_h, "izeron"), "beaufort": _send(client, admin_h, "beaufort")}
    _advance(client, 150)
    assert _wait_for(lambda: len(_sms_ids_by_site()) == 2), _sms_ids_by_site()
    sms = _sms_ids_by_site()
    first = "izeron" if last_site == "beaufort" else "beaufort"
    assert client.post(f"/internal/sms/{sms[first]}/sent", headers=GW).status_code == 200
    _advance(client, 5)
    assert client.post(f"/internal/sms/{sms[last_site]}/sent", headers=GW).status_code == 200
    return ids


def _ack_by_sms(client):
    return client.post("/internal/alarms/active/ack-by-phone", headers=GW, json={"phone_number": PHONE})


@pytest.mark.parametrize("last_site", ["izeron", "beaufort"])
def test_sms_reply_acks_alarm_of_last_sms_received(setup, last_site):
    client, admin_h, _, _ = setup
    ids = _two_alarms_with_sms_sent(client, admin_h, last_site)
    other = "izeron" if last_site == "beaufort" else "beaufort"

    r = _ack_by_sms(client)
    assert r.status_code == 200, r.text
    assert r.json()["alarm_id"] == ids[last_site], (
        f"« 1 » repond au dernier SMS recu ({last_site}) : {r.json()}")
    active = _active_by_site(client, admin_h)
    assert list(active) == [other], f"l'alarme de {other} doit continuer : {list(active)}"


def test_sms_reply_falls_back_when_last_sms_alarm_is_over(setup):
    client, admin_h, _, _ = setup
    ids = _two_alarms_with_sms_sent(client, admin_h, "beaufort")
    assert client.post(f"/api/alarms/{ids['beaufort']}/resolve", headers=admin_h).status_code == 200

    r = _ack_by_sms(client)
    assert r.status_code == 200, r.text
    assert r.json()["alarm_id"] == ids["izeron"], r.json()
    assert _active_by_site(client, admin_h) == {}


# ── INV-155 (reste) : le push d'alarme porte le site ───────────────────────

def test_alarm_pushes_carry_site(setup):
    client, admin_h, user1_h, _ = setup
    assert client.post("/api/devices/fcm-token", headers=user1_h,
                       json={"token": "tok-user1-inv155", "device_id": "dev-user1-inv155"}).status_code == 200
    client.post("/api/test/reset-fcm")
    alarm_id = _send(client, admin_h, "beaufort")
    assert client.post("/api/test/trigger-escalation").status_code == 200
    assert client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_h).status_code == 200
    _report(client, "alarm-3", "open", site="beaufort")  # contact sec

    pushes = [f["data"] for f in client.get("/api/test/last-fcm").json() if f["data"].get("alarm_id")]
    kinds = {d.get("type", "alarm") for d in pushes if d["alarm_id"] == str(alarm_id)}
    assert kinds == {"alarm", "alarm_resolved"}, pushes
    assert len(pushes) >= 3 and all(d.get("site") == "beaufort" for d in pushes), pushes
