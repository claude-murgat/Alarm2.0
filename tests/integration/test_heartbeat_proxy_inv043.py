"""
INV-043 (révisé 2026-06-16) — Heartbeat sur replica : proxy vers le leader
sur le nœud edge cloud, 503 sinon (onsite/failback).

Contexte : le nœud cloud (node3) est le seul joignable par les téléphones
externes/mobile (les onsite sont sur LAN privé). Quand node3 est un replica
Patroni, renvoyer 503 condamnait le tél d'astreinte en 4G à une "connexion
perdue" perpétuelle (il ne peut pas rotater vers le leader onsite injoignable),
alors que le cluster est sain.

Fix : sur le nœud edge (HEARTBEAT_PROXY_ON_REPLICA=true), un replica forwarde
le heartbeat au leader via WG. Les nœuds onsite gardent le 503 → l'app rotate
sur le LAN (comportement failback préservé, cf test_failback.py).

Tier 2 : TestClient + manipulation directe de `is_leader` (Event) et du flag.
"""
import pytest

from backend.app.leader_election import is_leader
from backend.app.api import devices


@pytest.fixture
def user1_headers(client):
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    assert r.status_code == 200, f"login user1 KO: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestHeartbeatProxyInv043:
    def teardown_method(self):
        # SQLite = toujours primary par défaut : restaurer l'état leader pour
        # ne pas polluer les autres tests de la session.
        is_leader.set()

    def test_leader_heartbeat_returns_200(self, client, user1_headers):
        """Régression : sur le leader, heartbeat normal → 200."""
        is_leader.set()
        r = client.post("/api/devices/heartbeat", headers=user1_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_replica_without_proxy_returns_503(self, client, user1_headers, monkeypatch):
        """Onsite (flag OFF) : replica → 503 pour que l'app rotate (INV-043 failback)."""
        monkeypatch.setattr(devices, "_PROXY_ON_REPLICA", False)
        is_leader.clear()
        r = client.post("/api/devices/heartbeat", headers=user1_headers)
        assert r.status_code == 503
        assert r.json()["detail"] == "replica"

    def test_replica_edge_proxies_to_leader(self, client, user1_headers, monkeypatch):
        """Edge cloud (flag ON) : replica → forward au leader → 200."""
        monkeypatch.setattr(devices, "_PROXY_ON_REPLICA", True)
        called = {}

        def fake_proxy(token):
            called["token"] = token
            return {"status": "ok", "timestamp": "2026-01-01T00:00:00+00:00"}

        monkeypatch.setattr(devices, "_proxy_heartbeat_to_primary", fake_proxy)
        is_leader.clear()
        r = client.post("/api/devices/heartbeat", headers=user1_headers)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert "token" in called, "le heartbeat aurait dû être forwardé au leader"

    def test_replica_edge_already_proxied_returns_503(self, client, user1_headers, monkeypatch):
        """Anti-boucle : un heartbeat DÉJÀ proxifié (header X-Heartbeat-Proxied)
        n'est PAS re-proxifié, même sur un edge avec le flag ON → 503."""
        monkeypatch.setattr(devices, "_PROXY_ON_REPLICA", True)

        def fake_proxy(token):
            raise AssertionError("ne doit PAS re-proxifier un heartbeat déjà proxifié")

        monkeypatch.setattr(devices, "_proxy_heartbeat_to_primary", fake_proxy)
        is_leader.clear()
        r = client.post(
            "/api/devices/heartbeat",
            headers={**user1_headers, "X-Heartbeat-Proxied": "1"},
        )
        assert r.status_code == 503
        assert r.json()["detail"] == "replica"
