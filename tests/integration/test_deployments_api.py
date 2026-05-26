"""
Tier 2 integration tests : API /api/deployments (PR 4 du plan CD V1).

Cf docs/CD_DESIGN.md §6 (Observabilité du déploiement). Le contrat doit garantir :
  - POST /events requiert X-Gateway-Key (cohérent avec INV-065 internal/sms/calls)
  - kind/status sont validés (pas de valeur arbitraire en table)
  - GET /events est admin-only
  - GET /state agrège correctement le dernier event par (node, image)

Budget P4 : 5 tests max. Ici : 5 tests.
"""
import os

import pytest

pytestmark = pytest.mark.integration


GATEWAY_KEY = os.environ.setdefault("GATEWAY_KEY", "test-gateway-key-cd")


def _gw_headers():
    return {"X-Gateway-Key": GATEWAY_KEY}


def test_post_event_requires_gateway_key(client, admin_headers):
    """POST /api/deployments/events sans header → 401, et admin token n'est PAS suffisant.

    INV implicite (CD §6) : seul l'orchestrateur écrit. L'admin lit, n'écrit pas.
    """
    body = {
        "node": "node1",
        "image": "alarm-backend",
        "kind": "pull",
        "to_digest": "sha256:abc",
        "status": "success",
    }
    # Sans header
    r = client.post("/api/deployments/events", json=body)
    assert r.status_code == 401, r.text

    # Avec admin bearer (mais pas de X-Gateway-Key) → toujours 401
    r = client.post("/api/deployments/events", json=body, headers=admin_headers)
    assert r.status_code == 401, r.text


def test_post_event_rejects_invalid_kind(client):
    """Validation stricte : un kind hors enum est rejeté (400).

    Attrape les fautes de frappe côté orchestrateur (ex: 'cannary_start' au lieu
    de 'canary_start') qui sinon pollueraient les dashboards et casseraient le
    rollback automatique qui filtre par kind.
    """
    body = {
        "node": "node3",
        "image": "alarm-backend",
        "kind": "totally-not-a-real-kind",
        "status": "success",
    }
    r = client.post("/api/deployments/events", json=body, headers=_gw_headers())
    assert r.status_code == 400, r.text
    assert "Invalid kind" in r.text


def test_post_and_list_round_trip(client, admin_headers):
    """Round-trip : insertion gateway → lecture admin retourne l'event."""
    body = {
        "node": "node2",
        "image": "alarm-patroni",
        "kind": "canary_promoted",
        "from_digest": "sha256:old",
        "to_digest": "sha256:new",
        "status": "success",
        "actor": "orchestrator",
        "details": {"soak_seconds": 600, "lag_ms": 12},
    }
    r = client.post("/api/deployments/events", json=body, headers=_gw_headers())
    assert r.status_code == 201, r.text
    event_id = r.json()["id"]

    # Admin peut lister
    r = client.get(
        "/api/deployments/events?node=node2&kind=canary_promoted",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    found = [e for e in events if e["id"] == event_id]
    assert len(found) == 1, f"event {event_id} not found in {events}"
    assert found[0]["details"]["soak_seconds"] == 600
    assert found[0]["actor"] == "orchestrator"


def test_state_endpoint_returns_latest_per_node_image(client, admin_headers):
    """GET /state retourne le dernier event par (node, image), pas l'historique."""
    # Insérer 2 events sur le même (node, image) avec différents kind
    for kind, to_digest in [("pull", "sha256:v1"), ("canary_promoted", "sha256:v2")]:
        r = client.post(
            "/api/deployments/events",
            json={
                "node": "node1",
                "image": "alarm-backend",
                "kind": kind,
                "to_digest": to_digest,
                "status": "success",
            },
            headers=_gw_headers(),
        )
        assert r.status_code == 201, r.text

    r = client.get("/api/deployments/state", headers=admin_headers)
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    key = "node1/alarm-backend"
    assert key in state, f"missing key in {list(state.keys())}"
    # Le plus récent doit être canary_promoted (inséré en 2e)
    assert state[key]["kind"] == "canary_promoted"
    assert state[key]["to_digest"] == "sha256:v2"


def test_post_event_on_replica_returns_503(client):
    """POST sur replica -> 503 'replica' (aligne sur INV-043 heartbeat).

    Sans ce garde, prod test 2026-05-23 : POST events declenche psycopg2
    ReadOnlySqlTransaction (500) sur les 2 replicas. Scripts CD doivent
    pouvoir basculer vers le leader via discover_leader() / GET /health.
    """
    from backend.app.leader_election import is_leader

    is_leader.clear()
    try:
        body = {
            "node": "node1",
            "image": "alarm-backend",
            "kind": "pull",
            "to_digest": "sha256:test-replica-503",
            "status": "success",
        }
        r = client.post("/api/deployments/events", json=body, headers=_gw_headers())
        assert r.status_code == 503, r.text
        assert "replica" in r.json().get("detail", "").lower(), r.json()
    finally:
        is_leader.set()
