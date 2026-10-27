"""
Tier 2 integration : INV-161 — /api/cluster donne le lieu de chaque noeud (Izeron, Beaufort, OVH).

Cluster de prod a 5 noeuds sur 3 lieux depuis le 2026-10-27 : NODE3 (OVH), onsite-1/2 (Izeron),
alarm-1/2 (Beaufort simule). Patroni est simule : seule la requete /cluster est interceptee.
"""
import io
import json
import urllib.request

import pytest

pytestmark = pytest.mark.integration

MEMBERS = [{"name": f"node{i}", "role": "replica", "state": "streaming", "host": f"10.99.0.{i}"}
           for i in (1, 2, 3, 4, 5)] + [{"name": "node9", "role": "replica", "state": "streaming"}]


@pytest.fixture
def patroni_cluster(monkeypatch):
    real = urllib.request.urlopen

    def fake(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/cluster"):
            return io.BytesIO(json.dumps({"members": MEMBERS}).encode())
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", fake)


def _set_locations(value):
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == "node_locations").first()
        if value is None:
            if row:
                db.delete(row)
        elif row:
            row.value = value
        else:
            db.add(SystemConfig(key="node_locations", value=value))
        db.commit()
    finally:
        db.close()


def test_default_locations(client, patroni_cluster, monkeypatch):
    _set_locations(None)  # base deja en service : pas de cle -> valeurs par defaut
    monkeypatch.setenv("NODE_NAME", "node3")
    data = client.get("/api/cluster").json()
    loc = {m["name"]: m.get("location") for m in data["members"]}
    assert loc == {"node1": "izeron", "node2": "izeron", "node3": "ovh",
                   "node4": "beaufort", "node5": "beaufort", "node9": None}
    assert data["local_location"] == "ovh"


def test_locations_follow_system_config(client, patroni_cluster, monkeypatch):
    _set_locations("node4:izeron,node5:izeron,node3:ovh")
    try:
        monkeypatch.setenv("NODE_NAME", "node4")
        data = client.get("/api/cluster").json()
        loc = {m["name"]: m.get("location") for m in data["members"]}
        assert loc["node4"] == "izeron" and loc["node5"] == "izeron" and loc["node3"] == "ovh"
        assert loc["node1"] is None  # absent de la liste configuree
        assert data["local_location"] == "izeron"
    finally:
        _set_locations(None)
