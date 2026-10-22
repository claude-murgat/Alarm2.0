"""
Tier 2 integration : INV-148 — role « test » d'un utilisateur.

Le booleen `is_test` se choisit a la creation (comme `is_admin`) et revient
dans le `user` du login : c'est la que l'app Android le lit pour ne pas faire
sonner l'alarme (INV-ANDROID-008). Une base existante doit gagner la colonne
a `false` : un NULL casserait `UserResponse` (bool) sur tous les comptes.
"""
import tempfile

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


def test_migration_adds_is_test_false_to_existing_users(client):
    """INV-148 : une base d'avant le role test gagne la colonne, a false pour
    les comptes deja la."""
    from backend.app.database import Base, run_migrations

    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db", prefix="alarm-inv148-")
    f.close()
    engine = create_engine(f"sqlite:///{f.name}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(users)"))]
        if "is_test" in cols:  # base d'avant INV-148
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
