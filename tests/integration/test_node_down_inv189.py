"""Tier 2 integration : INV-189 — nœud injoignable ou qui ne suit plus le leader depuis plus de 10 min
→ un email + un audit par épisode, puis un email « rétabli ».

Incident du 2026-11-09 : alarm-2 (node5, Beaufort) sans câble réseau ~45 min, personne prévenu.
Patroni simulé (seul GET /cluster est intercepté) : node2 leader `running` (timeline 290), les autres
`streaming`. Le vrai contrôle (`_run_node_down_check`) est appelé avec un `now` choisi (2030) ; le tick
de fond est neutralisé par `tests/integration/conftest.py` (fixture `real_node_down_check`).
"""
import io
import json
import urllib.request
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_ALERT_EMAIL = "direction.technique@charlesmurgat.test"
KEY = "node_down_alert"
T0 = datetime(2030, 1, 1, 12, 0)
MIN = timedelta(minutes=1)


def _member(n, role="replica", state="streaming", timeline=290):
    return {"name": f"node{n}", "role": role, "state": state, "host": f"10.99.0.{n}", "port": 5432,
            "api_url": f"http://10.99.0.{n}:8008/patroni", "timeline": timeline, "lag": 0}


def _healthy(leader=2):
    return [_member(n, role="leader", state="running") if n == leader else _member(n) for n in range(1, 8)]


def _without(members, name):
    return [m for m in members if m["name"] != name]


def _delete(key):
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    db = SessionLocal()
    try:
        db.query(SystemConfig).filter(SystemConfig.key == key).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _row(key):
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        return json.loads(row.value) if row else None
    finally:
        db.close()


def _audits(event_type):
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent
    db = SessionLocal()
    try:
        rows = db.query(AuditEvent).filter(AuditEvent.event_type == event_type).all()
        return [json.loads(r.details) for r in rows]
    finally:
        db.close()


@pytest.fixture
def patroni(monkeypatch, client, admin_headers, real_node_down_check):
    """Faux Patroni (`members` modifiable) ; `tick(m)` = le vrai contrôle à T0 + m minutes."""
    r = client.post("/api/config/system", json={"key": "alert_email", "value": _ALERT_EMAIL},
                    headers=admin_headers)
    assert r.status_code == 200
    for key in ("node_locations", "node_machines", "retiring_nodes", KEY):  # défauts INV-161, aucun épisode
        _delete(key)
    fake = {"members": _healthy()}
    real = urllib.request.urlopen

    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/cluster"):
            return io.BytesIO(json.dumps({"members": fake["members"]}).encode())
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    fake["tick"] = lambda minutes: real_node_down_check(T0 + minutes * MIN)
    yield fake
    _delete(KEY)


def test_node_absent_for_more_than_10_min_is_emailed_and_audited_once(patroni):
    patroni["members"] = _without(_healthy(), "node5")  # câble réseau débranché
    before = len(_audits("node_down"))
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        patroni["tick"](0)
        patroni["tick"](10)
        assert mail.call_count == 0, "10 min 00 : pas encore (strictement plus de 10 min)"
        patroni["tick"](11)
        assert mail.call_count == 1
        kw = mail.call_args.kwargs
        assert kw["to"] == _ALERT_EMAIL
        assert kw["subject"] == "[Alarme Murgat] Nœud injoignable : alarm-2 · node5 (Beaufort)"
        assert "absent de /cluster" in kw["body"]
        assert f"{T0.isoformat()} UTC" in kw["body"] and "11 min" in kw["body"]
        assert "onsite-2 · node2" in kw["body"], "le reste du cluster a toujours un leader"
        patroni["tick"](12)
        patroni["members"] = _without(_healthy(leader=3), "node5")  # bascule : node3 devient leader
        patroni["tick"](13)
        assert mail.call_count == 1, "un seul email par épisode, même après un changement de leader"
    assert _audits("node_down")[before:] == [{
        "node": "node5", "machine": "alarm-2", "location": "beaufort", "problem": "absent de /cluster",
        "since": T0.isoformat(), "minutes": 11}]
    assert _row(KEY) == {"node5": {"since": T0.isoformat(), "emailed": True}}


def test_recovered_node_gets_one_short_email_and_is_rearmed(patroni):
    patroni["members"] = _without(_healthy(), "node5")
    before = len(_audits("node_recovered"))
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        patroni["tick"](0)
        patroni["tick"](11)
        assert mail.call_count == 1
        patroni["members"] = _healthy()  # câble rebranché, node5 de nouveau en flux
        patroni["tick"](45)
        assert mail.call_count == 2
        kw = mail.call_args.kwargs
        assert kw["to"] == _ALERT_EMAIL
        assert kw["subject"] == "[Alarme Murgat] Nœud rétabli : alarm-2 · node5 (Beaufort)"
        assert f"{T0.isoformat()} UTC" in kw["body"] and "45 min" in kw["body"]
        patroni["tick"](46)
        assert mail.call_count == 2, "un seul email « rétabli »"
    assert _audits("node_recovered")[before:] == [{
        "node": "node5", "machine": "alarm-2", "location": "beaufort", "since": T0.isoformat(), "minutes": 45}]
    assert _row(KEY) == {}, "réarmé"


def test_two_nodes_each_get_their_own_email_at_their_own_time(patroni):
    patroni["members"] = _without(_healthy(), "node5")
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        patroni["tick"](0)
        patroni["members"] = [_member(4, state="running", timeline=289) if m["name"] == "node4" else m
                              for m in patroni["members"]]  # node4 reste sur l'ancienne timeline
        patroni["tick"](5)
        patroni["tick"](11)
        patroni["tick"](15)
        assert [c.kwargs["subject"] for c in mail.call_args_list] == [
            "[Alarme Murgat] Nœud injoignable : alarm-2 · node5 (Beaufort)"]
        patroni["tick"](16)
        assert [c.kwargs["subject"] for c in mail.call_args_list] == [
            "[Alarme Murgat] Nœud injoignable : alarm-2 · node5 (Beaufort)",
            "[Alarme Murgat] Nœud injoignable : alarm-1 · node4 (Beaufort)"]
        assert "running sur la timeline 289 (leader : 290)" in mail.call_args.kwargs["body"]


def test_background_tick_runs_check_off_event_loop_with_real_time(client, monkeypatch):
    """Lecture Patroni + DB + SMTP hors event loop ; horloge réelle : advance-clock ne déclenche rien."""
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
    monkeypatch.setattr(qm, "_run_site_modem_check", lambda now: None)
    monkeypatch.setattr(qm, "_run_node_down_check", check)

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
