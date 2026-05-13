"""
Tier 2 integration tests : INV-074 — POST /auth/refresh produit un nouveau token.

Source de verite : tests/INVARIANTS.md ligne 349-350.
> POST /auth/refresh avec token valide → nouveau token different.

Un refresh qui renverrait le token recu en entree ne sert a rien : le client
peut continuer a l'utiliser mais ne rafraichit pas son TTL. Ces tests
verrouillent la propriete (lock-in regression).

Budget P4 : 2 tests.
"""
import pytest

pytestmark = pytest.mark.integration


def test_new_refresh_returns_distinct_token(client):
    """Attrape la regression ou /auth/refresh renverrait le token recu en input.

    Si l'implementation faisait `return {"access_token": input_token}`,
    cette assertion failerait (token_after == token_before).
    """
    login = client.post(
        "/api/auth/login", json={"name": "admin", "password": "admin123"}
    )
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
    token_before = login.json()["access_token"]

    r = client.post(
        "/api/auth/refresh", headers={"Authorization": f"Bearer {token_before}"}
    )
    assert r.status_code == 200, f"refresh failed: {r.status_code} {r.text}"
    token_after = r.json()["access_token"]

    assert isinstance(token_after, str) and len(token_after) > 20
    assert token_after != token_before, (
        "INV-074 viole : /auth/refresh a renvoye le meme token qu'en entree"
    )


def test_new_token_is_usable(client):
    """Attrape une regression ou le token refresh serait malforme / non valide.

    Verifie que le token retourne par /auth/refresh authentifie un GET protege
    (`/api/auth/me`) et identifie bien le bon user.
    """
    login = client.post(
        "/api/auth/login", json={"name": "admin", "password": "admin123"}
    )
    assert login.status_code == 200
    token_before = login.json()["access_token"]

    r = client.post(
        "/api/auth/refresh", headers={"Authorization": f"Bearer {token_before}"}
    )
    assert r.status_code == 200
    token_after = r.json()["access_token"]

    me = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token_after}"}
    )
    assert me.status_code == 200, f"new token rejected by /me: {me.status_code} {me.text}"
    assert me.json()["name"] == "admin"
