"""Tier 2 integration : INV-170 / INV-171 — plus aucun leader Patroni, vu d'un replica.

Patroni simule (seule l'URL /cluster est interceptee) ; `is_leader` efface : ce noeud est un
replica, comme tous les survivants quand Patroni a retire le leader (quorum etcd perdu).
Date de test dans le futur (cf test_replica_reinit_inv168.py) : un tick de fond de
quorum_monitor_loop (horloge reelle) qui passerait pendant le test ne peut rien declencher.
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
_DEFAULT_EMAIL = "direction_technique@charlesmurgat.com"
T0 = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def _members(leader):
    roles = ("leader" if leader else "replica", "replica", "replica")
    return [{"name": f"node{i}", "role": r, "state": "running" if r == "leader" else "streaming",
             "host": f"10.99.0.{i}", "port": 5432, "timeline": 256}
            for i, r in enumerate(roles, 1)]


def _set_alert_email(value):
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
        if value is None:
            if row:
                db.delete(row)
        elif row:
            row.value = value
        else:
            db.add(SystemConfig(key="alert_email", value=value))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def patroni(monkeypatch, client):
    """Faux Patroni sans leader, ce noeud (node1, Izeron) replica. `down` = Patroni injoignable."""
    from backend.app import escalation, quorum_monitor
    from backend.app.clock import now as clock_now
    from backend.app.leader_election import is_leader
    from backend.app.logic.leader_loss import LeaderLossState
    monkeypatch.setattr(quorum_monitor, "_leader_loss", LeaderLossState())
    monkeypatch.setattr(quorum_monitor, "cluster_has_leader", True)
    monkeypatch.setattr(escalation, "last_tick_at", clock_now())  # /health : boucle vue vivante
    monkeypatch.setenv("NODE_NAME", "node1")
    _set_alert_email(_ALERT_EMAIL)
    fake = {"cluster": {"members": _members(leader=False)}, "down": False}
    real = urllib.request.urlopen

    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/cluster"):
            if fake["down"]:
                raise urllib.error.URLError("connection refused")
            return io.BytesIO(json.dumps(fake["cluster"]).encode())
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    was_leader = is_leader.is_set()
    is_leader.clear()
    yield fake
    if was_leader:
        is_leader.set()


def _ticks(*minutes):
    from backend.app.quorum_monitor import _run_leader_loss_check
    for m in minutes:
        _run_leader_loss_check(T0 + timedelta(minutes=m))


def _audits():
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent
    db = SessionLocal()
    try:
        rows = db.query(AuditEvent).filter(AuditEvent.event_type == "cluster_leader_lost").all()
        return [json.loads(r.details) for r in rows]
    finally:
        db.close()


def test_no_leader_for_3_min_emails_once_from_this_node(patroni):
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(0, 1, 2)
        assert mail.call_count == 0, "email avant 3 min sans leader"
        _ticks(4, 5, 60, 24 * 60)
    assert mail.call_count == 1, "un email par episode, depuis ce noeud"
    assert mail.call_args.kwargs["to"] == _ALERT_EMAIL
    assert mail.call_args.kwargs["subject"] == \
        "[Alarme Murgat] Aucun leader dans le cluster — vu de node1 (izeron)"
    body = mail.call_args.kwargs["body"]
    assert "node1" in body and "écriture" in body and "passerelles" in body
    audits = _audits()[before:]
    assert len(audits) == 1 and audits[0]["node"] == "node1"


def test_health_stays_200_and_says_read_only_until_a_leader_is_back(patroni, client):
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(0)
        h = client.get("/health")
        assert h.status_code == 200, "un 503 ferait reculer le canary et peer-health a tort"
        assert h.json()["cluster_writable"] is False
        assert client.get("/api/system/peer-health").json()["etcd_quorum_seen"] is False

        patroni["cluster"] = {"members": _members(leader=True)}
        _ticks(1)
        assert client.get("/health").json()["cluster_writable"] is True
        assert client.get("/api/system/peer-health").json()["etcd_quorum_seen"] is True
        _ticks(2, 10, 60)  # leader revenu : l'episode est oublie
    assert mail.call_count == 0


def test_patroni_unreachable_counts_as_no_leader(patroni, client):
    patroni["down"] = True
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(0, 4)
    assert mail.call_count == 1
    assert client.get("/health").json()["cluster_writable"] is False


def test_default_recipient_without_alert_email(patroni):
    _set_alert_email(None)
    try:
        with patch("backend.app.quorum_monitor.send_alert_email") as mail:
            _ticks(0, 4)
        assert mail.call_count == 1
        assert mail.call_args.kwargs["to"] == _DEFAULT_EMAIL
    finally:
        _set_alert_email(_ALERT_EMAIL)
