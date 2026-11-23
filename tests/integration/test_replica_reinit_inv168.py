"""Tier 2 integration : INV-168 — le leader re-clone un replica Patroni bloque.

Patroni simule (seules les URL Patroni sont interceptees) : GET /cluster renvoie node3 leader
(timeline 256) et des replicas ; les POST /reinitialize sont enregistres. node4 est bloque comme
a l'incident du 2026-10-29 : state « running », timeline 253.

Le controle de la boucle de fond est neutralise pour la session (conftest,
`real_replica_reinit_check`) : seuls les ticks du test (`ticks`, date en 2030) touchent l'etat.
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


def _member(n, role="replica", state="streaming", timeline=256, lag=0):
    return {"name": f"node{n}", "role": role, "state": state, "api_url": f"http://10.99.0.{n}:8008/patroni",
            "host": f"10.99.0.{n}", "port": 5432, "timeline": timeline, "lag": lag}


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


@pytest.fixture
def ticks(real_replica_reinit_check):
    def run(*minutes):
        for m in minutes:
            real_replica_reinit_check(T0 + timedelta(minutes=m))
    return run


def _audits():
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent
    db = SessionLocal()
    try:
        rows = db.query(AuditEvent).filter(AuditEvent.event_type == "replica_reinitialized").all()
        return [json.loads(r.details) for r in rows]
    finally:
        db.close()


def test_stuck_replica_reinitialized_once_audited_and_emailed(patroni, ticks):
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        ticks(0, 5, 9)
        assert patroni["posts"] == [], "reinit avant 10 min de blocage continu"
        ticks(10, 11, 30)

    assert patroni["posts"] == [("http://10.99.0.4:8008/reinitialize", "POST", {"force": True})]
    audits = _audits()[before:]
    assert len(audits) == 1
    assert audits[0]["member"] == "node4"
    assert audits[0]["reason"] == "state=running timeline=253 leader_timeline=256"
    assert mail.call_count == 1
    assert mail.call_args.kwargs["to"] == _ALERT_EMAIL
    assert "node4" in mail.call_args.kwargs["subject"]


def test_no_healthy_replica_emails_without_reinit(patroni, ticks):
    patroni["cluster"] = {"members": _members(1, 2, 4, 5)}
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        ticks(0, 10, 11, 30)
    assert patroni["posts"] == []
    assert mail.call_count == 1, "une alerte, puis silence pendant le cooldown d'1 h"
    assert mail.call_args.kwargs["to"] == _ALERT_EMAIL


def test_incident_2026_11_06_reinit_one_at_a_time(patroni, ticks):
    """Beaufort redemarre pendant un failover : leader node2 (timeline 257), node1/node3 sains,
    node4/node6/node7 bloques (3 replicas sur 5), node5 absent. node4 a 10 min, rien tant
    qu'il est bloque, node6 des qu'il repasse streaming, puis node7."""
    def cluster(*stuck):
        return {"members": [_member(2, role="leader", state="running", timeline=257)] + [
            _member(n, state="running", timeline=256) if n in stuck else _member(n, timeline=257)
            for n in (1, 3, 4, 6, 7)]}

    def reinitialized():
        return [url.split("/")[2].split(":")[0] for url, _, _ in patroni["posts"]]

    patroni["cluster"] = cluster(4, 6, 7)
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        ticks(0, 10, 11, 30)
        assert reinitialized() == ["10.99.0.4"], "un seul reinit tant que node4 est bloque"
        patroni["cluster"] = cluster(6, 7)
        ticks(31)
        assert reinitialized() == ["10.99.0.4", "10.99.0.6"]
        patroni["cluster"] = cluster(7)
        ticks(32)
    assert reinitialized() == ["10.99.0.4", "10.99.0.6", "10.99.0.7"]
    assert [c.kwargs["subject"] for c in mail.call_args_list] == [
        f"[Alarme Murgat] Replica node{n} re-cloné automatiquement" for n in (4, 6, 7)]


def test_only_lagging_beaufort_replicas_streaming_still_reinit(patroni, ticks):
    """Lags mesures en prod le 2026-11-06 (Beaufort via OVH) : seuls node4/node7 suivent, avec
    64 Mo / 32 Mo de lag stable. Ils prouvent que le leader va bien : node1 re-clone."""
    lags = {4: 67_109_384, 7: 33_554_952}
    patroni["cluster"] = {"members": [_member(2, role="leader", state="running", timeline=257)] + [
        _member(n, timeline=257, lag=lags[n]) if n in lags else _member(n, state="running", timeline=256)
        for n in (1, 3, 4, 6, 7)]}
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        ticks(0, 10)
    assert patroni["posts"] == [("http://10.99.0.1:8008/reinitialize", "POST", {"force": True})]
    assert mail.call_args.kwargs["subject"] == "[Alarme Murgat] Replica node1 re-cloné automatiquement"


def test_reinit_http_failure_is_audited_and_does_not_raise(patroni, ticks):
    patroni["fail_reinit"] = True
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        ticks(0, 10, 11)
    assert len(patroni["posts"]) == 1, "pas de nouvel essai avant 1 h"
    audits = _audits()[before:]
    assert len(audits) == 1 and audits[0]["ok"] is False
    assert mail.call_count == 1


def test_delay_follows_system_config(patroni, ticks, client, admin_headers):
    r = client.post("/api/config/system", json={"key": "replica_reinit_after_minutes", "value": "2"},
                    headers=admin_headers)
    assert r.status_code == 200
    try:
        with patch("backend.app.quorum_monitor.send_alert_email"):
            ticks(0, 1)
            assert patroni["posts"] == []
            ticks(2)
        assert len(patroni["posts"]) == 1
    finally:
        client.post("/api/config/system", json={"key": "replica_reinit_after_minutes", "value": "10"},
                    headers=admin_headers)
