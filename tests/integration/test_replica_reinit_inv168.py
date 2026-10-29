"""Tier 2 integration : INV-168 — le leader re-clone un replica Patroni bloque.

Patroni simule (seules les URL Patroni sont interceptees) : GET /cluster renvoie node3 leader
(timeline 256) et des replicas ; les POST /reinitialize sont enregistres. node4 est bloque comme
a l'incident du 2026-10-29 : state « running », timeline 253.

Date de test dans le futur : un tick de fond de quorum_monitor_loop (horloge reelle) qui
passerait pendant le test ne peut rien declencher.
"""
import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_ALERT_EMAIL = "direction.technique@charlesmurgat.test"
T0 = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def _member(n, role="replica", state="streaming", timeline=256):
    return {"name": f"node{n}", "role": role, "state": state, "api_url": f"http://10.99.0.{n}:8008/patroni",
            "host": f"10.99.0.{n}", "port": 5432, "timeline": timeline, "lag": 0}


def _members(*stuck):
    return [_member(3, role="leader", state="running")] + [
        _member(n, state="running", timeline=253) if n in stuck else _member(n) for n in (1, 2, 4, 5)]


@pytest.fixture
def patroni(monkeypatch, client, admin_headers):
    """Faux Patroni. `fail_reinit` = le POST /reinitialize leve (Patroni du membre injoignable)."""
    from backend.app import quorum_monitor
    monkeypatch.setattr(quorum_monitor, "_stuck_since", {})
    monkeypatch.setattr(quorum_monitor, "_last_handled", {})
    r = client.post("/api/config/system", json={"key": "alert_email", "value": _ALERT_EMAIL},
                    headers=admin_headers)
    assert r.status_code == 200
    fake = {"cluster": {"members": _members(4)}, "posts": [], "fail_reinit": False}
    real = urllib.request.urlopen

    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/cluster"):
            return io.BytesIO(json.dumps(fake["cluster"]).encode())
        if url.endswith("/reinitialize"):
            fake["posts"].append((url, req.get_method(), json.loads(req.data)))
            if fake["fail_reinit"]:
                raise urllib.error.URLError("connection refused")
            return io.BytesIO(b"reinitialize started")
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return fake


def _ticks(*minutes):
    from backend.app.quorum_monitor import _run_replica_reinit_check
    for m in minutes:
        _run_replica_reinit_check(T0 + timedelta(minutes=m))


def _audits():
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent
    db = SessionLocal()
    try:
        rows = db.query(AuditEvent).filter(AuditEvent.event_type == "replica_reinitialized").all()
        return [json.loads(r.details) for r in rows]
    finally:
        db.close()


def test_stuck_replica_reinitialized_once_audited_and_emailed(patroni):
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(0, 5, 9)
        assert patroni["posts"] == [], "reinit avant 10 min de blocage continu"
        _ticks(10, 11, 30)

    assert patroni["posts"] == [("http://10.99.0.4:8008/reinitialize", "POST", {"force": True})]
    audits = _audits()[before:]
    assert len(audits) == 1
    assert audits[0]["member"] == "node4"
    assert audits[0]["reason"] == "state=running timeline=253 leader_timeline=256"
    assert mail.call_count == 1
    assert mail.call_args.kwargs["to"] == _ALERT_EMAIL
    assert "node4" in mail.call_args.kwargs["subject"]


def test_more_than_half_stuck_emails_without_reinit(patroni):
    patroni["cluster"] = {"members": _members(1, 4, 5)}
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(0, 10, 11, 30)
    assert patroni["posts"] == []
    assert mail.call_count == 1, "une alerte, puis silence pendant le cooldown d'1 h"
    assert mail.call_args.kwargs["to"] == _ALERT_EMAIL


def test_reinit_http_failure_is_audited_and_does_not_raise(patroni):
    patroni["fail_reinit"] = True
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(0, 10, 11)
    assert len(patroni["posts"]) == 1, "pas de nouvel essai avant 1 h"
    audits = _audits()[before:]
    assert len(audits) == 1 and audits[0]["ok"] is False
    assert mail.call_count == 1


def test_delay_follows_system_config(patroni, client, admin_headers):
    r = client.post("/api/config/system", json={"key": "replica_reinit_after_minutes", "value": "2"},
                    headers=admin_headers)
    assert r.status_code == 200
    try:
        with patch("backend.app.quorum_monitor.send_alert_email"):
            _ticks(0, 1)
            assert patroni["posts"] == []
            _ticks(2)
        assert len(patroni["posts"]) == 1
    finally:
        client.post("/api/config/system", json={"key": "replica_reinit_after_minutes", "value": "10"},
                    headers=admin_headers)
