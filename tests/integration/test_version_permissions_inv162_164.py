"""
Tier 2 integration : INV-162, INV-163, INV-164 (decisions du proprietaire du 2026-10-29).

- INV-162 : resoudre une alarme (POST /api/alarms/{id}/resolve) est reserve aux admins.
- INV-163 : GET /api/config/system (alert_email, chaine, etat modem...) exige un jeton d'admin.
- INV-164 : /health renvoie la version deployee (`version` = APP_VERSION = numero de release du
  fichier VERSION, sinon sha court ; `commit` = sha complet lu dans GIT_SHA ; "dev" sans les deux).
"""
import pytest

pytestmark = pytest.mark.integration

FULL_SHA = "2c5dc8571e0f5b3a9d4c6e7f8a9b0c1d2e3f4a5b"


def _headers(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def alarm_id(client, admin_headers):
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    sent = client.post("/api/alarms/send", headers=admin_headers, json={
        "site": "izeron", "title": "INV-162", "message": "resolution admin", "severity": "critical",
        "assigned_user_id": None,
    })
    assert sent.status_code == 200, sent.text
    yield sent.json()["id"]
    client.post("/api/test/reset", params={"peer": "false"})


def _status(client, admin_headers, alarm_id):
    return next(a["status"] for a in client.get("/api/alarms/", headers=admin_headers).json()
                if a["id"] == alarm_id)


# ── INV-162 ────────────────────────────────────────────────────────────────

def test_non_admin_cannot_resolve(client, admin_headers, alarm_id):
    """INV-162 : user1 (notifie, position 1) -> 403, l'alarme reste en cours."""
    r = client.post(f"/api/alarms/{alarm_id}/resolve", headers=_headers(client, "user1", "user123"))
    assert r.status_code == 403, f"Resolution par un non-admin doit renvoyer 403, got {r.status_code}: {r.text}"
    assert _status(client, admin_headers, alarm_id) == "active"


def test_admin_resolves(client, admin_headers, alarm_id):
    """INV-162 : un admin resout -> 200, statut resolved."""
    r = client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"
    assert _status(client, admin_headers, alarm_id) == "resolved"


# ── INV-163 ────────────────────────────────────────────────────────────────

PROBE = {"key": "alert_email", "value": "inv163@example.com"}


@pytest.fixture
def system_config(client, admin_headers):
    assert client.post("/api/config/system", headers=admin_headers, json=PROBE).status_code == 200


def test_system_config_refused_without_token(client, system_config):
    """INV-163 : sans jeton -> 401/403, aucune valeur renvoyee."""
    r = client.get("/api/config/system")
    assert r.status_code in (401, 403), f"GET /api/config/system sans jeton doit etre refuse, got {r.status_code}"
    assert PROBE["value"] not in r.text


def test_system_config_refused_for_non_admin(client, system_config):
    """INV-163 : jeton d'un non-admin -> 403, aucune valeur renvoyee."""
    r = client.get("/api/config/system", headers=_headers(client, "user1", "user123"))
    assert r.status_code == 403, f"GET /api/config/system par un non-admin doit renvoyer 403, got {r.status_code}"
    assert PROBE["value"] not in r.text


def test_system_config_for_admin(client, admin_headers, system_config):
    """INV-163 : un admin lit la config systeme (200, dict cle -> valeur)."""
    r = client.get("/api/config/system", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json().get(PROBE["key"]) == PROBE["value"]


# ── INV-164 ────────────────────────────────────────────────────────────────

def test_health_version_dev_without_git_sha(client, monkeypatch):
    """INV-164 : sans GIT_SHA ni APP_VERSION -> version = commit = "dev" (200 ou 503, le corps les porte)."""
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("APP_VERSION", raising=False)
    body = client.get("/health").json()
    assert body.get("version") == "dev", body
    assert body.get("commit") == "dev", body


def test_health_version_from_git_sha(client, monkeypatch):
    """INV-164 : GIT_SHA seul = sha complet -> version = 7 premiers caracteres, commit = sha complet."""
    monkeypatch.setenv("GIT_SHA", FULL_SHA)
    monkeypatch.delenv("APP_VERSION", raising=False)
    body = client.get("/health").json()
    assert body.get("version") == FULL_SHA[:7], body
    assert body.get("commit") == FULL_SHA, body


def test_health_version_is_release_number_from_app_version(client, monkeypatch):
    """INV-164 : APP_VERSION (fichier VERSION au build) -> version = numero de release, commit = sha."""
    monkeypatch.setenv("GIT_SHA", FULL_SHA)
    monkeypatch.setenv("APP_VERSION", "1.6.1")
    body = client.get("/health").json()
    assert body.get("version") == "1.6.1", body
    assert body.get("commit") == FULL_SHA, body
