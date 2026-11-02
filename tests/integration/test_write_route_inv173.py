"""Tier 2 integration : INV-173 — le relais d'écriture (INV-044) et de heartbeat (INV-043) d'un replica
vise d'abord le leader Patroni, connexion bornée à 1,5 s.

Faux Patroni par `urlopen` (cf INV-161/168) : node1 (10.99.0.2, Izeron) est éteint et en tête de
`PEER_TEST_URLS`, node3 (10.99.0.1, OVH) est leader. httpx est remplacé : le peer mort lève.
"""
import io
import json
import urllib.error
import urllib.request

import httpx
import pytest

from backend.app import replica_proxy
from backend.app.api import devices
from backend.app.leader_election import is_leader

pytestmark = pytest.mark.integration

DEAD, LEADER = "http://10.99.0.2:8000", "http://10.99.0.1:8000"
MEMBERS = [{"name": "node1", "role": "replica", "state": "streaming", "host": "10.99.0.2"},
           {"name": "node3", "role": "leader", "state": "running", "host": "10.99.0.1"},
           {"name": "node2", "role": "replica", "state": "streaming", "host": "10.99.0.3"}]


@pytest.fixture
def patroni(monkeypatch):
    """`fake["cluster"] = None` : Patroni local injoignable. `timeouts` : ceux des GET /cluster."""
    fake = {"cluster": {"members": MEMBERS}, "timeouts": []}
    real = urllib.request.urlopen

    def urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/cluster"):
            fake["timeouts"].append(kw.get("timeout"))
            if fake["cluster"] is None:
                raise urllib.error.URLError("connection refused")
            return io.BytesIO(json.dumps(fake["cluster"]).encode())
        return real(req, *a, **kw)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return fake


@pytest.fixture
def replica(monkeypatch, client):
    """Ce nœud devient replica (après le login, qui est une écriture) ; jeton user1 renvoyé."""
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    assert r.status_code == 200, r.text
    for mod in (replica_proxy, devices):
        monkeypatch.setattr(mod, "_PROXY_ON_REPLICA", True)
        monkeypatch.setattr(mod, "_PEER_URLS", [DEAD, LEADER])
    is_leader.clear()
    yield {"Authorization": f"Bearer {r.json()['access_token']}"}
    is_leader.set()


def _fake_async_client(monkeypatch):
    """httpx.AsyncClient du relais d'écriture : enregistre le timeout et les URLs ; le peer mort lève."""
    calls = {"timeout": None, "urls": []}

    class Client:
        def __init__(self, *a, **kw):
            calls["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def request(self, method, url, headers=None, content=None):
            calls["urls"].append(url)
            if url.startswith(DEAD):
                raise httpx.ConnectError("unreachable")
            return httpx.Response(200, headers=[("content-type", "application/json")], content=b'{"ok":true}')
    monkeypatch.setattr(replica_proxy.httpx, "AsyncClient", Client)
    return calls


def test_write_relay_tries_patroni_leader_first(client, patroni, replica, monkeypatch):
    calls = _fake_async_client(monkeypatch)
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls["urls"] == [f"{LEADER}/api/auth/login"], "le leader d'abord : le peer mort n'est pas essayé"
    t = calls["timeout"]
    assert isinstance(t, httpx.Timeout) and t.connect == 1.5 and t.read == 5.0


def test_write_relay_keeps_configured_order_without_patroni(client, patroni, replica, monkeypatch):
    patroni["cluster"] = None
    calls = _fake_async_client(monkeypatch)
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    assert r.status_code == 200
    assert calls["urls"] == [f"{DEAD}/api/auth/login", f"{LEADER}/api/auth/login"]


def test_heartbeat_relay_tries_patroni_leader_first(client, patroni, replica, monkeypatch):
    calls = []

    def post(url, headers=None, timeout=None):
        calls.append((url, timeout))
        if url.startswith(DEAD):
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, json={"status": "ok", "timestamp": "2026-10-30T12:00:00"})
    monkeypatch.setattr(httpx, "post", post)
    r = client.post("/api/devices/heartbeat", headers=replica)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert [u for u, _ in calls] == [f"{LEADER}/api/devices/heartbeat"]
    t = calls[0][1]
    assert isinstance(t, httpx.Timeout) and t.connect == 1.5 and t.read == 3.0
    # GET /cluster borné à 1 s (budget de 5 s de l'app) ; un tick de fond peut aussi le lire à 3 s.
    assert 1 in patroni["timeouts"]
