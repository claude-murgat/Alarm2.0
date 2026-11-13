"""Tier 2 integration : INV-195 — site sans contact sec lisible depuis plus de 10 min → un email + un audit
par épisode, puis « rétabli ».

Depuis INV-194, une passerelle sans Arduino lisible rapporte `no_sensor` et ne déclenche plus INV-172 : un
site qui perdait son dernier Arduino ne prévenait personne. États réels par `POST /internal/alarms/report-state`,
horloge injectable (`/api/test/advance-clock`) ; le vrai contrôle (`_run_site_dry_contact_check`) est appelé
avec `clock_now()`. Le tick de fond est neutralisé par `tests/integration/conftest.py`
(fixture `real_site_dry_contact_check`, qui fournit le vrai contrôle).
"""
import json
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

GW = {"X-Gateway-Key": "test-gateway-key-INV-195"}
_ALERT_EMAIL = "direction.technique@charlesmurgat.test"
KEY = "site_dry_contact_alert"


def _db(fn):
    from backend.app.database import SessionLocal
    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


def _key():
    from backend.app.models import SystemConfig
    row = _db(lambda db: db.query(SystemConfig).filter(SystemConfig.key == KEY).first())
    return json.loads(row.value) if row else None


def _audits(event_type):
    from backend.app.models import AuditEvent
    return _db(lambda db: [json.loads(r.details) for r in
                           db.query(AuditEvent).filter(AuditEvent.event_type == event_type).order_by(AuditEvent.id)])


@pytest.fixture
def setup(client, admin_headers, monkeypatch, real_site_dry_contact_check):
    """(client, vrai contrôle INV-195) ; gateway_states, audit, horloge et clé remis à zéro."""
    from backend.app.models import SystemConfig
    monkeypatch.setenv("GATEWAY_KEY", GW["X-Gateway-Key"])

    def reset():
        assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
        assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200

        def drop_key(db):
            db.query(SystemConfig).filter(SystemConfig.key == KEY).delete()
            db.commit()
        _db(drop_key)

    reset()
    assert client.post("/api/config/system", headers=admin_headers,
                       json={"key": "alert_email", "value": _ALERT_EMAIL}).status_code == 200
    yield client, real_site_dry_contact_check
    reset()


def _report(client, gateway_id, state, site="beaufort"):
    r = client.post("/internal/alarms/report-state", headers=GW,
                    json={"gateway_id": gateway_id, "state": state, "site": site})
    assert r.status_code == 200, r.text


def _advance(client, seconds):
    assert client.post("/api/test/advance-clock", params={"seconds": seconds, "peer": "false"}).status_code == 200


def _last_seen(gateway_id):
    from backend.app.models import GatewayState
    return _db(lambda db: db.query(GatewayState).filter(GatewayState.gateway_id == gateway_id).one().last_seen)


def test_site_losing_its_last_readable_contact_is_emailed_once_then_retabli(setup):
    from backend.app import clock
    client, check = setup
    _report(client, "alarm-1", "closed")
    _report(client, "alarm-2", "closed")
    _report(client, "alarm-4", "no_sensor", site="izeron")  # Izeron n'a jamais eu d'Arduino lisible
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        check(clock.now())
        assert _key() == {"beaufort": {"since": None, "emailed": False}}, "Beaufort lisible, donc suivi"

        _report(client, "alarm-1", "no_sensor")  # Arduino d'alarm-1 débranché ; alarm-2 se tait
        _advance(client, 20)
        _report(client, "alarm-1", "no_sensor")
        _report(client, "alarm-4", "no_sensor", site="izeron")
        t1 = clock.now()
        check(t1)
        assert _key() == {"beaufort": {"since": t1.isoformat(), "emailed": False}}

        _advance(client, 9 * 60)
        _report(client, "alarm-1", "no_sensor")
        check(clock.now())
        assert mail.call_count == 0, "9 min sans contact lisible : rien"

        _advance(client, 2 * 60)
        _report(client, "alarm-1", "no_sensor")
        _report(client, "alarm-4", "no_sensor", site="izeron")
        check(clock.now())
        assert mail.call_count == 1, "seul Beaufort : Izeron n'a jamais eu de contact sec"
        kw = mail.call_args.kwargs
        assert kw["to"] == _ALERT_EMAIL
        assert kw["subject"] == "[Alarme Murgat] Site Beaufort sans contact sec"
        body = kw["body"]
        assert "Beaufort" in body and t1.isoformat() in body and "11 min" in body
        assert "alarm-1 : sans Arduino (no_sensor)" in body
        assert f"alarm-2 : muette depuis {_last_seen('alarm-2').isoformat()} UTC" in body
        assert "ne peut plus déclencher d'alarme" in body
        assert "alarm-4" not in body, "passerelle d'un autre site"

        check(clock.now())
        assert mail.call_count == 1, "un seul email par épisode"
        assert _audits("site_dry_contact_lost") == [{"site": "beaufort", "since": t1.isoformat(), "minutes": 11}]
        assert _key() == {"beaufort": {"since": t1.isoformat(), "emailed": True}}

        _advance(client, 60)
        _report(client, "alarm-1", "closed")  # Arduino rebranché
        check(clock.now())
        assert mail.call_count == 2
        kw = mail.call_args.kwargs
        assert kw["to"] == _ALERT_EMAIL
        assert kw["subject"] == "[Alarme Murgat] Site Beaufort : contact sec rétabli"
        assert t1.isoformat() in kw["body"] and "12 min" in kw["body"] and "alarm-1" in kw["body"]
        assert _audits("site_dry_contact_recovered") == [
            {"site": "beaufort", "since": t1.isoformat(), "minutes": 12}]
        assert _key() == {"beaufort": {"since": None, "emailed": False}}, "réarmé"

        check(clock.now())
        assert mail.call_count == 2


def test_background_tick_runs_check_off_event_loop_with_clock_now(client, monkeypatch):
    """Lecture DB + SMTP hors event loop ; horloge injectable, comme INV-172 (vivacité sur last_seen)."""
    import asyncio
    from datetime import datetime, timedelta
    from backend.app import clock
    from backend.app import quorum_monitor as qm
    calls = []

    def check(now):
        try:
            asyncio.get_running_loop()
            calls.append(("event loop", now))
        except RuntimeError:
            calls.append(("thread", now))
    monkeypatch.setattr(qm, "_QUORUM_TICK_SECONDS", 0)
    monkeypatch.setattr(qm, "_build_snapshot_sync",
                        lambda now: qm.ClusterSnapshot(has_quorum=True, patroni_reachable=True, timestamp=now))
    monkeypatch.setattr(qm, "_run_quorum_check", lambda *a, **kw: None)
    monkeypatch.setattr(qm, "_run_replica_reinit_check", lambda now: None)
    monkeypatch.setattr(qm, "_run_site_dry_contact_check", check)

    async def one_tick():
        task = asyncio.create_task(qm.quorum_monitor_loop())
        while not calls:
            await asyncio.sleep(0.01)
        task.cancel()
    clock.advance(24 * 3600)
    try:
        asyncio.run(asyncio.wait_for(one_tick(), timeout=5))
    finally:
        clock.reset()
    where, now = calls[0]
    assert where == "thread"
    assert abs(now - (datetime.utcnow() + timedelta(hours=24))) < timedelta(minutes=1)
