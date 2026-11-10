"""
Tier 2 integration : INV-100 (amende le 2026-11-10) — l'alarme d'une passerelle
(contact sec) a son audit `alarm_created` puis `alarm_resolved`, ecrits apres le
commit de `report-state`.

Prod 2026-11-10 : 0 des 46 alarmes `gateway_dry_contact` avaient `alarm_created`
(62 sur 62 pour l'API). L'audit (session dediee) partait avant le commit : cle
etrangere vers une alarme invisible en PostgreSQL, verrou d'ecriture SQLite
attendu 5 s, puis perte silencieuse.
"""
import time

import pytest

pytestmark = pytest.mark.integration

GW_KEY = "test-gateway-key-INV-100"
GW = {"X-Gateway-Key": GW_KEY}


@pytest.fixture
def admin_h(client, admin_headers, monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", GW_KEY)  # lu a chaque requete /internal/*
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200
    yield admin_headers
    client.post("/api/test/reset-clock", params={"peer": "false"})
    client.post("/api/test/reset", params={"peer": "false"})


def _report(client, state):
    """POST report-state, renvoie sa duree (SQLite : ~5 s si l'audit attend le verrou)."""
    t0 = time.monotonic()
    r = client.post("/internal/alarms/report-state", headers=GW, json={"gateway_id": "inv100-gw", "state": state})
    assert r.status_code == 200, r.text
    return time.monotonic() - t0


def _audits(client, admin_h, alarm_id, event_type):
    r = client.get("/api/audit/", headers=admin_h, params={"alarm_id": alarm_id, "event_type": event_type})
    assert r.status_code == 200, r.text
    return r.json()


def test_gateway_alarm_creation_is_audited(client, admin_h):
    elapsed = _report(client, "open")
    active = client.get("/api/alarms/active", headers=admin_h).json()
    assert len(active) == 1, active

    audits = _audits(client, admin_h, active[0]["id"], "alarm_created")
    assert len(audits) == 1, f"un audit alarm_created par alarme de passerelle, got {audits} ({elapsed:.1f} s)"
    assert audits[0]["details"] == {"source": "gateway_dry_contact", "assigned_to": active[0]["assigned_user_id"]}
    assert elapsed < 2, f"report-state open a pris {elapsed:.1f} s"


def test_gateway_alarm_resolution_is_audited(client, admin_h):
    _report(client, "open")
    alarm_id = client.get("/api/alarms/active", headers=admin_h).json()[0]["id"]

    elapsed = _report(client, "closed")
    assert client.get("/api/alarms/active", headers=admin_h).json() == []
    audits = _audits(client, admin_h, alarm_id, "alarm_resolved")
    assert len(audits) == 1, f"un audit alarm_resolved par resolution automatique, got {audits} ({elapsed:.1f} s)"
    assert audits[0]["details"] == {"source": "gateway_dry_contact_auto_resolve"}
    assert elapsed < 2, f"report-state closed a pris {elapsed:.1f} s"
