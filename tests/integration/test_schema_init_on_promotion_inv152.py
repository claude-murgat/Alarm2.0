"""
Tier 2 integration : INV-152 — un noeud qui devient leader applique les
migrations de schema en attente.

Constat prod 2026-10-22 (#253) : pendant le canary, chaque backend a demarre en
replica (Patroni relance, leader node2 -> node3) ; personne n'a rejoue les
migrations en devenant leader -> colonne users.is_test absente des 3 noeuds.
"""
import asyncio
import tempfile

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.integration


class _Primary:
    status = 200  # GET {PATRONI_URL}/primary -> 200 : ce noeud est le leader


def _user_columns(engine):
    return {c["name"] for c in inspect(engine).get_columns("users")}


def test_node_promoted_after_boot_applies_pending_migrations(monkeypatch, client):
    """Base d'avant la migration (backend demarre en replica, rien migre), puis
    Patroni annonce ce noeud leader -> la colonne manquante est creee."""
    from backend.app import leader_election as le
    from backend.app import main as main_mod
    from backend.app.database import Base

    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db", prefix="alarm-inv152-")
    f.close()
    engine = create_engine(f"sqlite:///{f.name}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        if "is_test" in _user_columns(engine):
            conn.execute(text("ALTER TABLE users DROP COLUMN is_test"))
    assert "is_test" not in _user_columns(engine)

    monkeypatch.setattr(le.urllib.request, "urlopen", lambda req, timeout=None: _Primary())
    le.is_leader.clear()

    async def promote():
        task = asyncio.create_task(le.leader_election_loop(
            "postgresql://patroni/alarm", on_promoted=lambda: main_mod._init_db_schema(engine)))
        for _ in range(50):
            await asyncio.sleep(0.1)
            if "is_test" in _user_columns(engine):
                break
        task.cancel()

    try:
        asyncio.run(promote())
    finally:
        le.is_leader.set()  # tier 2 = SQLite : le noeud est toujours leader

    assert "is_test" in _user_columns(engine), (
        "Un noeud devenu leader doit jouer les migrations en attente (INV-152)"
    )
