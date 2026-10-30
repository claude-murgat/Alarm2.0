"""
Tier 2 integration : INV-175 — GET /api/resilience (admin) : etat reel des 5 noeuds et des
passerelles + scenarios de panne calcules ; GET /api/resilience/whatif pour un scenario libre.

Patroni et les /health des autres noeuds sont simules par `urlopen` (motif de
test_cluster_location_inv161.py) : 10.99.0.4 (node4) ne repond pas.
"""
import io
import json
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

pytestmark = pytest.mark.integration

MEMBERS = [{"name": f"node{i}", "role": "leader" if i == 3 else "replica",
            "state": "running" if i == 3 else "streaming", "host": f"10.99.0.{i}"} for i in (1, 2, 3, 4, 5)]


@pytest.fixture
def fake_cluster(monkeypatch):
    real = urllib.request.urlopen
    seen = []

    def fake(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append(url)
        if url.endswith("/cluster"):
            return io.BytesIO(json.dumps({"members": MEMBERS}).encode())
        if url.endswith(":8000/health"):
            host = url.split("//")[1].split(":")[0]
            if host == "10.99.0.4":
                raise urllib.error.URLError("connection refused")
            return io.BytesIO(json.dumps({"status": "ok", "version": f"v-{host[-1]}"}).encode())
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


@pytest.fixture
def patroni_down(monkeypatch):
    def fake(req, *a, **kw):
        raise urllib.error.URLError("patroni down")
    monkeypatch.setattr(urllib.request, "urlopen", fake)


@pytest.fixture
def user1_headers(client):
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def stale_gateway():
    """onsite-2 vue il y a 125 s via report-state ; alarm-1 jamais vue."""
    from backend.app.clock import now as clock_now
    from backend.app.database import SessionLocal
    from backend.app.models import GatewayState
    db = SessionLocal()
    try:
        db.query(GatewayState).filter(GatewayState.gateway_id.in_(["onsite-2", "alarm-1"])).delete(
            synchronize_session=False)
        db.add(GatewayState(gateway_id="onsite-2", state="closed", site="izeron",
                            last_seen=clock_now() - timedelta(seconds=125)))
        db.commit()
        yield
        db.query(GatewayState).filter(GatewayState.gateway_id == "onsite-2").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_live_nodes_and_gateways(client, admin_headers, fake_cluster, stale_gateway):
    r = client.get("/api/resilience", headers=admin_headers)
    assert r.status_code == 200, r.text
    live = r.json()["live"]
    nodes = {n["name"]: n for n in live["nodes"]}
    assert list(nodes) == ["node1", "node2", "node3", "node4", "node5"]
    assert nodes["node3"] == {"name": "node3", "site": "ovh", "voter": True, "reachable": True,
                              "role": "leader", "state": "running", "version": "v-3"}
    assert nodes["node4"] == {"name": "node4", "site": "beaufort", "voter": False, "reachable": False,
                              "role": "replica", "state": "streaming", "version": None}
    assert nodes["node1"]["voter"] and nodes["node1"]["reachable"] and nodes["node1"]["version"] == "v-1"
    assert live["leader"] == "node3"
    assert live["etcd_voters_up"] == 3
    # Une sonde /health par membre Patroni
    assert sorted(u for u in fake_cluster if u.endswith("/health")) == [
        f"http://10.99.0.{i}:8000/health" for i in (1, 2, 3, 4, 5)]
    gws = {g["gateway_id"]: g for g in live["gateways"]}
    assert set(gws) == {"onsite-2", "alarm-1"}
    assert gws["onsite-2"]["site"] == "izeron" and gws["onsite-2"]["host"] == "node2"
    assert gws["onsite-2"]["registered"] is True and 120 <= gws["onsite-2"]["last_seen_s"] <= 130
    assert gws["alarm-1"] == {"gateway_id": "alarm-1", "host": "node4", "site": "beaufort",
                              "registered": False, "last_seen_s": None}


def test_whatif_default_scenarios(client, admin_headers, fake_cluster):
    whatif = {w["scenario"]: w for w in client.get("/api/resilience", headers=admin_headers).json()["whatif"]}
    assert {"izeron_one_node", "ovh_down", "izeron_both", "beaufort_both", "gateway_izeron"} <= set(whatif)
    both = whatif["izeron_both"]
    assert both["untestable"] is True and both["leader_possible"] is False
    assert both["alerted_by"] == ["email INV-170 (après PR 1)"]
    assert both["label"] == "Izeron entier (node1 + node2)"
    ovh = whatif["ovh_down"]
    assert ovh["untestable"] is False and ovh["leader_possible"] is True
    assert ovh["writable_from"] == {"izeron": True, "ovh": False, "beaufort": False}
    assert ovh["sms_calls"] == {"izeron": True, "beaufort": False}
    assert ovh["app_4g_reachable"] is False
    assert "Leader : oui" in ovh["sentence"]


def test_whatif_adhoc(client, admin_headers):
    r = client.get("/api/resilience/whatif?nodes=node4,node5,nodeX", headers=admin_headers)
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["leader_possible"] is True
    assert o["writable_from"]["beaufort"] is False and o["alarms_created"]["beaufort"] is False
    assert o["alarms_created"]["izeron"] is True
    g = client.get("/api/resilience/whatif?gateways=onsite-2", headers=admin_headers).json()
    assert g["alarms_created"] == {"izeron": False, "beaufort": True} and g["alerted_by"] == ["email INV-144"]


def test_non_admin_forbidden(client, user1_headers, fake_cluster):
    assert client.get("/api/resilience", headers=user1_headers).status_code == 403
    assert client.get("/api/resilience/whatif?nodes=node1", headers=user1_headers).status_code == 403


def test_patroni_unreachable_503(client, admin_headers, patroni_down):
    r = client.get("/api/resilience", headers=admin_headers)
    assert r.status_code == 503
    assert "Patroni" in r.text
