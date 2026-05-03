"""
Fixtures pour les tests d'integration tier 2.

Strategie : FastAPI TestClient contre SQLite temp (un fichier par session pytest).
- Pas besoin de cluster Patroni / Docker compose (rapide, isole).
- Les seed users (admin, user1, user2) sont crees au demarrage du lifespan.
- Pour CI, DATABASE_URL peut etre overridee (ex: postgres service container).

Important : DATABASE_URL DOIT etre defini AVANT l'import de backend.app,
car backend/app/database.py:8-15 cree l'engine au moment de l'import.
"""
import os
import tempfile

# --- Setup env AVANT tout import de backend.app ---
_TEST_DB_FILE = os.environ.get("TEST_DB_FILE")
if not _TEST_DB_FILE:
    _f = tempfile.NamedTemporaryFile(delete=False, suffix=".db", prefix="alarm-int-")
    _f.close()
    _TEST_DB_FILE = _f.name
    os.environ["TEST_DB_FILE"] = _TEST_DB_FILE

os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_DB_FILE}")
os.environ.setdefault("SECRET_KEY", "integration-test-secret-not-for-prod")
# /api/test/* endpoints disponibles en tier 2 (utilises par les tests E2E,
# notamment connected-users-detailed pour le chantier #21 failover bloquant).
# En prod, ENABLE_TEST_ENDPOINTS=false => 404 (cf INV-076).
os.environ.setdefault("ENABLE_TEST_ENDPOINTS", "true")
# Empeche les background tasks (escalation/watchdog) de spammer pendant les tests :
# elles tournent quand meme via lifespan mais leur tick est de l'ordre de la minute.

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    """TestClient FastAPI avec lifespan complet (tables crees + users seeded)."""
    # Import differe : garantit que DATABASE_URL est lu apres notre setenv.
    from backend.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_token(client):
    """JWT admin (admin/admin123 seede par main.py:38-71)."""
    r = client.post("/api/auth/login", json={"name": "admin", "password": "admin123"})
    assert r.status_code == 200, f"login admin failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}
