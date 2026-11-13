"""Tier 2 integration : INV-196 — base etcd d'un voteur a 1 Gio ou plus, ou alarme NOSPACE
→ un email + un audit par episode et par niveau ; reamre quand tous les voteurs sont sains.

Constat prod du 2026-11-13 : ~1716 Mo par voteur, tout utilise (aucune compaction configuree).
etcd simule : seul POST http://<voteur>/v3/maintenance/status est intercepte, reponse au format reel
d'etcd 3.5.17 (dbSize en chaine, `errors` = "memberID:... alarm:NOSPACE "). Le vrai controle
(`_run_etcd_space_check`) est appele par le test ; le tick de fond est neutralise par
`tests/integration/conftest.py` (fixture `real_etcd_space_check`).
"""
import io
import json
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_ALERT_EMAIL = "direction.technique@charlesmurgat.test"
KEY = "etcd_space_alert"
HOSTS = ["10.99.0.2:2379", "10.99.0.3:2379", "10.99.0.1:2379"]  # node1, node2, node3
MB = 1048576
NOSPACE = "memberID:10276657743932975437 alarm:NOSPACE "


def _st(size_mb, in_use_mb=None, nospace=False):
    st = {"header": {"revision": "4690000"}, "version": "3.5.17", "dbSize": str(size_mb * MB),
          "dbSizeInUse": str((size_mb if in_use_mb is None else in_use_mb) * MB)}
    if nospace:
        st["errors"] = [NOSPACE]
    return st


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
        return row.value if row else None
    finally:
        db.close()


def _audits():
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent
    db = SessionLocal()
    try:
        rows = db.query(AuditEvent).filter(AuditEvent.event_type == "etcd_space_alert").all()
        return [json.loads(r.details) for r in rows]
    finally:
        db.close()


@pytest.fixture
def etcd(monkeypatch, client, admin_headers, real_etcd_space_check):
    """Faux voteurs etcd (`status[voteur]` modifiable, None = injoignable) ; `tick()` = le vrai contrôle."""
    r = client.post("/api/config/system", json={"key": "alert_email", "value": _ALERT_EMAIL},
                    headers=admin_headers)
    assert r.status_code == 200
    _delete(KEY)
    monkeypatch.setenv("ETCD_HOSTS", ",".join(HOSTS))
    fake = {"status": {h: _st(70) for h in HOSTS}, "requests": [], "tick": real_etcd_space_check}
    real = urllib.request.urlopen

    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/v3/maintenance/status"):
            host = url.split("/")[2]
            fake["requests"].append((req.get_method(), url, req.data))
            if fake["status"][host] is None:
                raise urllib.error.URLError("timed out")
            return io.BytesIO(json.dumps(fake["status"][host]).encode())
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    yield fake
    _delete(KEY)


def test_db_over_1_gib_is_emailed_and_audited_once(etcd):
    """Le constat du 2026-11-13 : les 3 voteurs à 1716 Mo, tout utilisé."""
    etcd["status"] = {h: _st(1716) for h in HOSTS}
    before = len(_audits())
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        etcd["tick"]()
        assert mail.call_count == 1
        kw = mail.call_args.kwargs
        assert kw["to"] == _ALERT_EMAIL
        assert kw["subject"] == "[Alarme Murgat] etcd : base à 1716 Mo sur 2048"
        for h in HOSTS:
            assert f"  - {h} : 1716 Mo (1716 Mo utilisés)" in kw["body"]
        assert "etcd refuse toute écriture" in kw["body"]
        assert "la compaction automatique ne tourne pas" in kw["body"]
        assert "docs/CD_DESIGN.md §9sexies" in kw["body"]
        etcd["tick"]()
        assert mail.call_count == 1, "un seul email par épisode"
    assert etcd["requests"][:3] == [("POST", f"http://{h}/v3/maintenance/status", b"{}") for h in HOSTS]
    assert _audits()[before:] == [{"level": "size", "db_size": {h: 1716 * MB for h in HOSTS}}]
    assert _row(KEY) == "size"


def test_nospace_sends_a_second_email_and_defrag_rearms(etcd):
    etcd["status"] = {h: _st(1716) for h in HOSTS}
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        etcd["tick"]()
        etcd["status"][HOSTS[0]] = _st(2048, nospace=True)
        etcd["status"][HOSTS[2]] = None  # NODE3 injoignable : ne cache pas l'alarme des autres
        etcd["tick"]()
        assert mail.call_count == 2
        kw = mail.call_args.kwargs
        assert kw["subject"] == "[Alarme Murgat] etcd plein (NOSPACE) : écritures refusées"
        assert f"  - {HOSTS[0]} : 2048 Mo (2048 Mo utilisés), alarme NOSPACE" in kw["body"]
        assert f"  - {HOSTS[2]} : injoignable" in kw["body"]
        assert "etcdctl alarm disarm" in kw["body"]
        etcd["tick"]()
        assert mail.call_count == 2
        assert _row(KEY) == "nospace"
        etcd["status"] = {h: _st(3) for h in HOSTS}  # compacté, défragmenté, alarme levée
        etcd["tick"]()
        assert mail.call_count == 2 and _row(KEY) == "", "réarmé, sans email"
        etcd["status"][HOSTS[1]] = _st(1100, in_use_mb=4)
        etcd["tick"]()
        assert mail.call_count == 3, "nouvel épisode, nouvel email"
        assert "1100 Mo (4 Mo utilisés)" in mail.call_args.kwargs["body"]
    assert _audits()[-2]["level"] == "nospace"
    assert _audits()[-2]["db_size"][HOSTS[2]] is None


def test_without_etcd_hosts_nothing_is_read(etcd, monkeypatch):
    monkeypatch.delenv("ETCD_HOSTS")
    with patch("backend.app.quorum_monitor.send_alert_email") as mail:
        etcd["tick"]()
    assert etcd["requests"] == [] and mail.call_count == 0 and _row(KEY) is None


def test_background_tick_runs_check_off_event_loop(client, monkeypatch):
    import asyncio
    from backend.app import quorum_monitor as qm
    calls = []

    def check():
        try:
            asyncio.get_running_loop()
            calls.append("event loop")
        except RuntimeError:
            calls.append("thread")
    monkeypatch.setattr(qm, "_QUORUM_TICK_SECONDS", 0)
    monkeypatch.setattr(qm, "_build_snapshot_sync",
                        lambda now: qm.ClusterSnapshot(has_quorum=True, patroni_reachable=True, timestamp=now))
    monkeypatch.setattr(qm, "_run_quorum_check", lambda *a, **kw: None)
    monkeypatch.setattr(qm, "_run_replica_reinit_check", lambda now: None)
    monkeypatch.setattr(qm, "_run_etcd_space_check", check)

    async def one_tick():
        task = asyncio.create_task(qm.quorum_monitor_loop())
        while not calls:
            await asyncio.sleep(0.01)
        task.cancel()
    asyncio.run(asyncio.wait_for(one_tick(), timeout=5))
    assert calls[0] == "thread"
