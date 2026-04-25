"""
Tier 2 integration tests : contrat de POST /api/config/escalation.

Couvre INV-019 et INV-020 (tests/INVARIANTS.md) :
  - INV-019 : position est unique. POST avec position occupee → 409.
  - INV-020 : user_id est unique. POST avec user_id deja present → 409.

Motivation :
  - INV-019 (issue #19) : un admin qui se trompe de position risque d'ecraser
    un user existant.
  - INV-020 (2026-04-25) : meme verrou mais sur user_id, pour eviter qu'un user
    soit a 2 positions (sonneries doublees + casse l'invariant 'first occurrence'
    sur lequel s'appuie la logique pure `_find_next_user_id`).

Budget P4 : 5 tests max. Ici : 3 tests.
"""
import pytest

pytestmark = pytest.mark.integration


def _get_user_id(client, admin_headers, name):
    r = client.get("/api/users/", headers=admin_headers)
    assert r.status_code == 200, r.text
    for u in r.json():
        if u["name"] == name:
            return u["id"]
    raise AssertionError(f"user {name} not found in GET /api/users/")


def test_post_existing_position_returns_409_and_does_not_overwrite(client, admin_headers):
    """INV-019 : POST /config/escalation avec position deja occupee doit 409.

    Attrape le bug de l'issue #19 : aujourd'hui le serveur fait un upsert
    silencieux (200 OK) et remplace le user existant a cette position.
    Apres fix, un 4xx (409) est retourne et la chaine reste inchangee.
    """
    # Etat seed: position 1 = user1, position 2 = user2, position 3 = admin (main.py:55-58).
    # On lit la chaine pour etre independant de l'ordre des tests (pytest-randomly).
    r = client.get("/api/config/escalation")
    assert r.status_code == 200, r.text
    chain = r.json()
    assert len(chain) >= 2, f"seed chain should have >=2 entries, got {chain}"

    target = chain[0]  # une position deja occupee
    target_position = target["position"]
    occupant_user_id = target["user_id"]

    # Choisir un autre user (different de l'occupant courant).
    other = next((e for e in chain if e["user_id"] != occupant_user_id), None)
    assert other is not None, "need at least 2 different users in seed chain"
    other_user_id = other["user_id"]

    # Tentative de prise de la meme position par un autre user.
    r = client.post(
        "/api/config/escalation",
        json={"position": target_position, "user_id": other_user_id, "delay_minutes": 15.0},
        headers=admin_headers,
    )
    assert r.status_code == 409, (
        f"INV-019: POST sur position deja occupee doit 409, got {r.status_code} {r.text}"
    )

    # L'occupant initial doit etre toujours en place (pas d'ecrasement silencieux).
    r = client.get("/api/config/escalation")
    assert r.status_code == 200
    still = next((e for e in r.json() if e["position"] == target_position), None)
    assert still is not None, f"position {target_position} disappeared from chain"
    assert still["user_id"] == occupant_user_id, (
        f"INV-019: position {target_position} doit rester assigne a user {occupant_user_id}, "
        f"got user {still['user_id']}"
    )


def test_post_new_position_still_succeeds(client, admin_headers):
    """Regression guard : creer une position LIBRE doit continuer de marcher (200).

    Le fix INV-019 doit rejeter uniquement les collisions de position, pas les
    insertions legitimes.
    """
    # Trouver une position libre en remontant au-dessus du max existant.
    r = client.get("/api/config/escalation")
    assert r.status_code == 200
    existing = r.json()
    free_position = max((e["position"] for e in existing), default=0) + 10

    # Choisir n'importe quel user_id different de ceux deja dans la chaine
    # pour eviter un hypothetique conflit de user_id (INV-020, hors scope).
    used_user_ids = {e["user_id"] for e in existing}
    # admin/user1/user2 sont 3 users seed (main.py:43-47). On cree un autre user.
    r = client.post(
        "/api/auth/register",
        json={"name": f"probe{free_position}", "password": "probe123"},
    )
    assert r.status_code in (200, 201), r.text
    probe_user_id = r.json()["id"] if "id" in r.json() else r.json().get("user", {}).get("id")
    if not probe_user_id:
        # Fallback : relire via /api/users/.
        probe_user_id = _get_user_id(client, admin_headers, f"probe{free_position}")
    assert probe_user_id not in used_user_ids, "probe user should be new"

    try:
        r = client.post(
            "/api/config/escalation",
            json={"position": free_position, "user_id": probe_user_id, "delay_minutes": 15.0},
            headers=admin_headers,
        )
        assert r.status_code == 200, (
            f"POST /config/escalation sur position libre doit passer, got {r.status_code} {r.text}"
        )
        body = r.json()
        assert body["position"] == free_position
        assert body["user_id"] == probe_user_id
    finally:
        # Nettoyage : retirer la ligne qu'on a ajoutee pour ne pas polluer les autres tests.
        r = client.get("/api/config/escalation")
        for e in r.json():
            if e["position"] == free_position:
                client.delete(f"/api/config/escalation/{e['id']}", headers=admin_headers)
                break


def test_post_existing_user_id_returns_409_and_does_not_overwrite(client, admin_headers):
    """INV-020 : POST /config/escalation avec un user_id deja present dans la chaine
    doit retourner 409. Sinon le meme user serait sonne 2 fois (en plus de casser
    l'invariant 'first occurrence' implicite dans `_find_next_user_id`).

    Le verrou existe deja sur /escalation/bulk (validation 422 sur la liste user_ids) ;
    ce test verifie qu'il existe aussi sur le single insert.
    """
    r = client.get("/api/config/escalation")
    assert r.status_code == 200, r.text
    chain = r.json()
    assert len(chain) >= 1, f"seed chain should have >=1 entries, got {chain}"

    # Cibler un user deja dans la chaine.
    target = chain[0]
    target_user_id = target["user_id"]
    target_old_position = target["position"]

    # Position libre (au-dessus du max existant) ou on tenterait de re-mettre target.
    free_position = max((e["position"] for e in chain), default=0) + 10

    r = client.post(
        "/api/config/escalation",
        json={"position": free_position, "user_id": target_user_id, "delay_minutes": 15.0},
        headers=admin_headers,
    )
    assert r.status_code == 409, (
        f"INV-020: POST avec user_id deja en chaine doit 409, got {r.status_code} {r.text}"
    )

    # L'user n'a pas bouge — toujours unique dans la chaine, position inchangee.
    r = client.get("/api/config/escalation")
    assert r.status_code == 200
    chain_after = r.json()
    user_entries = [e for e in chain_after if e["user_id"] == target_user_id]
    assert len(user_entries) == 1, (
        f"INV-020: user {target_user_id} doit rester unique dans la chaine, "
        f"got {len(user_entries)} entries: {user_entries}"
    )
    assert user_entries[0]["position"] == target_old_position, (
        f"user {target_user_id} a bouge de pos {target_old_position} a "
        f"pos {user_entries[0]['position']} : 409 ne doit jamais autoriser un move silencieux"
    )
