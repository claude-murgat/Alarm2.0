"""
INV-043 (révisé 2026-06-17) — Heartbeat sur replica : proxy vers le leader
(défaut, tous nœuds) ; 503 seulement si kill-switch OFF ou aucun leader.

Contexte : un téléphone externe/mobile qui tombe sur un replica ne peut pas
rotater vers les autres nœuds (LAN privé, ou cloud tombé). S'il recevait 503 →
"connexion perdue" perpétuelle alors que le cluster est sain. Donc tout replica
forwarde le heartbeat au leader via WG (`_proxy_heartbeat_to_primary`) et
renvoie 200. Défaut `HEARTBEAT_PROXY_ON_REPLICA=true` sur tous les nœuds ;
`false` = kill-switch restaurant l'ancien 503 (chemin testé ci-dessous).

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

    def test_replica_killswitch_off_returns_503(self, client, user1_headers, monkeypatch):
        """Kill-switch (flag OFF) : replica → 503 (ancien comportement, l'app rotate)."""
        monkeypatch.setattr(devices, "_PROXY_ON_REPLICA", False)
        is_leader.clear()
        r = client.post("/api/devices/heartbeat", headers=user1_headers)
        assert r.status_code == 503
        assert r.json()["detail"] == "replica"

    def test_replica_default_proxies_to_leader(self, client, user1_headers, monkeypatch):
        """Défaut (flag ON, tous nœuds) : replica → forward au leader → 200."""
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
