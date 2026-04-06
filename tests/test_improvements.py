"""
Tests RED pour les 7 améliorations Alarm 2.0.

Ces tests DOIVENT ÉCHOUER sur le code actuel (phase RED).
Ils passeront après implémentation (phase GREEN).

Lancés contre un backend live :
  python -m pytest tests/test_improvements.py -v

Prérequis :
- Backend actif sur http://localhost:8000
"""

import os
import time
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
    """Re-detecte le primary avant chaque test, au cas ou un failover a eu lieu."""
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


def _login(name, password):
    """Helper : login et retourne (token, headers, user_id)."""
    r = requests.post(f"{API}/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"Login failed for {name}: {r.text}"
    data = r.json()
    token = data["access_token"]
    return token, {"Authorization": f"Bearer {token}"}, data["user"]["id"]


def _admin_headers():
    _, headers, _ = _login(ADMIN_NAME, ADMIN_PASSWORD)
    return headers


def _user1_headers():
    _, headers, uid = _login(USER1_NAME, USER1_PASSWORD)
    return headers, uid


# ═══════════════════════════════════════════════════════════════════════════════
# 1. SÉCURISATION DES ENDPOINTS (AUTH OBLIGATOIRE + RÔLES)
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndpointAuth:
    """Les endpoints sensibles doivent exiger un token Bearer.
    Actuellement ils sont ouverts → ces tests échouent (RED)."""

    # ── Endpoints qui doivent exiger une authentification ──

    def test_send_alarm_requires_auth(self):
        """POST /alarms/send sans token → 401."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Test", "message": "msg", "severity": "critical",
        })
        assert r.status_code == 401, (
            f"POST /alarms/send devrait exiger auth, got {r.status_code}"
        )

    def test_resolve_alarm_requires_auth(self):
        """POST /alarms/{id}/resolve sans token → 401."""
        # Créer une alarme d'abord (avec auth)
        headers = _admin_headers()
        r = requests.post(f"{API}/alarms/send", json={
            "title": "To resolve", "message": "m", "severity": "critical",
        }, headers=headers)
        alarm_id = r.json()["id"]

        # Tenter de résoudre sans auth
        r = requests.post(f"{API}/alarms/{alarm_id}/resolve")
        assert r.status_code == 401, (
            f"POST /alarms/resolve devrait exiger auth, got {r.status_code}"
        )

        # Cleanup
        requests.post(f"{API}/alarms/{alarm_id}/resolve", headers=headers)

    def test_list_users_requires_auth(self):
        """GET /users/ sans token → 401."""
        r = requests.get(f"{API}/users/")
        assert r.status_code == 401, (
            f"GET /users/ devrait exiger auth, got {r.status_code}"
        )

    def test_list_all_alarms_requires_auth(self):
        """GET /alarms/ sans token → 401."""
        r = requests.get(f"{API}/alarms/")
        assert r.status_code == 401, (
            f"GET /alarms/ devrait exiger auth, got {r.status_code}"
        )

    def test_active_alarms_requires_auth(self):
        """GET /alarms/active sans token → 401."""
        r = requests.get(f"{API}/alarms/active")
        assert r.status_code == 401, (
            f"GET /alarms/active devrait exiger auth, got {r.status_code}"
        )

    def test_list_devices_requires_auth(self):
        """GET /devices/ sans token → 401."""
        r = requests.get(f"{API}/devices/")
        assert r.status_code == 401, (
            f"GET /devices/ devrait exiger auth, got {r.status_code}"
        )

    # ── Endpoints admin-only ──

    def test_delete_user_requires_admin(self):
        """DELETE /users/{id} par un non-admin → 403."""
        headers, _ = _user1_headers()
        # Trouver user2
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        user2 = next(u for u in users if u["name"] == "user2")

        r = requests.delete(f"{API}/users/{user2['id']}", headers=headers)
        assert r.status_code == 403, (
            f"DELETE /users/ par non-admin devrait retourner 403, got {r.status_code}"
        )

    def test_update_user_requires_admin(self):
        """PATCH /users/{id} par un non-admin → 403."""
        headers, uid = _user1_headers()

        # Tenter de modifier un autre utilisateur
        admin_h = _admin_headers()
        users = requests.get(f"{API}/users/", headers=admin_h).json()
        user2 = next(u for u in users if u["name"] == "user2")

        r = requests.patch(
            f"{API}/users/{user2['id']}",
            json={"phone_number": "+33600000000"},
            headers=headers,
        )
        assert r.status_code == 403, (
            f"PATCH /users/ d'un autre user par non-admin devrait retourner 403, got {r.status_code}"
        )

    def test_escalation_config_requires_admin(self):
        """POST /config/escalation par un non-admin → 403."""
        headers, _ = _user1_headers()
        r = requests.post(f"{API}/config/escalation", json={
            "position": 99, "user_id": 1, "delay_minutes": 5.0,
        }, headers=headers)
        assert r.status_code == 403, (
            f"POST /config/escalation par non-admin devrait retourner 403, got {r.status_code}"
        )

    def test_escalation_bulk_requires_admin(self):
        """POST /config/escalation/bulk par un non-admin → 403."""
        headers, _ = _user1_headers()
        r = requests.post(f"{API}/config/escalation/bulk", json={
            "user_ids": [1, 2, 3],
        }, headers=headers)
        assert r.status_code == 403, (
            f"POST /config/escalation/bulk par non-admin devrait retourner 403, got {r.status_code}"
        )

    def test_system_config_write_requires_admin(self):
        """POST /config/system par un non-admin → 403."""
        headers, _ = _user1_headers()
        r = requests.post(f"{API}/config/system", json={
            "key": "test_key", "value": "test_value",
        }, headers=headers)
        assert r.status_code == 403, (
            f"POST /config/system par non-admin devrait retourner 403, got {r.status_code}"
        )

    def test_reset_alarms_requires_admin(self):
        """POST /alarms/reset par un non-admin → 403."""
        headers, _ = _user1_headers()
        r = requests.post(f"{API}/alarms/reset", headers=headers)
        assert r.status_code == 403, (
            f"POST /alarms/reset par non-admin devrait retourner 403, got {r.status_code}"
        )

    # ── Vérifier que l'auth fonctionne toujours pour les utilisateurs légitimes ──

    def test_send_alarm_works_with_auth(self):
        """POST /alarms/send avec un token valide → 200."""
        headers = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=headers)
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Auth Test", "message": "msg", "severity": "critical",
        }, headers=headers)
        assert r.status_code == 200
        # Cleanup
        requests.post(f"{API}/alarms/{r.json()['id']}/resolve", headers=headers)

    def test_admin_can_delete_user(self):
        """DELETE /users/{id} par un admin → 200."""
        headers = _admin_headers()
        # Créer un user temporaire
        r = requests.post(f"{API}/auth/register", json={
            "name": "tempdelete", "password": "test123",
        })
        uid = r.json()["id"]

        r = requests.delete(f"{API}/users/{uid}", headers=headers)
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ENDPOINTS DE TEST CONDITIONNÉS À ENABLE_TEST_ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndpointsToggle:
    """Les endpoints /api/test/* doivent être désactivables en production.

    NOTE : Ce test vérifie que le mécanisme existe. En production
    (ENABLE_TEST_ENDPOINTS absent ou false), ces endpoints doivent retourner 404.
    Comme les tests tournent contre un backend de dev (ENABLE_TEST_ENDPOINTS=true),
    on teste ici que le backend expose un indicateur dans /health ou /api/test/status.
    """

    def test_test_endpoints_expose_enabled_flag(self):
        """GET /health doit indiquer si les endpoints de test sont activés."""
        r = requests.get(f"{BASE_URL}/health")
        assert r.status_code == 200
        data = r.json()
        assert "test_endpoints_enabled" in data, (
            "/health devrait indiquer si les endpoints de test sont activés"
        )

    def test_test_endpoints_flag_matches_env(self):
        """Le flag test_endpoints_enabled doit refléter la variable d'env."""
        r = requests.get(f"{BASE_URL}/health")
        data = r.json()
        # En environnement de test, le flag doit être True
        assert data.get("test_endpoints_enabled") is True, (
            "En environnement de test, test_endpoints_enabled devrait être True"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. TABLE DE LIAISON alarm_notifications (remplacement du CSV)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlarmNotificationsTable:
    """Le champ CSV notified_user_ids doit être remplacé par une table de liaison.
    Les réponses API doivent toujours contenir notified_user_ids et notified_user_names."""

    @pytest.fixture(autouse=True)
    def setup(self):
        admin_h = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=admin_h)
        requests.post(f"{API}/test/reset")
        self.admin_headers = admin_h
        self.token1, self.headers1, self.uid1 = _login(USER1_NAME, USER1_PASSWORD)
        self.token2, self.headers2, self.uid2 = _login(USER2_NAME, USER2_PASSWORD)
        yield
        requests.post(f"{API}/alarms/reset", headers=admin_h)

    def test_alarm_response_has_notified_fields(self):
        """La réponse d'alarme doit contenir notified_user_ids (list[int]) et notified_user_names (list[str])."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Notif test", "message": "m", "severity": "critical",
        }, headers=self.admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert "notified_user_ids" in data
        assert "notified_user_names" in data
        assert isinstance(data["notified_user_ids"], list)
        assert isinstance(data["notified_user_names"], list)
        assert len(data["notified_user_ids"]) >= 1

    def test_escalation_adds_to_notified(self):
        """Après escalade, le nouvel utilisateur est ajouté aux notifiés."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Escal notif", "message": "m", "severity": "critical",
        }, headers=self.admin_headers)
        alarm_id = r.json()["id"]
        initial_notified = r.json()["notified_user_ids"]

        # Forcer l'escalade
        requests.post(f"{API}/test/trigger-escalation")

        r = requests.get(f"{API}/alarms/active", headers=self.admin_headers)
        alarm = next(a for a in r.json() if a["id"] == alarm_id)
        assert len(alarm["notified_user_ids"]) > len(initial_notified), (
            "L'escalade doit ajouter un utilisateur aux notifiés"
        )

    def test_notified_user_names_resolved(self):
        """notified_user_names doit contenir les vrais noms des utilisateurs."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Names test", "message": "m", "severity": "critical",
        }, headers=self.admin_headers)
        data = r.json()
        assert len(data["notified_user_names"]) >= 1
        # Les noms doivent être des strings non vides, pas "?"
        for name in data["notified_user_names"]:
            assert isinstance(name, str) and len(name) > 0 and name != "?"

    def test_mine_filters_by_notified(self):
        """GET /alarms/mine ne retourne que les alarmes où l'utilisateur est notifié."""
        # Envoyer une alarme assignée à user1
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Mine filter", "message": "m", "severity": "critical",
            "assigned_user_id": self.uid1,
        }, headers=self.admin_headers)
        assert r.status_code == 200

        # user1 doit la voir
        r = requests.get(f"{API}/alarms/mine", headers=self.headers1)
        assert len(r.json()) == 1

        # user2 ne doit PAS la voir (pas encore escaladé)
        r = requests.get(f"{API}/alarms/mine", headers=self.headers2)
        assert len(r.json()) == 0, (
            "user2 ne devrait pas voir une alarme assignée uniquement à user1"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. CORS RESTREINT
# ═══════════════════════════════════════════════════════════════════════════════

class TestCorsRestriction:
    """CORS ne doit plus accepter toutes les origines."""

    def test_cors_rejects_random_origin(self):
        """Une requête preflight depuis une origine inconnue doit être refusée."""
        r = requests.options(f"{API}/auth/login", headers={
            "Origin": "https://evil-site.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        })
        # Le serveur ne doit PAS renvoyer Access-Control-Allow-Origin: https://evil-site.com
        acao = r.headers.get("Access-Control-Allow-Origin", "")
        assert acao != "*", "CORS ne devrait plus accepter '*'"
        assert acao != "https://evil-site.com", (
            "CORS ne devrait pas autoriser une origine inconnue"
        )

    def test_cors_allows_legitimate_origin(self):
        """Une requête preflight depuis une origine légitime doit être acceptée."""
        # L'origine légitime est définie dans ALLOWED_ORIGINS env var
        # En dev, c'est typiquement localhost
        r = requests.options(f"{API}/auth/login", headers={
            "Origin": f"{BASE_URL}",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        })
        acao = r.headers.get("Access-Control-Allow-Origin", "")
        assert acao in [BASE_URL, "*"] or r.status_code == 200, (
            f"CORS devrait accepter l'origine locale {BASE_URL}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. SECRET_KEY EXTERNALISÉ
# ═══════════════════════════════════════════════════════════════════════════════

class TestSecretKey:
    """Le SECRET_KEY JWT ne doit pas être la valeur par défaut en production."""

    def test_health_warns_default_secret(self):
        """Si le SECRET_KEY est la valeur par défaut, /health doit le signaler."""
        r = requests.get(f"{BASE_URL}/health")
        data = r.json()
        # Le health check devrait inclure un warning si la clé par défaut est utilisée
        assert "secret_key_default" in data, (
            "/health devrait signaler si le SECRET_KEY est la valeur par défaut"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. RATE LIMITING SUR LOGIN
# ═══════════════════════════════════════════════════════════════════════════════

class TestRateLimiting:
    """Le login doit être protégé contre le brute-force."""

    def test_login_rate_limited_after_many_failures(self):
        """Après 10 tentatives échouées, le login doit retourner 429."""
        # Envoyer 10 tentatives échouées rapides
        for i in range(10):
            requests.post(f"{API}/auth/login", json={
                "name": "bruteforce", "password": f"wrong{i}",
            })

        # La 11ème doit être rate-limited
        r = requests.post(f"{API}/auth/login", json={
            "name": "bruteforce", "password": "wrong10",
        })
        assert r.status_code == 429, (
            f"Après 10 échecs, le login devrait être rate-limited (429), got {r.status_code}"
        )

    def test_legitimate_login_still_works_after_rate_limit(self):
        """Un login légitime avec un autre compte ne doit pas être bloqué."""
        # Cet appel doit toujours fonctionner même après le brute-force ci-dessus
        # (rate limit basé sur IP+username, pas juste IP globale)
        r = requests.post(f"{API}/auth/login", json={
            "name": ADMIN_NAME, "password": ADMIN_PASSWORD,
        })
        assert r.status_code == 200, (
            "Un login légitime ne devrait pas être bloqué par le rate limit d'un autre user"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 7. VALIDATION DES ENTRÉES (SCHEMAS PYDANTIC)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInputValidation:
    """Les entrées doivent être validées (enum severity, longueur max, etc.)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.headers = _admin_headers()
        requests.post(f"{API}/alarms/reset", headers=self.headers)
        yield
        requests.post(f"{API}/alarms/reset", headers=self.headers)

    def test_alarm_rejects_invalid_severity(self):
        """Une alarme avec une sévérité invalide doit être refusée (422)."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "Test", "message": "m", "severity": "banana",
        }, headers=self.headers)
        assert r.status_code == 422, (
            f"severity='banana' devrait être refusée, got {r.status_code}"
        )

    def test_alarm_rejects_too_long_title(self):
        """Un titre d'alarme trop long (>200 chars) doit être refusé."""
        r = requests.post(f"{API}/alarms/send", json={
            "title": "A" * 201, "message": "m", "severity": "critical",
        }, headers=self.headers)
        assert r.status_code == 422, (
            f"Un titre de 201 chars devrait être refusé, got {r.status_code}"
        )

    def test_alarm_accepts_valid_severities(self):
        """Les sévérités valides doivent être acceptées."""
        for sev in ["low", "medium", "high", "critical"]:
            r = requests.post(f"{API}/alarms/send", json={
                "title": f"Test {sev}", "message": "m", "severity": sev,
            }, headers=self.headers)
            assert r.status_code in [200, 409], (
                f"severity='{sev}' devrait être acceptée, got {r.status_code}"
            )
            if r.status_code == 200:
                requests.post(f"{API}/alarms/{r.json()['id']}/resolve",
                              headers=self.headers)
