"""Tier 2 integration : INV-185 — site sans modem depuis 10 min → un email + un audit par épisode.

Revue PR #296 (F1) : un modem absent dès le démarrage d'une machine n'alertait personne (le watchdog
ignore un modem jamais vu, l'Arduino garde la passerelle vivante pour INV-172). Le vrai contrôle
(`_run_site_modem_check`) est appelé avec un `now` choisi ; les états viennent du vrai
`POST /internal/alarms/modem-status`. Le tick de fond est neutralisé par `tests/integration/conftest.py`
(fixture `real_site_modem_check`, qui fournit le vrai contrôle).
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_ALERT_EMAIL = "direction.technique@charlesmurgat.test"
GATEWAY_KEY = "test-gateway-key-INV-185"
KEY = "site_modem_alert"
MIN = timedelta(minutes=1)


def _clean():
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    db = SessionLocal()
    try:
        db.query(SystemConfig).filter((SystemConfig.key == KEY) | SystemConfig.key.like("modem_status%")).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def check(monkeypatch, client, admin_headers, real_site_modem_check):
    """Le vrai tick INV-185, état propre (le contrôle de fond est neutralisé par conftest)."""
    monkeypatch.setenv("GATEWAY_KEY", GATEWAY_KEY)
    r = client.post("/api/config/system", json={"key": "alert_email", "value": _ALERT_EMAIL},
                    headers=admin_headers)
    assert r.status_code == 200
    _clean()
    yield real_site_modem_check
    _clean()


def _row(key):
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        return json.loads(row.value) if row else None
    finally:
        db.close()


def _report(client, gateway_id, site):
    """modem-status d'une passerelle avec modem ; renvoie son reported_at."""
    r = client.post("/internal/alarms/modem-status",
                    json={"registered": True, "tech": "LTE", "rsrp": -95.0, "csq": 20,
                          "gateway_id": gateway_id, "site": site},
                    headers={"X-Gateway-Key": GATEWAY_KEY})
    assert r.status_code == 200, r.text
    return datetime.fromisoformat(_row(f"modem_status:{gateway_id}")["reported_at"])


def _age(gateway_id, age):
    """Vieillit l'état d'une passerelle (modem débranché depuis `age`)."""
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == f"modem_status:{gateway_id}").one()
        value = json.loads(row.value)
        value["reported_at"] = (datetime.fromisoformat(value["reported_at"]) - age).isoformat()
        row.value = json.dumps(value)
        db.commit()
    finally:
        db.close()


def _audits():
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent
    db = SessionLocal()
    try:
        rows = db.query(AuditEvent).filter(AuditEvent.event_type == "site_without_modem").all()
        return [json.loads(r.details) for r in rows]
    finally:
        db.close()


def _beaufort(mail):
    return [c.kwargs for c in mail.call_args_list if "Beaufort" in c.kwargs["subject"]]


def test_site_without_modem_for_10_min_is_emailed_and_audited_once(client, check):
    t0 = _report(client, "alarm-1", "beaufort")
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        check(t0 + 9 * MIN)
        assert mail.call_count == 0, "modem encore frais"
        check(t0 + 11 * MIN)
        assert mail.call_count == 1  # Izeron (aucun état) : épisode ouvert à t0 + 9, pas encore 10 min
        kw = mail.call_args.kwargs
        assert kw["to"] == _ALERT_EMAIL
        assert kw["subject"] == "[Alarme Murgat] Site Beaufort sans modem depuis 11 min"
        assert "SMS et appels impossibles" in kw["body"] and "SIM7600" in kw["body"]
        assert t0.isoformat() in kw["body"]
        check(t0 + 12 * MIN)
        assert mail.call_count == 1, "un seul email par épisode"
    audits = _audits()[before:]
    assert audits == [{"site": "beaufort", "since": t0.isoformat(), "minutes": 11}]
    assert _row(KEY) == {"beaufort": {"since": t0.isoformat(), "emailed": True},
                         "izeron": {"since": (t0 + 9 * MIN).isoformat(), "emailed": False}}


def test_new_modem_status_rearms_the_site(client, check):
    t0 = _report(client, "alarm-1", "beaufort")
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        check(t0 + 11 * MIN)
        assert len(_beaufort(mail)) == 1
        t1 = _report(client, "alarm-1", "beaufort")  # le modem est revenu
        check(t1 + MIN)
        assert "beaufort" not in _row(KEY), "réarmé"
        check(t1 + 10 * MIN)
        assert len(_beaufort(mail)) == 2
        assert _beaufort(mail)[-1]["subject"].endswith("sans modem depuis 10 min")


def test_site_that_never_reported_a_modem_is_emailed_10_min_after_the_first_check(client, check):
    start = datetime(2026, 11, 5, 12, 0, 0)
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        check(start)
        check(start + 9 * MIN)
        assert mail.call_count == 0
        check(start + 10 * MIN)
        subjects = sorted(c.kwargs["subject"] for c in mail.call_args_list)
        assert subjects == ["[Alarme Murgat] Site Beaufort sans modem depuis 10 min",
                            "[Alarme Murgat] Site Izeron sans modem depuis 10 min"]
        assert "aucun" in mail.call_args.kwargs["body"]


def test_modem_moved_to_another_machine_of_the_same_site_is_not_alerted(client, check):
    _report(client, "alarm-1", "beaufort")
    _age("alarm-1", timedelta(days=2))  # l'ancienne machine ne rapporte plus
    t = _report(client, "alarm-2", "beaufort")
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        check(t + MIN)
        assert _beaufort(mail) == []
    assert "beaufort" not in (_row(KEY) or {})


def test_background_tick_runs_check_off_event_loop_with_real_time(client, monkeypatch):
    """Lecture DB + SMTP hors event loop ; horloge réelle : advance-clock ne déclenche rien."""
    import asyncio
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
    monkeypatch.setattr(qm, "_run_gateway_silence_check", lambda now: None)
    monkeypatch.setattr(qm, "_run_site_modem_check", check)

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
    assert abs(now - datetime.utcnow()) < timedelta(minutes=1)
