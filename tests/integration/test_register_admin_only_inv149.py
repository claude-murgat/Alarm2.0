"""
Tier 2 integration : INV-149 — seul un admin peut creer un compte.

Avant : POST /api/auth/register etait public. N'importe qui joignant l'API
pouvait se creer un compte admin (`is_admin` dans le body).
"""
import uuid

import pytest

pytestmark = pytest.mark.integration


def _names(client, admin_headers):
    return {u["name"] for u in client.get("/api/users/", headers=admin_headers).json()}


def test_register_without_token_creates_nothing(client, admin_headers):
    """INV-149 : sans jeton -> refus, aucun compte cree (meme en demandant admin)."""
    name = f"intrus{uuid.uuid4().hex[:8]}"
    r = client.post("/api/auth/register", json={"name": name, "password": "pass1234", "is_admin": True})
    assert r.status_code in (401, 403), f"Creation sans jeton doit etre refusee, got {r.status_code}: {r.text}"
    assert name not in _names(client, admin_headers), "Aucun compte ne doit avoir ete cree"


def test_register_by_non_admin_creates_nothing(client, admin_headers):
    """INV-149 : un utilisateur non admin (user1) -> 403, aucun compte cree."""
    tok = client.post("/api/auth/login", json={"name": "user1", "password": "user123"}).json()["access_token"]
    name = f"intrus{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/api/auth/register", json={"name": name, "password": "pass1234"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 403, f"Creation par un non-admin doit renvoyer 403, got {r.status_code}: {r.text}"
    assert name not in _names(client, admin_headers), "Aucun compte ne doit avoir ete cree"
