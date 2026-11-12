"""Tier 2 integration : INV-194 — passerelle vivante sans Arduino (`report-state` `no_sensor`).

Incident du 2026-11-12 : Arduinos deplaces d'une machine a l'autre ; alarm-4, sans Arduino mais en
marche, a recu l'email INV-172 « passerelle muette ». Ici :
- vivacite : un rapport `no_sensor` tient la passerelle vivante pour INV-172 ; plus rien pendant
  10 min -> email (vrai controle `_run_gateway_silence_check`, `now` choisi) ;
- contact sec du site : `no_sensor` hors de l'ensemble des passerelles vivantes avec capteur
  (ni creation, ni blocage, ni resolution, ni desaccord INV-123) ;
- onglet Cluster (INV-192) : `dry_contact.state = "no_sensor"`, puis `silent`.
Horloge injectable (`/api/test/advance-clock`).
"""
from datetime import timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

GW_KEY = "test-gateway-key-INV-194"
GW = {"X-Gateway-Key": GW_KEY}
_ALERT_EMAIL = "direction.technique@charlesmurgat.test"
_SILENCE_KEY = "gateway_silence_emailed"


def _db(fn):
    from backend.app.database import SessionLocal
    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


@pytest.fixture
def setup(client, admin_headers, monkeypatch):
    from backend.app.models import SystemConfig
    monkeypatch.setenv("GATEWAY_KEY", GW_KEY)

    def reset():
        assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
        assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200

        def drop_key(db):
            db.query(SystemConfig).filter(SystemConfig.key == _SILENCE_KEY).delete()
            db.commit()
        _db(drop_key)

    reset()
    assert client.post("/api/config/system", headers=admin_headers,
                       json={"key": "alert_email", "value": _ALERT_EMAIL}).status_code == 200
    yield client
    reset()


def _report(client, gateway_id, state, site="beaufort"):
    r = client.post("/internal/alarms/report-state", headers=GW,
                    json={"gateway_id": gateway_id, "state": state, "site": site})
    assert r.status_code == 200, r.text
    return r.json()


def _advance(client, seconds):
    assert client.post("/api/test/advance-clock", params={"seconds": seconds, "peer": "false"}).status_code == 200


def _row(gateway_id):
    from backend.app.models import GatewayState
    return _db(lambda db: (lambda g: (g.state, g.last_seen))(
        db.query(GatewayState).filter(GatewayState.gateway_id == gateway_id).one()))


def _gateway_alarms():
    """(status, sensor_dissensus_since) des alarmes contact sec, dans l'ordre de creation."""
    from backend.app.models import Alarm
    return _db(lambda db: [(a.status, a.sensor_dissensus_since) for a in
                           db.query(Alarm).filter(Alarm.source == "gateway_dry_contact").order_by(Alarm.id)])


def test_alive_without_sensor_is_not_silent_until_it_sends_nothing(setup, real_gateway_silence_check):
    client, check = setup, real_gateway_silence_check
    _report(client, "alarm-4", "no_sensor", site="izeron")
    state, t0 = _row("alarm-4")
    assert state == "no_sensor"
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _advance(client, 9 * 60)
        _report(client, "alarm-4", "no_sensor", site="izeron")  # toujours en marche, toujours sans Arduino
        _, t1 = _row("alarm-4")
        check(t0 + timedelta(minutes=11))
        assert mail.call_count == 0, "vivante sans Arduino : pas muette"
        check(t1 + timedelta(minutes=10))  # plus rien depuis 10 min : vraiment muette
        assert mail.call_count == 1
        assert mail.call_args.kwargs["subject"] == "[Alarme Murgat] Passerelle alarm-4 (Izeron) muette depuis 10 min"
        assert mail.call_args.kwargs["to"] == _ALERT_EMAIL


def test_open_and_no_sensor_creates_then_closing_resolves_without_dissensus(setup, monkeypatch):
    # Comme INV-123 : re-POSTs espaces de 6 min sans qu'une passerelle passe pour muette entre-temps.
    monkeypatch.setenv("GATEWAY_LIVENESS_WINDOW_SECONDS", "3600")
    client = setup
    with patch("backend.app.email_service.send_alert_email") as mail:
        assert _report(client, "alarm-2", "no_sensor") == {"alarm_active": False, "dissensus": False}
        assert _report(client, "alarm-1", "open") == {"alarm_active": True, "dissensus": False}
        assert _report(client, "alarm-2", "no_sensor") == {"alarm_active": True, "dissensus": False}
        [(status, since)] = _gateway_alarms()
        assert status in ("active", "escalated") and since is None, "alarme creee, sans desaccord"

        _advance(client, 6 * 60)  # au-dela du seuil de desaccord (5 min)
        _report(client, "alarm-1", "open")
        assert _report(client, "alarm-2", "no_sensor") == {"alarm_active": True, "dissensus": False}
        [(status, since)] = _gateway_alarms()
        assert status in ("active", "escalated") and since is None

        assert _report(client, "alarm-1", "closed") == {"alarm_active": False, "dissensus": False}
        assert _gateway_alarms() == [("resolved", None)], "resolue, pas bloquee par la passerelle sans capteur"
    subjects = [c.kwargs.get("subject", "") for c in mail.call_args_list]
    assert not any("discordance" in s.lower() for s in subjects), subjects


def test_no_sensor_alone_neither_creates_nor_resolves(setup):
    client = setup
    assert _report(client, "alarm-2", "no_sensor") == {"alarm_active": False, "dissensus": False}
    assert _gateway_alarms() == [], "aucune alarme sans capteur"
    _report(client, "alarm-1", "open")
    _advance(client, 20)  # alarm-1 se tait (> 15 s) : son contact n'est plus lu
    _report(client, "alarm-2", "no_sensor")
    [(status, _)] = _gateway_alarms()
    assert status in ("active", "escalated"), "fail-to-alarm : une passerelle sans capteur ne resout pas"


def test_cluster_tab_sees_no_sensor_then_silent(setup, admin_headers):
    client = setup

    def dry_contact():
        body = client.get("/api/system/gateways", headers=admin_headers).json()
        return {g["gateway_id"]: g for g in body}["alarm-4"]["dry_contact"]

    _report(client, "alarm-4", "no_sensor", site="izeron")
    assert dry_contact() == {"state": "no_sensor", "last_seen": None}
    _advance(client, 20)
    assert dry_contact()["state"] == "silent"
