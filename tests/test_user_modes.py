"""
Tests RED — Modes astreinte/veille (is_oncall dans TokenResponse).

Ces tests DOIVENT ECHOUER sur le code actuel (phase RED).
Ils passeront apres implementation (phase GREEN).

Prerequis :
- Backend actif (cluster 3 noeuds)
- Chaine d'escalade : user1 (pos 1), user2 (pos 2), admin (pos 3)
"""

import os
import requests
import pytest

_ALL_BACKEND_URLS = ["http://localhost:8000", "http://localhost:8001", "http://localhost:8002"]


def _find_primary_url():
    for url in _ALL_BACKEND_URLS:
        try:
            r = requests.get(f"{url}/health", timeout=2)
            if r.status_code == 200 and r.json().get("role") == "primary":
                return url
        except Exception:
            continue
    return os.getenv("BACKEND_URL", "http://localhost:8000")


BASE_URL = os.getenv("BACKEND_URL", None) or _find_primary_url()
API = f"{BASE_URL}/api"


@pytest.fixture(autouse=True, scope="function")
def _ensure_primary_api():
    global BASE_URL, API
    new_url = _find_primary_url()
    if new_url != BASE_URL:
        BASE_URL = new_url
        API = f"{BASE_URL}/api"


ADMIN_NAME = "admin"
ADMIN_PASSWORD = "admin123"
USER1_NAME = "user1"
USER1_PASSWORD = "user123"
USER2_NAME = "user2"
USER2_PASSWORD = "user123"


def _admin_headers():
    r = requests.post(f"{API}/auth/login", json={"name": ADMIN_NAME, "password": ADMIN_PASSWORD})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestIsOncall:
    """Le champ is_oncall dans la reponse de login indique si l'utilisateur
    est en position 1 de la chaine d'escalade (astreinte)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """S'assurer que la chaine d'escalade est dans l'etat par defaut :
        user1 (pos 1), user2 (pos 2), admin (pos 3)."""
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        uid1 = next(u["id"] for u in users if u["name"] == USER1_NAME)
        uid2 = next(u["id"] for u in users if u["name"] == USER2_NAME)
        uid_admin = next(u["id"] for u in users if u["name"] == ADMIN_NAME)
        requests.post(f"{API}/config/escalation/bulk", json={
            "user_ids": [uid1, uid2, uid_admin],
        }, headers=admin_h)
        yield
        # Restaurer apres chaque test
        requests.post(f"{API}/config/escalation/bulk", json={
            "user_ids": [uid1, uid2, uid_admin],
        }, headers=admin_h)

    def test_login_returns_is_oncall(self):
        """Login user1 (position 1) → is_oncall: true."""
        r = requests.post(f"{API}/auth/login", json={
            "name": USER1_NAME, "password": USER1_PASSWORD,
        })
        assert r.status_code == 200
        data = r.json()
        assert "is_oncall" in data, (
            "La reponse de login doit contenir le champ 'is_oncall'"
        )
        assert data["is_oncall"] is True, (
            f"user1 (position 1) devrait etre is_oncall=true, got {data['is_oncall']}"
        )

    def test_login_returns_not_oncall(self):
        """Login user2 (position 2) → is_oncall: false."""
        r = requests.post(f"{API}/auth/login", json={
            "name": USER2_NAME, "password": USER2_PASSWORD,
        })
        assert r.status_code == 200
        data = r.json()
        assert "is_oncall" in data, (
            "La reponse de login doit contenir le champ 'is_oncall'"
        )
        assert data["is_oncall"] is False, (
            f"user2 (position 2) devrait etre is_oncall=false, got {data['is_oncall']}"
        )

    def test_oncall_changes_when_chain_updated(self):
        """Modifier la chaine d'escalade change le is_oncall au prochain login."""
        admin_h = _admin_headers()

        # Recuperer les user ids
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        uid1 = next(u["id"] for u in users if u["name"] == USER1_NAME)
        uid2 = next(u["id"] for u in users if u["name"] == USER2_NAME)
        uid_admin = next(u["id"] for u in users if u["name"] == ADMIN_NAME)

        # Sauvegarder la chaine originale
        original_chain = requests.get(f"{API}/config/escalation", headers=admin_h).json()
        original_ids = [e["user_id"] for e in sorted(original_chain, key=lambda x: x["position"])]

        try:
            # Mettre user2 en position 1
            r = requests.post(f"{API}/config/escalation/bulk", json={
                "user_ids": [uid2, uid1, uid_admin],
            }, headers=admin_h)
            assert r.status_code == 200

            # user2 devrait maintenant etre is_oncall=true
            r2 = requests.post(f"{API}/auth/login", json={
                "name": USER2_NAME, "password": USER2_PASSWORD,
            })
            assert r2.json().get("is_oncall") is True, (
                "user2 en position 1 devrait etre is_oncall=true"
            )

            # user1 ne devrait plus etre is_oncall
            r1 = requests.post(f"{API}/auth/login", json={
                "name": USER1_NAME, "password": USER1_PASSWORD,
            })
            assert r1.json().get("is_oncall") is False, (
                "user1 en position 2 devrait etre is_oncall=false"
            )
        finally:
            # Restaurer la chaine originale
            requests.post(f"{API}/config/escalation/bulk", json={
                "user_ids": original_ids,
            }, headers=admin_h)
