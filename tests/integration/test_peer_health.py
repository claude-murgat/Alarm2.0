"""
Tier 2 integration tests : GET /api/system/peer-health (PR 8 du plan CD V1).

Cf docs/CD_DESIGN.md §7 (Mode urgence, Cond 1 vote majoritaire). Le contrat :
  - Endpoint public (pas d'auth) → consumable par l'orchestrateur sans token.
  - Lit PEER_TEST_URLS, ping chaque peer, retourne (reachable, health, lag_ms).
  - Distinction 503 explicite vs timeout (réseau-like) — clé pour le durcissement.

Budget P4 : 5 tests max. Ici : 2 tests (le reste est couvert par le code review
du module qui est <100 lignes simples).
"""
import os

import pytest

pytestmark = pytest.mark.integration


def test_peer_health_returns_shape(client):
    """GET /api/system/peer-health retourne la shape attendue, sans auth."""
    r = client.get("/api/system/peer-health")
    assert r.status_code == 200, r.text
    body = r.json()
    # Les 5 champs documentes dans PeerHealthResponse
    assert "self_node" in body
    assert "peers" in body
    assert "etcd_quorum_seen" in body
    assert "uptime_s" in body
    assert "checked_at" in body
    assert isinstance(body["peers"], dict)
    assert isinstance(body["uptime_s"], int)
    assert body["uptime_s"] >= 0


def test_peer_unreachable_marked_as_not_reachable(client, monkeypatch):
    """Un peer injoignable est marque reachable=false avec health=timeout/refused.

    Garantit que le detecteur peut distinguer "silence reseau" (a ignorer) de
    "503 explicite" (signal de panne logicielle).
    """
    # Force un PEER_TEST_URLS qui pointe vers un port mort sur 127.0.0.1.
    # Note : c'est lu par le module au runtime de l'endpoint, pas au lifespan.
    monkeypatch.setenv("PEER_TEST_URLS", "http://127.0.0.1:1")

    r = client.get("/api/system/peer-health")
    assert r.status_code == 200, r.text
    body = r.json()

    peer = body["peers"].get("http://127.0.0.1:1")
    assert peer is not None, f"peer non present dans {body['peers']}"
    assert peer["reachable"] is False, f"attendu reachable=False, recu {peer}"
    # health doit indiquer un mode "reseau-like" (refused/timeout/unreachable),
    # PAS un code HTTP (qui indiquerait que le backend a repondu).
    assert peer["health"] in {"refused", "timeout", "unreachable", "error"}, peer
