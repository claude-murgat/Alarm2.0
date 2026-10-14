"""Tier 2 integration — INV-129 : etat reseau du modem remonte par la gateway,
expose a l'app Android et au web pour l'indicateur de signal 4G.

  - POST /internal/alarms/modem-status : X-Gateway-Key obligatoire (INV-065)
  - GET /api/system/modem-status : tout utilisateur connecte, barres calculees
  - sans rapport : etat "unknown" (gateway muette = on ne sait pas)
"""
import pytest

pytestmark = pytest.mark.integration

GATEWAY_KEY = "test-gateway-key-INV-129"
GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}
LTE_EDGE = {"registered": True, "tech": "LTE", "rsrp": -115.7, "csq": 15}


@pytest.fixture(autouse=True)
def _set_gateway_key(monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", GATEWAY_KEY)


@pytest.fixture(autouse=True)
def _clean_modem_status():
    from sqlalchemy import text
    from backend.app.database import SessionLocal

    def wipe():
        db = SessionLocal()
        try:
            db.execute(text("DELETE FROM system_config WHERE key = 'modem_status'"))
            db.commit()
        finally:
            db.close()
    wipe()
    yield
    wipe()


@pytest.fixture
def user_headers(client):
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_report_requires_gateway_key(client):
    assert client.post("/internal/alarms/modem-status", json=LTE_EDGE).status_code == 401


def test_read_requires_login(client):
    assert client.get("/api/system/modem-status").status_code in (401, 403)


def test_unknown_before_any_report(client, user_headers):
    r = client.get("/api/system/modem-status", headers=user_headers)
    assert r.status_code == 200 and r.json()["state"] == "unknown"


def test_report_then_read(client, user_headers):
    assert client.post("/internal/alarms/modem-status", json=LTE_EDGE,
                       headers=GATEWAY_HEADERS).status_code == 200
    body = client.get("/api/system/modem-status", headers=user_headers).json()
    assert (body["state"], body["bars"], body["tech"], body["rsrp"]) == ("ok", 1, "LTE", -115.7)


def test_no_network_report(client, user_headers):
    client.post("/internal/alarms/modem-status", headers=GATEWAY_HEADERS,
                json={"registered": False, "tech": None, "rsrp": None, "csq": 0})
    body = client.get("/api/system/modem-status", headers=user_headers).json()
    assert (body["state"], body["bars"]) == ("no_network", 0)
