"""Fixtures pour les tests de configuration prod (INV-076 + futurs).

Strategie : forcer ENABLE_TEST_ENDPOINTS=false AVANT l'import de backend.app,
puis lancer un TestClient FastAPI sur SQLite temp. Comme l'import de
`backend.app.api.test_api` cache la valeur de `ENABLE_TEST_ENDPOINTS` au
moment du `import`, ce dossier DOIT etre lance dans un process pytest separe
des tests/integration/ (qui forcent =true). En CI : 2 jobs distincts.

Localement : `pytest tests/prod_config -p no:randomly` (ne PAS mixer avec
tests/integration dans le meme run).
"""
import os
import tempfile

# --- Setup env AVANT tout import de backend.app ---
# Cle de INV-076 : ENABLE_TEST_ENDPOINTS doit etre lu = false par le module
# test_api.py au moment de son import → tous les endpoints /api/test/* doivent
# retourner 404 via le guard _require_test_endpoints().
os.environ["ENABLE_TEST_ENDPOINTS"] = "false"

# DB temp SQLite pour eviter d'utiliser une DB partagee.
_f = tempfile.NamedTemporaryFile(delete=False, suffix=".db", prefix="alarm-prod-cfg-")
_f.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_f.name}")
os.environ.setdefault("SECRET_KEY", "prod-config-test-secret-not-for-prod")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    """TestClient FastAPI avec lifespan complet, lance avec ENABLE_TEST_ENDPOINTS=false."""
    from backend.app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_token(client):
    """JWT admin pour test des endpoints qui requierent auth.

    INV-076 : on veut prouver que meme un admin authentifie ne peut pas appeler
    /api/test/* en prod (le guard 404 est avant l'auth)."""
    r = client.post("/api/auth/login", json={"name": "admin", "password": "admin123"})
    assert r.status_code == 200, f"login admin failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}
