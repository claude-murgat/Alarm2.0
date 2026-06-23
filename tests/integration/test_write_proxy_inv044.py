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
import httpx
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


def _install_fake_httpx_client(monkeypatch, peer_responses):
    """Stub `httpx.AsyncClient` utilisé par `_proxy_write_to_primary`.

    `peer_responses` : liste ordonnée de `(prefix, item)` où `item` est soit
    une `httpx.Response` à renvoyer, soit une `Exception` à lever. La 1ʳᵉ
    entrée dont le `prefix` matche le `url` de la requête est utilisée.

    Retourne la liste `calls` (mutable) que le test peut inspecter pour
    vérifier l'ordre d'itération des peers (anti-boucle, fall-through).
    """
    calls = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, headers=None, content=None):
            calls.append({"method": method, "url": url})
            for prefix, item in peer_responses:
                if url.startswith(prefix):
                    if isinstance(item, BaseException):
                        raise item
                    return item
            raise AssertionError(f"unstubbed peer call: {url}")

    monkeypatch.setattr(replica_proxy.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


class TestWriteProxyInv044HeaderRelay:
    """INV-044 — la spec dit littéralement « la réponse du leader est relayée
    telle quelle (200/401/429/…) ». Donc les headers métier du leader
    (Set-Cookie, WWW-Authenticate, Retry-After, ETag, Cache-Control,
    X-Correlation-ID) doivent traverser le proxy. Sans ce verrou, une PR
    future qui ajoute rate limiting, cookie de session ou OAuth bearer
    challenge sur un endpoint `/api/*` serait silencieusement cassée sur
    les replicas (header perdu en chemin).

    Ces tests exécutent le `_proxy_write_to_primary` RÉEL (httpx stubbé)
    au lieu de monkeypatcher la fonction entière : ils couvrent donc aussi
    les branches "decline-retry" et "no leader available" qui étaient
    auparavant non exercées (cf issue #177, point [M] §2).
    """

    def teardown_method(self):
        is_leader.set()

    def test_leader_response_headers_relayed_telles_quelles(
        self, client, monkeypatch
    ):
        """RED avant fix : `Response(media_type=...)` ne copie QUE
        content-type. Les autres headers du leader (Set-Cookie / WWW-
        Authenticate / Retry-After / ETag / Cache-Control / X-Correlation-
        ID) sont perdus → INV-044 « réponse relayée telle quelle » violé
        en silence pour tout ce qui sort du content-type.

        Sabotage mental : si on supprime la boucle `response.headers.append`
        dans `_proxy_write_to_primary`, ce test redevient rouge — il prouve
        donc bien le whitelist d'INV-044.
        """
        leader_resp = httpx.Response(
            status_code=401,
            headers=[
                ("content-type", "application/json"),
                ("www-authenticate", 'Bearer realm="api"'),
                ("retry-after", "30"),
                ("set-cookie", "session=abc123; Path=/; HttpOnly"),
                ("set-cookie", "csrf=xyz; Path=/"),
                ("etag", '"v1"'),
                ("cache-control", "no-store"),
                ("x-correlation-id", "trace-42"),
                # Headers volontairement NON whitelistés : ne doivent PAS fuiter.
                ("server", "leader-secret/1.0"),
                ("x-internal-debug", "should-not-leak"),
            ],
            content=b'{"detail":"unauthorized"}',
        )
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", True)
        monkeypatch.setattr(replica_proxy, "_PEER_URLS", ["http://leader.test"])
        _install_fake_httpx_client(
            monkeypatch, [("http://leader.test", leader_resp)]
        )
        is_leader.clear()

        r = client.post(
            "/api/auth/login", json={"name": "user1", "password": "user123"}
        )

        assert r.status_code == 401
        assert r.json() == {"detail": "unauthorized"}
        # Headers métier whitelistés → relayés tels quels.
        assert r.headers.get("www-authenticate") == 'Bearer realm="api"'
        assert r.headers.get("retry-after") == "30"
        assert r.headers.get("etag") == '"v1"'
        assert r.headers.get("cache-control") == "no-store"
        # Note : x-correlation-id est dans le whitelist du proxy, mais le
        # middleware `CorrelationIdMiddleware` (plus externe, cf main.py
        # ~L218) réécrit systématiquement ce header avec l'ID local du
        # request → impossible à observer côté client dans ce test, sa
        # bonne traversée du proxy se constate côté logs SRE.
        # Set-Cookie multi-valué : les DEUX cookies doivent arriver côté client.
        set_cookies = r.headers.get_list("set-cookie") if hasattr(
            r.headers, "get_list"
        ) else [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
        assert "session=abc123; Path=/; HttpOnly" in set_cookies
        assert "csrf=xyz; Path=/" in set_cookies
        # Non whitelistés → ne fuitent PAS (sinon risque d'exposer la stack
        # leader ou des headers internes via le replica).
        assert "x-internal-debug" not in {k.lower() for k in r.headers}
        # `server` est éventuellement réécrit par Starlette/TestClient → on
        # vérifie juste qu'on ne propage pas la valeur du leader.
        assert r.headers.get("server") != "leader-secret/1.0"

    def test_replica_decline_falls_through_to_next_peer(
        self, client, monkeypatch
    ):
        """Anti-boucle côté SORTIE : si le 1ᵉʳ peer répond 503 +
        `X-Write-Proxy-Declined: 1` (il est aussi replica), on tente le
        peer suivant jusqu'à trouver un leader. Cette branche réseau
        n'était pas couverte par les tests existants (tous stubaient
        `_proxy_write_to_primary` entier — cf issue #177 §2).
        """
        declined = httpx.Response(
            status_code=503,
            headers=[("x-write-proxy-declined", "1")],
            content=b'{"detail":"replica"}',
        )
        leader_ok = httpx.Response(
            status_code=200,
            headers=[("content-type", "application/json")],
            content=b'{"access_token":"jwt-from-leader"}',
        )
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", True)
        monkeypatch.setattr(
            replica_proxy,
            "_PEER_URLS",
            ["http://peer-replica.test", "http://peer-leader.test"],
        )
        calls = _install_fake_httpx_client(
            monkeypatch,
            [
                ("http://peer-replica.test", declined),
                ("http://peer-leader.test", leader_ok),
            ],
        )
        is_leader.clear()

        r = client.post(
            "/api/auth/login", json={"name": "user1", "password": "user123"}
        )

        # Le client voit la réponse du leader (pas le 503 du peer replica).
        assert r.status_code == 200
        assert r.json() == {"access_token": "jwt-from-leader"}
        # Les 2 peers ont été essayés, dans l'ordre, jusqu'à trouver le leader.
        urls = [c["url"] for c in calls]
        assert urls == [
            "http://peer-replica.test/api/auth/login",
            "http://peer-leader.test/api/auth/login",
        ]

    def test_no_peer_reachable_returns_503_no_primary(self, client, monkeypatch):
        """Tous les peers injoignables (panne cluster / pas de quorum / WG
        coupé) → 503 « no primary available for write ». Branche réseau
        non exercée par les tests existants (cf issue #177 §2).

        Couvre aussi le point [L] §3 : le `logger.warning(...)` ajouté ne
        doit pas faire péter le chemin réseau (sinon la branche serait
        rouge ici).
        """
        monkeypatch.setattr(replica_proxy, "_PROXY_ON_REPLICA", True)
        monkeypatch.setattr(
            replica_proxy,
            "_PEER_URLS",
            ["http://peer1.test", "http://peer2.test"],
        )
        _install_fake_httpx_client(
            monkeypatch,
            [
                ("http://peer1.test", httpx.ConnectError("unreachable")),
                ("http://peer2.test", httpx.ConnectError("unreachable")),
            ],
        )
        is_leader.clear()

        r = client.post(
            "/api/auth/login", json={"name": "user1", "password": "user123"}
        )

        assert r.status_code == 503
        assert r.json() == {"detail": "no primary available for write"}
