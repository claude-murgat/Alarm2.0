"""
INV-044 — Écriture `/api/*` reçue par un replica → proxy au leader.

Généralise INV-043 (heartbeat) à TOUTES les écritures applicatives : une requête
mutante (POST/PUT/PATCH/DELETE) sur `/api/*` arrivant sur un replica est forwardée
au leader (peers WireGuard) qui exécute, et la réponse est relayée telle quelle.

Motivation : l'UI web est servie par un nœud FIXE (souvent le cloud OVH, seul
joignable publiquement) et ne peut pas rotater les URLs comme l'app mobile
(INV-ANDROID-304). Si ce nœud est replica, `POST /api/auth/login` (qui écrit :
refresh token + log_event) échoue en read-only → connexion impossible. Le proxy
backend rend l'UI web utilisable quel que soit le primary, ce qui permet de
garder le leader Patroni sur un onsite.

Exclusions vérifiées : `/api/devices/heartbeat` (relai dédié INV-043) et `/api/test/*`.
Les endpoints node-to-node (`/internal/*`) sont hors `/api/` → jamais relayés.

Tier 2 : TestClient + manipulation directe de `is_leader` (Event) et des globals
du middleware (`replica_proxy`).
"""
import pytest

from backend.app.leader_election import is_leader
from backend.app import replica_proxy
from backend.app.api import devices

pytestmark = pytest.mark.integration


@pytest.fixture
def user1_headers(client):
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    assert r.status_code == 200, f"login user1 KO: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestWriteProxyInv044:
    def teardown_method(self):
        # SQLite = toujours primary par défaut : restaurer l'état leader pour
        # ne pas polluer les autres tests de la session.
        is_leader.set()

    def test_leader_write_executes_locally(self, client, monkeypatch):
        """Régression : sur le leader, une écriture /api/* s'exécute localement
        (le proxy n'est JAMAIS appelé)."""
        async def boom(request):
            raise AssertionError("le leader ne doit pas proxifier ses propres écritures")

        monkeypatch.setattr(replica_proxy, "_proxy_write_to_primary", boom)
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", True)
        is_leader.set()
        r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_replica_default_proxies_to_leader(self, client, monkeypatch):
        """Défaut (flag ON) : une écriture /api/* sur un replica est forwardée au
        leader, et sa réponse est relayée telle quelle."""
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", True)
        called = {}

        async def fake_proxy(request):
            from starlette.responses import JSONResponse
            called["path"] = request.url.path
            called["method"] = request.method
            return JSONResponse({"proxied": True}, status_code=200)

        monkeypatch.setattr(replica_proxy, "_proxy_write_to_primary", fake_proxy)
        is_leader.clear()
        r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
        assert r.status_code == 200
        assert r.json().get("proxied") is True
        assert called.get("path") == "/api/auth/login"
        assert called.get("method") == "POST"

    def test_replica_killswitch_off_no_proxy(self, client, monkeypatch):
        """Kill-switch (flag OFF) : l'écriture n'est PAS relayée — elle atteint
        l'endpoint local (ancien comportement). Sur SQLite le write réussit."""
        async def boom(request):
            raise AssertionError("kill-switch OFF : aucune proxification attendue")

        monkeypatch.setattr(replica_proxy, "_proxy_write_to_primary", boom)
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", False)
        is_leader.clear()
        r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
        # Pas de proxy : l'endpoint local répond (SQLite n'est pas read-only).
        assert r.status_code == 200

    def test_replica_already_proxied_declines(self, client, monkeypatch):
        """Anti-boucle : une écriture DÉJÀ proxifiée (X-Write-Proxied) reçue par
        un replica n'est ni ré-exécutée ni re-proxifiée → 503 + header de refus,
        pour que le nœud d'entrée tente le peer suivant."""
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", True)

        async def boom(request):
            raise AssertionError("ne doit PAS re-proxifier une écriture déjà proxifiée")

        monkeypatch.setattr(replica_proxy, "_proxy_write_to_primary", boom)
        is_leader.clear()
        r = client.post(
            "/api/auth/login",
            json={"name": "user1", "password": "user123"},
            headers={"X-Write-Proxied": "1"},
        )
        assert r.status_code == 503
        assert r.json()["detail"] == "replica"
        assert r.headers.get("X-Write-Proxy-Declined") == "1"

    def test_heartbeat_path_excluded(self, client, user1_headers, monkeypatch):
        """Le proxy générique d'écritures NE TOUCHE PAS /api/devices/heartbeat :
        ce chemin reste géré par le relai dédié INV-043 (devices.py).

        Preuve : on désactive le relai INV-043 (kill-switch devices) pour obtenir
        un 503 "replica" déterministe, et on fait planter le proxy générique s'il
        intervenait. Le 503 "replica" prouve que le générique a laissé passer."""
        async def boom(request):
            raise AssertionError("le proxy d'écritures ne doit pas toucher le heartbeat")

        monkeypatch.setattr(replica_proxy, "_proxy_write_to_primary", boom)
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", True)
        # INV-043 kill-switch OFF → heartbeat replica = 503 "replica" déterministe.
        monkeypatch.setattr(devices, "_PROXY_ON_REPLICA", False)
        is_leader.clear()
        r = client.post("/api/devices/heartbeat", headers=user1_headers)
        assert r.status_code == 503
        assert r.json()["detail"] == "replica"

    def test_deployments_path_excluded(self, client, monkeypatch):
        """`/api/deployments/*` est exclu : il garde son propre garde
        replica→503 "replica" (plumbing CD, leader découvert par les scripts).

        Preuve : on fait planter le proxy générique s'il intervenait ; le 503
        "replica" provient du garde natif de deployments.py (INV non relayé)."""
        async def boom(request):
            raise AssertionError("le proxy d'écritures ne doit pas toucher /api/deployments")

        monkeypatch.setattr(replica_proxy, "_proxy_write_to_primary", boom)
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", True)
        # GATEWAY_KEY lu à chaud par deployments._check_gateway_key → on le fixe
        # pour passer l'auth et atteindre le garde natif replica→503.
        monkeypatch.setenv("GATEWAY_KEY", "inv044-gw-key")
        is_leader.clear()
        body = {
            "node": "node1",
            "image": "alarm-backend",
            "kind": "pull",
            "to_digest": "sha256:inv044-excluded",
            "status": "success",
        }
        r = client.post(
            "/api/deployments/events",
            json=body,
            headers={"X-Gateway-Key": "inv044-gw-key"},
        )
        # 503 "replica" = garde NATIF de deployments.py (pas le proxy générique,
        # qui aurait appelé boom). Prouve l'exclusion de /api/deployments.
        assert r.status_code == 503
        assert r.json()["detail"] == "replica"

    def test_get_read_not_proxied(self, client, admin_headers, monkeypatch):
        """Une lecture (GET) sur un replica n'est jamais relayée (read-only OK)."""
        async def boom(request):
            raise AssertionError("un GET ne doit jamais être proxifié")

        monkeypatch.setattr(replica_proxy, "_proxy_write_to_primary", boom)
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", True)
        is_leader.clear()
        r = client.get("/api/users/", headers=admin_headers)
        assert r.status_code == 200
