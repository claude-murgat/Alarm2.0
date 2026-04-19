"""
Tier 2 integration tests : contrat d'authentification API.

Couvre les invariants de l'API d'auth qui doivent rester stables face aux refactors :
- Login valide retourne un JWT exploitable.
- Login invalide est rejete (pas de fuite d'info via timing/format different).
- Routes protegees rejetent l'absence de token.

Budget P4 = 5 tests max sur ce module. Aujourd'hui : 4 tests.
"""
import pytest

pytestmark = pytest.mark.integration


def test_login_admin_succeeds_and_returns_jwt(client):
    """Attrape une regression sur le contrat de login si POST /api/auth/login change de schema."""
    r = client.post("/api/auth/login", json={"name": "admin", "password": "admin123"})
    assert r.status_code == 200
    body = r.json()
    assert "access_token" in body and isinstance(body["access_token"], str) and len(body["access_token"]) > 20
    assert body["user"]["name"] == "admin"
    assert body["user"]["is_admin"] is True


def test_login_wrong_password_rejected_with_401(client):
    """Attrape une regression si un mauvais mot de passe renverrait 200 (= bypass auth)."""
    r = client.post("/api/auth/login", json={"name": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert "access_token" not in r.text


def test_login_unknown_user_rejected_with_401(client):
    """Attrape une fuite d'info : meme code 401 pour user inconnu que mauvais mot de passe."""
    r = client.post("/api/auth/login", json={"name": "ghost", "password": "anything"})
    assert r.status_code == 401


def test_protected_route_requires_token(client):
    """Attrape une regression si une route admin devenait accessible sans auth.

    GET /api/users liste tous les users — endpoint admin, doit refuser sans token.
    """
    r = client.get("/api/users/")
    assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"
