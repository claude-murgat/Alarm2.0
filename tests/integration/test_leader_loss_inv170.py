"""Tier 2 integration : INV-170 / INV-171 — plus aucun leader vivant, vu d'un replica.

Patroni simule (/cluster) et backend simule du leader liste (node2, http://10.99.0.2:8000/health) ;
`is_leader` efface : ce noeud (node1) est un replica. Le cas par defaut est celui reproduit le
2026-10-30 : quorum etcd perdu, /cluster liste encore node2 `leader` mais son backend dit `secondary`.
Le controle de la boucle de fond est neutralise pendant chaque test : seuls les ticks du test
(`patroni["tick"]`, date en 2030) touchent l'etat, aucune course avec un tick reel.
"""
import asyncio
import io
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_ALERT_EMAIL = "direction.technique@charlesmurgat.test"
_DEFAULT_EMAIL = "direction_technique@charlesmurgat.com"
_LEADER_HEALTH = "http://10.99.0.2:8000/health"
T0 = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


def _members(leader):
    roles = ("replica", "leader" if leader else "replica", "replica")
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
    """`down` : Patroni local injoignable ; `leader_role` : role annonce par le backend du leader
    liste (None = injoignable). `tick` : le vrai controle, que la boucle de fond n'appelle plus."""
    from backend.app import escalation, quorum_monitor
    from backend.app.clock import now as clock_now
    from backend.app.leader_election import is_leader
    from backend.app.logic.leader_loss import LeaderLossState
    fake = {"cluster": {"members": _members(leader=True)}, "leader_role": "secondary", "down": False,
            "tick": quorum_monitor._run_leader_loss_check}
    monkeypatch.setattr(quorum_monitor, "_run_leader_loss_check", lambda now: None)
    monkeypatch.setattr(quorum_monitor, "_leader_loss", LeaderLossState())
    monkeypatch.setattr(quorum_monitor, "cluster_has_leader", True)
    monkeypatch.setattr(escalation, "last_tick_at", clock_now())  # /health : boucle vue vivante
    monkeypatch.setenv("NODE_NAME", "node1")
    _set_alert_email(_ALERT_EMAIL)
    real = urllib.request.urlopen

    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/cluster"):
            if fake["down"]:
                raise urllib.error.URLError("connection refused")
            return io.BytesIO(json.dumps(fake["cluster"]).encode())
        if url == _LEADER_HEALTH:
            if fake["leader_role"] is None:
                raise urllib.error.URLError("timed out")
            return io.BytesIO(json.dumps({"status": "ok", "role": fake["leader_role"]}).encode())
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    was_leader = is_leader.is_set()
    is_leader.clear()
    yield fake
    is_leader.set() if was_leader else is_leader.clear()


def _ticks(patroni, *minutes):
    for m in minutes:
        patroni["tick"](T0 + timedelta(minutes=m))


def _audits():
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent
    db = SessionLocal()
    try:
        rows = db.query(AuditEvent).filter(AuditEvent.event_type == "cluster_leader_lost").all()
        return [json.loads(r.details) for r in rows]
    finally:
        db.close()


def test_stale_leader_in_patroni_emails_once_from_this_node(patroni):
    """Cas reproduit : /cluster liste node2 leader, son backend n'est plus primary."""
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(patroni, 0, 1, 2)
        assert mail.call_count == 0, "email avant 3 min sans leader"
        _ticks(patroni, 4, 5, 60, 24 * 60)
    assert mail.call_count == 1, "un email par episode, depuis ce noeud"
    assert mail.call_args.kwargs["to"] == _ALERT_EMAIL
    assert mail.call_args.kwargs["subject"] == \
        "[Alarme Murgat] Aucun leader vu de node1 (izeron) — leader listé mais hors service"
    body = mail.call_args.kwargs["body"]
    assert "Vu de node1" in body and "10.99.0.2" in body and "écriture" in body and "passerelles" in body
    audits = _audits()[before:]
    assert len(audits) == 1 and audits[0]["node"] == "node1" and audits[0]["cause"] == "leader_hors_service"


@pytest.mark.parametrize("setup, cause, label", [
    ({"down": True}, "patroni_injoignable", "Patroni injoignable depuis ce nœud"),
    ({"cluster": {"members": _members(leader=False)}}, "aucun_leader", "Patroni ne liste aucun leader"),
    ({"leader_role": None}, "leader_hors_service", "leader listé mais hors service"),
])
def test_each_cause_is_named(patroni, client, setup, cause, label):
    patroni.update(setup)
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(patroni, 0, 4)
    assert mail.call_count == 1
    assert mail.call_args.kwargs["subject"] == f"[Alarme Murgat] Aucun leader vu de node1 (izeron) — {label}"
    assert [a["cause"] for a in _audits()[before:]] == [cause]
    assert client.get("/health").json()["cluster_writable"] is False


def test_health_stays_200_and_says_read_only_until_a_live_leader_is_back(patroni, client):
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(patroni, 0)
        h = client.get("/health")
        assert h.status_code == 200, "un 503 ferait reculer le canary et peer-health a tort"
        assert h.json()["cluster_writable"] is False
        assert client.get("/api/cluster").json()["cluster_writable"] is False

        patroni["leader_role"] = "primary"
        _ticks(patroni, 1)
        assert client.get("/health").json()["cluster_writable"] is True
        assert client.get("/api/cluster").json()["cluster_writable"] is True
        _ticks(patroni, 2, 10, 60)  # leader vivant : l'episode est oublie
    assert mail.call_count == 0


def test_primary_with_silent_patroni_never_emails(patroni, client):
    """Ce noeud est primary : il y a un leader, meme si son Patroni local ne repond plus."""
    from backend.app.leader_election import is_leader
    patroni["down"] = True
    is_leader.set()
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        _ticks(patroni, 0, 4, 60)
    assert mail.call_count == 0
    assert client.get("/health").json()["cluster_writable"] is True


def test_default_recipient_without_alert_email(patroni):
    _set_alert_email(None)
    try:
        with patch("backend.app.quorum_monitor.send_alert_email") as mail:
            _ticks(patroni, 0, 4)
        assert mail.call_count == 1
        assert mail.call_args.kwargs["to"] == _DEFAULT_EMAIL
    finally:
        _set_alert_email(_ALERT_EMAIL)


def test_loop_runs_the_check_on_a_replica(patroni, monkeypatch):
    """Cablage : le controle tourne avant le gate leader de quorum_monitor_loop, donc sur un replica."""
    from backend.app import quorum_monitor
    called = threading.Event()
    monkeypatch.setattr(quorum_monitor, "_QUORUM_TICK_SECONDS", 0)
    monkeypatch.setattr(quorum_monitor, "_run_leader_loss_check", lambda now: called.set())

    async def first_tick_ran():
        task = asyncio.create_task(quorum_monitor.quorum_monitor_loop())
        try:
            for _ in range(200):
                if called.is_set():
                    return True
                await asyncio.sleep(0.01)
            return False
        finally:
            task.cancel()
    assert asyncio.run(first_tick_ran()), "le controle INV-170 n'a pas tourne sur un replica"
