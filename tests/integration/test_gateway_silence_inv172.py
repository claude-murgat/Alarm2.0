"""Tier 2 integration : INV-172 — passerelle muette depuis 10 min → un email + un audit par épisode.

Le tick réel (`_run_gateway_silence_check`) est appelé avec un `now` choisi ; `last_seen` vient d'un vrai
`POST /internal/alarms/report-state`. La boucle de fond `quorum_monitor_loop` tourne aussi dans le
TestClient : son appel passe par le nom du module, neutralisé pour toute la session par
`tests/integration/conftest.py` (fixture `real_gateway_silence_check`, qui fournit le vrai contrôle).
"""
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_ALERT_EMAIL = "direction.technique@charlesmurgat.test"
GATEWAY_KEY = "test-gateway-key-INV-172"
GW = "onsite-2"
KEY = "gateway_silence_emailed"


def _clean():
    from backend.app.database import SessionLocal
    from backend.app.models import GatewayState, SystemConfig
    db = SessionLocal()
    try:
        db.query(GatewayState).delete()
        db.query(SystemConfig).filter(SystemConfig.key == KEY).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def check(monkeypatch, client, admin_headers, real_gateway_silence_check):
    """Le vrai tick INV-172, état propre (le contrôle de fond est neutralisé par conftest)."""
    monkeypatch.setenv("GATEWAY_KEY", GATEWAY_KEY)
    r = client.post("/api/config/system", json={"key": "alert_email", "value": _ALERT_EMAIL},
                    headers=admin_headers)
    assert r.status_code == 200
    _clean()
    yield real_gateway_silence_check
    _clean()


def _report(client):
    """report-state de la passerelle (contact au repos) ; renvoie son last_seen."""
    from backend.app.database import SessionLocal
    from backend.app.models import GatewayState
    r = client.post("/internal/alarms/report-state",
                    json={"gateway_id": GW, "state": "closed", "site": "izeron"},
                    headers={"X-Gateway-Key": GATEWAY_KEY})
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        return db.query(GatewayState).filter(GatewayState.gateway_id == GW).one().last_seen
    finally:
        db.close()


def _audits():
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent
    db = SessionLocal()
    try:
        rows = db.query(AuditEvent).filter(AuditEvent.event_type == "gateway_silent").all()
        return [json.loads(r.details) for r in rows]
    finally:
        db.close()


def _persisted():
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == KEY).first()
        return json.loads(row.value) if row else None
    finally:
        db.close()


def test_silent_gateway_emailed_and_audited_once(client, check):
    t0 = _report(client)
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        check(t0 + timedelta(minutes=9))
        assert mail.call_count == 0, "pas encore muette"
        check(t0 + timedelta(minutes=11))
        assert mail.call_count == 1
        kw = mail.call_args.kwargs
        assert kw["to"] == _ALERT_EMAIL
        assert kw["subject"] == f"[Alarme Murgat] Passerelle {GW} (Izeron) muette depuis 11 min"
        assert t0.isoformat() in kw["body"]
        assert "contact sec" in kw["body"] and "SMS" in kw["body"]
        assert "alarm-sms-gateway" in kw["body"] and "WireGuard" in kw["body"]
        check(t0 + timedelta(minutes=12))
        assert mail.call_count == 1, "un seul email par épisode"
    audits = _audits()[before:]
    assert len(audits) == 1
    assert audits[0]["gateway_id"] == GW and audits[0]["site"] == "izeron"
    assert audits[0]["silent_minutes"] == 11
    assert _persisted() == [GW]


def test_gateway_back_then_silent_again_is_emailed_again(client, check):
    t0 = _report(client)
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        check(t0 + timedelta(minutes=11))
        assert mail.call_count == 1
        t1 = _report(client)  # la passerelle reparle
        check(t1 + timedelta(minutes=1))
        assert _persisted() == [], "réarmée"
        check(t1 + timedelta(minutes=10))
        assert mail.call_count == 2
        assert "muette depuis 10 min" in mail.call_args.kwargs["subject"]


# Premier contrôle d'un processus neuf (déploiement, bascule de leader) : sous-processus sur la même
# base, sans état de module hérité des tests précédents ni du tick de fond. Renvoie les sujets envoyés.
_FRESH_PROCESS_CHECK = """
import json, sys
from datetime import datetime
from unittest.mock import patch
from backend.app import quorum_monitor
with patch.object(quorum_monitor, "send_alert_email") as mail:
    quorum_monitor._run_gateway_silence_check(datetime.fromisoformat(sys.argv[1]))
print(json.dumps([c.kwargs["subject"] for c in mail.call_args_list]))
"""


def _first_check_in_fresh_process(now: datetime) -> list:
    r = subprocess.run([sys.executable, "-c", _FRESH_PROCESS_CHECK, now.isoformat()],
                       cwd=pathlib.Path(__file__).resolve().parents[2], env=os.environ.copy(),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.splitlines()[-1])


def test_gateway_already_silent_at_first_check_is_emailed(client, check):
    """Pas de suppression au démarrage : une passerelle tombée juste avant un déploiement ou une
    bascule est signalée au premier contrôle du nouveau leader."""
    t0 = _report(client)
    before = len(_audits())
    assert _first_check_in_fresh_process(t0 + timedelta(minutes=30)) == [
        f"[Alarme Murgat] Passerelle {GW} (Izeron) muette depuis 30 min"]
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        check(t0 + timedelta(minutes=31))
        assert mail.call_count == 0, "déjà signalée par le leader précédent (clé partagée)"
    assert len(_audits()[before:]) == 1
    assert _persisted() == [GW]


def test_background_tick_runs_check_off_event_loop(client, monkeypatch):
    """La lecture DB et l'email SMTP (timeout 10 s) ne doivent pas geler l'event loop (/health, heartbeats)."""
    import asyncio
    from backend.app import quorum_monitor as qm
    where = []

    def check(*args):
        try:
            asyncio.get_running_loop()
            where.append("event loop")
        except RuntimeError:
            where.append("thread")
    monkeypatch.setattr(qm, "_QUORUM_TICK_SECONDS", 0)
    monkeypatch.setattr(qm, "_build_snapshot_sync",
                        lambda now: qm.ClusterSnapshot(has_quorum=True, patroni_reachable=True, timestamp=now))
    monkeypatch.setattr(qm, "_run_quorum_check", lambda *a, **kw: None)
    monkeypatch.setattr(qm, "_run_replica_reinit_check", lambda now: None)
    monkeypatch.setattr(qm, "_run_gateway_silence_check", check)

    async def one_tick():
        task = asyncio.create_task(qm.quorum_monitor_loop())
        while not where:
            await asyncio.sleep(0.01)
        task.cancel()
    asyncio.run(asyncio.wait_for(one_tick(), timeout=5))
    assert where[0] == "thread"
