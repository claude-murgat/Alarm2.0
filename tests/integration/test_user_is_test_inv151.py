"""
Tier 2 integration : INV-151 — role « test » d'un utilisateur.

Le booleen `is_test` se choisit a la creation (comme `is_admin`) et revient
dans le `user` du login : c'est la que l'app Android le lit pour ne pas faire
sonner l'alarme (INV-ANDROID-008). Une base existante doit gagner la colonne
a `false` : un NULL casserait `UserResponse` (bool) sur tous les comptes.
"""
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


def _register_and_login(client, admin_headers, **extra):
    name = f"inv151{uuid.uuid4().hex[:8]}"
    r = client.post("/api/auth/register", headers=admin_headers,
                    json={"name": name, "password": "pass1234", **extra})
    assert r.status_code == 200, r.text
    login = client.post("/api/auth/login", json={"name": name, "password": "pass1234"})
    assert login.status_code == 200, login.text
    return r.json()["id"], login.json()["user"]


def test_test_role_set_at_creation_is_returned_to_the_app(client, admin_headers):
    """INV-151 : un compte cree avec is_test=true le voit dans le `user` du
    login (lu par l'app) et dans la liste des utilisateurs (lue par le web)."""
    user_id, login_user = _register_and_login(client, admin_headers, is_test=True)
    try:
        assert login_user.get("is_test") is True, (
            f"Le login d'un compte test doit renvoyer user.is_test=true, got {login_user}"
        )
        listed = {u["id"]: u for u in client.get("/api/users/", headers=admin_headers).json()}
        assert listed[user_id].get("is_test") is True, (
            f"GET /api/users/ doit exposer is_test=true, got {listed[user_id]}"
        )
    finally:
        client.delete(f"/api/users/{user_id}", headers=admin_headers)


def test_test_role_defaults_to_false(client, admin_headers):
    """INV-151 : sans is_test a la creation, le compte n'est pas un compte test
    (champ present et faux, pas absent)."""
    user_id, login_user = _register_and_login(client, admin_headers)
    try:
        assert login_user.get("is_test") is False, (
            f"Un compte cree sans is_test doit valoir is_test=false, got {login_user}"
        )
    finally:
        client.delete(f"/api/users/{user_id}", headers=admin_headers)


def test_migration_adds_is_test_false_to_existing_users(client):
    """INV-151 : une base d'avant le role test gagne la colonne, a false pour
    les comptes deja la."""
    from backend.app.database import Base, run_migrations

    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db", prefix="alarm-inv151-")
    f.close()
    engine = create_engine(f"sqlite:///{f.name}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(users)"))]
        if "is_test" in cols:  # base d'avant INV-151
            conn.execute(text("ALTER TABLE users DROP COLUMN is_test"))
        conn.execute(text(
            "INSERT INTO users (name, hashed_password, is_admin, is_active, is_online) "
            "VALUES ('ancien', 'x', 0, 1, 0)"
        ))

    run_migrations(engine)

    with engine.connect() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(users)"))]
        assert "is_test" in cols, "run_migrations doit ajouter users.is_test"
        value = conn.execute(text("SELECT is_test FROM users WHERE name = 'ancien'")).scalar()
    assert value is not None and not value, (
        f"Un compte existant doit valoir is_test=false apres migration, got {value!r}"
    )
