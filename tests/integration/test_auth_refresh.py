"""
Tier 2 integration tests : INV-074 — POST /auth/refresh produit un nouveau token.

Source de verite : tests/INVARIANTS.md (INV-074 [C]).
> POST /auth/refresh avec token valide → nouveau token different.
> POST /auth/refresh doit rejeter (401) les tokens invalides : expire,
> signature corrompue, header absent.

Path heureux : un refresh qui renverrait le token recu en entree ne sert a
rien (pas de rotation TTL). Path negatif : un /auth/refresh qui accepterait un
token expire ou mal signe = trou de securite majeur (un attaquant qui sniffe
un token expire pourrait re-emettre un acces indefiniment, contournant
l'expiration JWT). Ces tests verrouillent la propriete (lock-in regression).

Budget P4 : 4 tests (2 happy + 2 negatifs).
"""
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

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


def _admin_user_id(client) -> int:
    """Renvoie l'id du user admin seede (sub valide pour forger un JWT)."""
    login = client.post(
        "/api/auth/login", json={"name": "admin", "password": "admin123"}
    )
    assert login.status_code == 200, f"login failed: {login.status_code} {login.text}"
    return login.json()["user"]["id"]


def test_refresh_rejects_expired_token(client):
    """Attrape le bug ou /auth/refresh accepterait un token JWT expire.

    On forge un JWT signe avec le VRAI SECRET_KEY mais dont `exp` est dans le
    passe, et dont `sub` pointe sur un user qui existe (admin) — ainsi la SEULE
    raison possible d'un 401 est l'expiration, pas un "user not found".

    Si le handler retirait la verif d'expiration (jwt.decode avec
    verify_exp=False), ce token expire serait accepte et l'endpoint renverrait
    200 + un nouveau token => contournement complet de l'expiration JWT.
    """
    from backend.app.auth import ALGORITHM, SECRET_KEY

    user_id = _admin_user_id(client)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    expired_token = jwt.encode(
        {
            "sub": str(user_id),
            "iat": past - timedelta(hours=24),
            "exp": past,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )

    r = client.post(
        "/api/auth/refresh",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert r.status_code == 401, (
        f"INV-074 viole : token expire accepte par /auth/refresh "
        f"({r.status_code} {r.text})"
    )
    assert "access_token" not in r.json(), (
        "INV-074 viole : /auth/refresh a re-emis un token a partir d'un token expire"
    )


def test_refresh_rejects_bad_signature_token(client):
    """Attrape le bug ou /auth/refresh accepterait un token mal signe.

    On forge un JWT structurellement valide (sub = admin existant, exp futur)
    mais signe avec un SECRET_KEY different. Si le handler retirait la verif de
    signature (jwt.decode avec verify_signature=False), n'importe qui pourrait
    forger un token et obtenir un acces valide via /auth/refresh.
    """
    from backend.app.auth import ALGORITHM

    user_id = _admin_user_id(client)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    forged_token = jwt.encode(
        {
            "sub": str(user_id),
            "iat": datetime.now(timezone.utc),
            "exp": future,
        },
        "wrong-secret-key-not-the-real-one",
        algorithm=ALGORITHM,
    )

    r = client.post(
        "/api/auth/refresh",
        headers={"Authorization": f"Bearer {forged_token}"},
    )
    assert r.status_code == 401, (
        f"INV-074 viole : token mal signe accepte par /auth/refresh "
        f"({r.status_code} {r.text})"
    )
    assert "access_token" not in r.json(), (
        "INV-074 viole : /auth/refresh a re-emis un token a partir d'un token mal signe"
    )
