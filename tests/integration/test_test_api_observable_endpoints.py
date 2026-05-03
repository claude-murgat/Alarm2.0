"""
Tier 2 integration tests : endpoints /api/test/* utilises pour substituer
les blind `time.sleep(N)` par du polling sur condition observable.

Couvre l'etape 0a du chantier #21 (failover bloquant) : exposer un endpoint
detaille avec timestamps de heartbeat permettant aux tests E2E de poller sur
"user X a heartbeate depuis < Y secondes" plutot que d'attendre N secondes
aveuglement (race conditions sur lente CI).

Prerequis : ENABLE_TEST_ENDPOINTS=true (set par conftest.py).
"""
import pytest

pytestmark = pytest.mark.integration


def test_connected_users_detailed_returns_all_seed_users(client):
    """Sans filtre user_id, retourne tous les users avec champs attendus."""
    r = client.get("/api/test/connected-users-detailed")
    assert r.status_code == 200, r.text
    body = r.json()

    assert "users" in body
    assert "now" in body

    # Seed users de main.py : admin, user1, user2
    names = {u["name"] for u in body["users"]}
    assert {"admin", "user1", "user2"}.issubset(names), (
        f"seed users manquants ; got {names}"
    )

    # Chaque entree a les champs attendus avec types attendus
    for u in body["users"]:
        assert isinstance(u["id"], int)
        assert isinstance(u["name"], str)
        assert isinstance(u["is_online"], bool)
        # last_heartbeat / age_seconds peuvent etre None (user jamais connecte)
        assert u["last_heartbeat"] is None or isinstance(u["last_heartbeat"], str)
        assert u["age_seconds"] is None or isinstance(u["age_seconds"], (int, float))


def test_connected_users_detailed_with_user_id_filter(client):
    """Avec ?user_id=N, retourne uniquement cet user."""
    # Recuperer l'id admin via login
    r_login = client.post(
        "/api/auth/login", json={"name": "admin", "password": "admin123"}
    )
    assert r_login.status_code == 200, r_login.text
    admin_id = r_login.json()["user"]["id"]

    r = client.get(f"/api/test/connected-users-detailed?user_id={admin_id}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["users"]) == 1
    assert body["users"][0]["id"] == admin_id
    assert body["users"][0]["name"] == "admin"


def test_connected_users_detailed_with_unknown_user_id_returns_empty_list(client):
    """user_id inexistant -> liste vide, pas 404. Permet aux tests E2E
    d'iterer sur une liste vide sans cas special."""
    r = client.get("/api/test/connected-users-detailed?user_id=999999")
    assert r.status_code == 200
    body = r.json()
    assert body["users"] == []
    assert "now" in body  # timestamp serveur present meme si liste vide


def test_connected_users_detailed_age_seconds_increases_over_time(client):
    """Age_seconds doit refleter le temps ecoule entre 2 lectures.
    Garantit qu'on peut polling sur "age_seconds < N" sans craindre un cache stale."""
    # Recuperer 2 snapshots a quelques ms d'intervalle
    r1 = client.get("/api/test/connected-users-detailed")
    r2 = client.get("/api/test/connected-users-detailed")
    assert r1.status_code == 200 and r2.status_code == 200

    users1 = {u["id"]: u for u in r1.json()["users"]}
    users2 = {u["id"]: u for u in r2.json()["users"]}

    # Pour chaque user qui a un heartbeat, age_seconds doit etre >= dans r2
    for uid, u1 in users1.items():
        u2 = users2.get(uid)
        if u1["age_seconds"] is None or u2 is None or u2["age_seconds"] is None:
            continue
        assert u2["age_seconds"] >= u1["age_seconds"], (
            f"user {uid}: age_seconds doit etre monotone croissant tant qu'il "
            f"n'y a pas de nouveau heartbeat. r1={u1['age_seconds']}, r2={u2['age_seconds']}"
        )
