"""
Tier 2 integration tests : contrat de POST /api/config/escalation.

Couvre INV-019 (tranche 2026-04-20 dans tests/INVARIANTS.md) :
  Dans EscalationConfig, `position` est unique. POST /api/config/escalation avec
  position deja occupee doit retourner 409 Conflict, JAMAIS un upsert silencieux.

Motivation (issue #19) : un admin qui se trompe de `position` dans sa requete
risque d'ecraser un user existant sans avertissement, ce qui peut sortir de la
chaine le seul user disponible un week-end d'astreinte.

Budget P4 : 5 tests max. Ici : 2 tests — 1 pour le 409, 1 pour la preservation
de l'etat existant (invariant).
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
