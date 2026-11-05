"""
Tier 2 integration : INV-161 — /api/cluster donne le lieu (Izeron, Beaufort, OVH), la machine et
le badge « bientôt obsolète » de chaque noeud.

Cluster de prod a 5 noeuds sur 3 lieux depuis le 2026-10-27 : NODE3 (OVH), onsite-1/2 (Izeron),
alarm-1/2 (Beaufort simule) ; 7 noeuds depuis le 2026-11-05 : alarm-3/4 (node6/node7, futurs noeuds
d'Izeron, non votants). Patroni est simule : seule la requete /cluster est interceptee.
"""
import io
import json
import urllib.request

import pytest

pytestmark = pytest.mark.integration

MEMBERS = [{"name": f"node{i}", "role": "replica", "state": "streaming", "host": f"10.99.0.{i}"}
           for i in range(1, 8)] + [{"name": "node9", "role": "replica", "state": "streaming"}]


@pytest.fixture
def patroni_cluster(monkeypatch):
    real = urllib.request.urlopen

    def fake(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/cluster"):
            return io.BytesIO(json.dumps({"members": MEMBERS}).encode())
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", fake)


def _set(key, value):
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig
    db = SessionLocal()
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if value is None:
            if row:
                db.delete(row)
        elif row:
            row.value = value
        else:
            db.add(SystemConfig(key=key, value=value))
        db.commit()
    finally:
        db.close()


def _set_locations(value):
    _set("node_locations", value)


@pytest.fixture
def no_stored_keys():
    """Base deja en service : aucune des trois cles -> valeurs par defaut."""
    for key in ("node_locations", "node_machines", "retiring_nodes"):
        _set(key, None)


def _by_name(data, field):
    return {m["name"]: m.get(field) for m in data["members"]}


def test_default_locations(client, patroni_cluster, no_stored_keys, monkeypatch):
    monkeypatch.setenv("NODE_NAME", "node3")
    data = client.get("/api/cluster").json()
    assert _by_name(data, "location") == {"node1": "izeron", "node2": "izeron", "node3": "ovh",
                                          "node4": "beaufort", "node5": "beaufort",
                                          "node6": "izeron", "node7": "izeron", "node9": None}
    assert data["local_location"] == "ovh"


def test_default_machines_and_retiring(client, patroni_cluster, no_stored_keys, monkeypatch):
    """2026-11-05 : machine de chaque noeud ; onsite-1/onsite-2 bientot obsoletes. node9 inconnu :
    son nom Patroni, jamais null (contrat de l'app Android)."""
    monkeypatch.setenv("NODE_NAME", "node6")
    data = client.get("/api/cluster").json()
    assert _by_name(data, "machine") == {"node1": "onsite-1", "node2": "onsite-2", "node3": "OVH",
                                         "node4": "alarm-1", "node5": "alarm-2", "node6": "alarm-3",
                                         "node7": "alarm-4", "node9": "node9"}
    assert _by_name(data, "retiring") == {"node1": True, "node2": True, "node3": False, "node4": False,
                                          "node5": False, "node6": False, "node7": False, "node9": False}
    assert (data["local_node"], data["local_machine"], data["local_location"]) == ("node6", "alarm-3", "izeron")


def test_unknown_local_node_machine_is_its_name(client, patroni_cluster, no_stored_keys, monkeypatch):
    monkeypatch.setenv("NODE_NAME", "node9")
    data = client.get("/api/cluster").json()
    assert data["local_machine"] == "node9" and data["local_location"] is None


def test_locations_follow_system_config(client, patroni_cluster, monkeypatch):
    _set_locations("node4:izeron,node5:izeron,node3:ovh")
    try:
        monkeypatch.setenv("NODE_NAME", "node4")
        data = client.get("/api/cluster").json()
        loc = _by_name(data, "location")
        assert loc["node4"] == "izeron" and loc["node5"] == "izeron" and loc["node3"] == "ovh"
        assert loc["node1"] is None and loc["node6"] is None  # absents de la liste configuree
        assert data["local_location"] == "izeron"
    finally:
        _set_locations(None)


def test_machines_and_retiring_follow_system_config(client, patroni_cluster, monkeypatch):
    """Une cle enregistree remplace son defaut entierement (pas de fusion)."""
    _set("node_machines", "node6:alarm-3-izeron,node1:vieux-pc")
    _set("retiring_nodes", "node4")
    try:
        monkeypatch.setenv("NODE_NAME", "node1")
        data = client.get("/api/cluster").json()
        machine, retiring = _by_name(data, "machine"), _by_name(data, "retiring")
        assert machine["node6"] == "alarm-3-izeron" and machine["node1"] == "vieux-pc"
        assert machine["node4"] == "node4"  # absent de la cle enregistree -> nom Patroni
        assert retiring["node4"] is True and retiring["node1"] is False and retiring["node2"] is False
        group = _by_name(data, "group")  # le groupe suit retiring_nodes ; retiring -> toujours « Legacy (Izeron) »
        assert (group["node4"], group["node1"]) == ("Legacy (Izeron)", "Izeron")
        assert data["local_machine"] == "vieux-pc"
    finally:
        _set("node_machines", None)
        _set("retiring_nodes", None)


def test_default_groups(client, patroni_cluster, no_stored_keys, monkeypatch):
    """2026-11-05 : groupe d'affichage, libelles EXACTS du contrat avec l'app Android (PR #297) :
    « Izeron », « Beaufort », « OVH », « Legacy (Izeron) » ; lieu inconnu -> « Lieu inconnu »."""
    monkeypatch.setenv("NODE_NAME", "node3")
    data = client.get("/api/cluster").json()
    assert _by_name(data, "group") == {"node1": "Legacy (Izeron)", "node2": "Legacy (Izeron)", "node3": "OVH",
                                       "node4": "Beaufort", "node5": "Beaufort", "node6": "Izeron",
                                       "node7": "Izeron", "node9": "Lieu inconnu"}
