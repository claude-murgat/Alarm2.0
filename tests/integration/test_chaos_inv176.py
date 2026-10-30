"""
Tier 2 integration : INV-176/177/179 — panne controlee : POST/GET/DELETE /api/chaos (admin),
GET /internal/chaos/next et POST /internal/chaos/{id}/events (cle passerelle, leader seul).

Patroni est simule par `urlopen` (motif de test_cluster_location_inv161.py) : 5 membres sains,
leader node3. La base SQLite est partagee avec les autres tests : les alarmes actives laissees
par eux sont mises de cote (resolved) le temps du test, puis restaurees.
"""
import io
import json
import os
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.integration

GATEWAY_KEY = os.environ.setdefault("GATEWAY_KEY", "test-gateway-key-cd")
GW = {"X-Gateway-Key": GATEWAY_KEY}
MEMBERS = [{"name": f"node{i}", "role": "leader" if i == 3 else "replica",
            "state": "running" if i == 3 else "streaming", "host": f"10.99.0.{i}"} for i in (1, 2, 3, 4, 5)]
UNFINISHED = ("pending", "claimed", "running")
ALARM_BUSY = ("active", "escalated", "acknowledged")


@pytest.fixture
def patroni(monkeypatch):
    real = urllib.request.urlopen

    def fake(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/cluster"):
            return io.BytesIO(json.dumps({"members": MEMBERS}).encode())
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", fake)


@pytest.fixture
def clean(client, patroni):
    """Aucune demande chaos, aucune alarme en cours, noeud leader."""
    from backend.app.database import SessionLocal
    from backend.app.leader_election import is_leader
    from backend.app.models import Alarm, ChaosRequest
    is_leader.set()
    db = SessionLocal()
    db.query(ChaosRequest).delete()
    parked = {a.id: a.status for a in db.query(Alarm).filter(Alarm.status.in_(ALARM_BUSY))}
    db.query(Alarm).filter(Alarm.id.in_(parked)).update({"status": "resolved"}, synchronize_session=False)
    db.commit()
    yield db
    db.query(ChaosRequest).delete()
    for alarm_id, status in parked.items():
        db.query(Alarm).filter(Alarm.id == alarm_id).update({"status": status}, synchronize_session=False)
    db.commit()
    db.close()


@pytest.fixture
def user1_headers(client):
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@contextmanager
def active_alarm():
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm
    db = SessionLocal()
    a = Alarm(title="chaos", message="alarme en cours", status="active", original_created_at=datetime.utcnow())
    db.add(a)
    db.commit()
    try:
        yield a.id
    finally:
        db.delete(a)
        db.commit()
        db.close()


def _audits(kind):
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent
    db = SessionLocal()
    try:
        return [(r.user_id, json.loads(r.details or "{}"))
                for r in db.query(AuditEvent).filter(AuditEvent.event_type == kind).order_by(AuditEvent.id)]
    finally:
        db.close()


def _row(client, admin_headers, chaos_id):
    rows = client.get("/api/chaos", headers=admin_headers).json()
    return next(r for r in rows if r["id"] == chaos_id)


def _request(client, admin_headers, nodes="node4", mode="stop", minutes=2):
    r = client.post("/api/chaos", json={"nodes": nodes, "mode": mode, "minutes": minutes}, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()


def test_non_admin_forbidden(clean, client, user1_headers):
    assert client.post("/api/chaos", json={"nodes": "node4", "mode": "stop", "minutes": 2}, headers=user1_headers).status_code == 403
    assert client.get("/api/chaos", headers=user1_headers).status_code == 403
    assert client.delete("/api/chaos/1", headers=user1_headers).status_code == 403


def test_whole_izeron_refused_quorum(clean, client, admin_headers):
    r = client.post("/api/chaos", json={"nodes": "site:izeron", "mode": "stop", "minutes": 2}, headers=admin_headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "node1, node2 : quorum etcd perdu — refusé"
    assert client.get("/api/chaos", headers=admin_headers).json() == []


def test_active_alarm_refused(clean, client, admin_headers):
    with active_alarm():
        r = client.post("/api/chaos", json={"nodes": "node4", "mode": "stop", "minutes": 2}, headers=admin_headers)
    assert r.status_code == 409, r.text
    assert "alarme" in r.json()["detail"]


def test_request_then_claim_once(clean, client, admin_headers):
    before = len(_audits("chaos_requested"))
    created = _request(client, admin_headers, nodes="site:beaufort", minutes=3)
    assert created["status"] == "pending" and created["nodes"] == ["node4", "node5"] and created["minutes"] == 3
    requested = _audits("chaos_requested")[before:]
    assert len(requested) == 1 and requested[0][0] is not None  # admin
    assert requested[0][1]["chaos_id"] == created["id"] and requested[0][1]["nodes"] == "node4,node5"

    # un seul test a la fois
    r = client.post("/api/chaos", json={"nodes": "node3", "mode": "stop", "minutes": 1}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["detail"] == "un test est déjà en cours"

    assert client.get("/internal/chaos/next").status_code == 401
    r = client.get("/internal/chaos/next", headers=GW)
    assert r.status_code == 200, r.text
    claimed = r.json()
    assert claimed["id"] == created["id"] and claimed["status"] == "claimed" and claimed["claimed_at"]
    assert claimed["nodes"] == ["node4", "node5"] and claimed["mode"] == "stop"
    assert client.get("/internal/chaos/next", headers=GW).json() is None
    assert _row(client, admin_headers, created["id"])["status"] == "claimed"


def test_internal_next_on_replica_503(clean, client, admin_headers):
    from backend.app.leader_election import is_leader
    _request(client, admin_headers)
    is_leader.clear()
    try:
        r = client.get("/internal/chaos/next", headers=GW)
        assert r.status_code == 503 and r.json()["detail"] == "replica"
    finally:
        is_leader.set()
    assert _row(client, admin_headers, client.get("/api/chaos", headers=admin_headers).json()[0]["id"])["status"] == "pending"


def test_events_drive_status_timestamps_audit_and_alert_window(clean, client, admin_headers):
    from backend.app.events import log_event
    created = _request(client, admin_headers)
    cid = created["id"]
    client.get("/internal/chaos/next", headers=GW)
    ev = f"/internal/chaos/{cid}/events"
    assert client.post(ev, json={"kind": "started", "details": {"leader_before": "node3"}}).status_code == 401

    r = client.post(ev, json={"kind": "started", "details": {"leader_before": "node3"}}, headers=GW)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running" and r.json()["started_at"] and r.json()["restored_at"] is None

    log_event("alarm_created", alarm_id=None, title="pendant la panne")  # ce que le systeme fait pendant le test
    before_leader = len(_audits("chaos_leader_changed"))
    r = client.post(ev, json={"kind": "leader_changed", "details": {"from": "node3", "to": "node1"}}, headers=GW)
    assert r.json()["status"] == "running"
    assert len(_audits("chaos_leader_changed")) == before_leader  # pas audite

    r = client.post(ev, json={"kind": "restored", "details": {"leader_after": "node1", "restore_seconds": 42}}, headers=GW)
    assert r.json()["status"] == "restored" and r.json()["restored_at"]

    row = _row(client, admin_headers, cid)
    assert [e["kind"] for e in row["events"]] == ["started", "leader_changed", "restored"]
    assert row["events"][2]["details"] == {"leader_after": "node1", "restore_seconds": 42}
    assert all(e["ts"] for e in row["events"])
    types = [a["event_type"] for a in row["alerts"]]
    assert "alarm_created" in types and not any(t.startswith("chaos_") for t in types)
    assert all(a["timestamp"] for a in row["alerts"])
    assert any(d["chaos_id"] == cid for _, d in _audits("chaos_started"))
    assert any(d["chaos_id"] == cid and d["restore_seconds"] == 42 for _, d in _audits("chaos_restored"))

    # mauvais kind -> 422, id inconnu -> 404
    assert client.post(ev, json={"kind": "exploded", "details": {}}, headers=GW).status_code == 422
    assert client.post("/internal/chaos/999999/events", json={"kind": "started", "details": {}}, headers=GW).status_code == 404


def test_cancel_pending_ok_running_409(clean, client, admin_headers):
    created = _request(client, admin_headers)
    r = client.delete(f"/api/chaos/{created['id']}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert _row(client, admin_headers, created["id"])["status"] == "cancelled"
    assert any(d["chaos_id"] == created["id"] for _, d in _audits("chaos_cancelled"))
    assert client.delete(f"/api/chaos/{created['id']}", headers=admin_headers).status_code == 409

    second = _request(client, admin_headers)  # possible : le precedent est annule
    client.get("/internal/chaos/next", headers=GW)
    client.post(f"/internal/chaos/{second['id']}/events", json={"kind": "started", "details": {}}, headers=GW)
    assert client.delete(f"/api/chaos/{second['id']}", headers=admin_headers).status_code == 409
    assert _row(client, admin_headers, second["id"])["status"] == "running"
    assert client.delete("/api/chaos/999999", headers=admin_headers).status_code == 404


def test_claim_refused_when_alarm_appeared_meanwhile(clean, client, admin_headers):
    created = _request(client, admin_headers)
    with active_alarm():
        assert client.get("/internal/chaos/next", headers=GW).json() is None
    row = _row(client, admin_headers, created["id"])
    assert row["status"] == "failed" and row["claimed_at"] is None
    assert row["events"][0]["kind"] == "failed" and "alarme" in row["events"][0]["details"]["reason"]
    assert any(d["chaos_id"] == created["id"] and "alarme" in d["reason"] for _, d in _audits("chaos_failed"))
    # la file est libre : une nouvelle demande passe
    _request(client, admin_headers)


def test_unfinished_request_expires_after_30_minutes(clean, client, admin_headers):
    from backend.app.models import ChaosRequest
    created = _request(client, admin_headers)
    client.get("/internal/chaos/next", headers=GW)
    clean.query(ChaosRequest).filter(ChaosRequest.id == created["id"]).update(
        {"created_at": datetime.utcnow() - timedelta(minutes=31)})
    clean.commit()
    row = _row(client, admin_headers, created["id"])
    assert row["status"] == "failed"
    assert row["events"][-1] ["kind"] == "failed" and row["events"][-1]["details"]["reason"] == "timeout"
    assert any(d["chaos_id"] == created["id"] and d["reason"] == "timeout" for _, d in _audits("chaos_failed"))
    assert client.get("/internal/chaos/next", headers=GW).json() is None  # plus rien a reclamer


def test_history_keeps_last_10(clean, client, admin_headers):
    for _ in range(12):
        created = _request(client, admin_headers, minutes=1)
        client.delete(f"/api/chaos/{created['id']}", headers=admin_headers)
    rows = client.get("/api/chaos", headers=admin_headers).json()
    assert len(rows) == 10 and rows[0]["id"] == created["id"]  # plus recent en premier
