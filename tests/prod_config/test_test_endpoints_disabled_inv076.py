"""INV-076 [C] : en prod (ENABLE_TEST_ENDPOINTS=false), tous les endpoints
`/api/test/*` doivent retourner 404 — pas 200, pas 401, pas 403.

Pourquoi c'est critique : les endpoints `/api/test/*` exposent des actions
dangereuses (`reset` vide les alarmes, `advance-clock` avance l'horloge
globale, `send-alarm` cree des alarmes arbitraires, `simulate-watchdog-failure`
marque les users offline). Si quelqu'un les laisse ouverts en prod par erreur
(env var oubliee, refactor du gating retire), un attaquant pourrait :
- vider les alarmes pour rendre l'oncall inopérant
- créer une alarme bidon pour épuiser l'opérateur
- avancer l'horloge pour casser les TTL JWT / escalades

Le test verifie le routing FastAPI complet (le guard `_require_test_endpoints`
au debut de chaque handler ferme bien avec 404 quand ENABLE_TEST_ENDPOINTS=false).

Conftest dedie (`tests/prod_config/conftest.py`) force la variable a `false`
AVANT l'import de `backend.app.api.test_api` qui cache la valeur au module-level.

Ne PAS mixer le run de ce dossier avec `tests/integration/` qui force `=true`
(conflit de cache module). En CI : 2 jobs separes (pr.yml: prod_config_check).
"""
import pytest

pytestmark = pytest.mark.integration  # tier 2 (TestClient + SQLite), comme integration/


# Liste explicite des endpoints les plus critiques de `/api/test/*` a verrouiller.
# Verifies via `grep '@router\.' backend/app/api/test_api.py` (21 endpoints au total
# au 2026-05-29). On en couvre 6 representatifs : 2 actions destructrices
# (reset, send-alarm), 2 manipulations runtime (advance-clock, simulate-watchdog),
# 1 lecture (status), 1 escalation (trigger-escalation).
#
# Strategie : tester (method, path, body) en parametre paramétré. Si un mutant
# retire `_require_test_endpoints()` d'UN seul endpoint, le test correspondant
# echoue. Couverture mutation-aware [C] : 6 tests = 6 mutants tues.
_TEST_API_ENDPOINTS = [
    ("POST", "/api/test/reset", None),
    ("POST", "/api/test/send-alarm", None),
    ("POST", "/api/test/advance-clock", {"minutes": 1}),
    ("POST", "/api/test/simulate-watchdog-failure", None),
    ("GET", "/api/test/status", None),
    ("POST", "/api/test/trigger-escalation", None),
]


@pytest.mark.parametrize("method,path,params", _TEST_API_ENDPOINTS)
def test_test_endpoint_returns_404_when_disabled(client, method, path, params):
    """INV-076 [C] : tout `/api/test/*` doit retourner 404 quand
    ENABLE_TEST_ENDPOINTS=false (env de prod).

    Si ce test echoue → le guard `_require_test_endpoints()` a ete retire
    de l'endpoint correspondant, OU le mecanisme de chargement de la variable
    a regresse. C'est un VECTEUR D'ATTAQUE direct en prod (cf docstring module).
    """
    if method == "GET":
        r = client.get(path, params=params or {})
    elif method == "POST":
        r = client.post(path, params=params or {})
    else:  # pragma: no cover
        raise ValueError(f"method non geree: {method}")

    assert r.status_code == 404, (
        f"INV-076 [C] : {method} {path} doit renvoyer 404 quand "
        f"ENABLE_TEST_ENDPOINTS=false. Got {r.status_code} : {r.text[:200]}. "
        f"Si 200 : le guard `_require_test_endpoints()` est manquant sur ce "
        f"handler → VULNERABILITE PROD (endpoint dangereux exposé). "
        f"Si 401/403 : guard auth applique avant guard `_require`, l'attaquant "
        f"avec credentials passerait."
    )


def test_test_endpoint_returns_404_even_with_admin_auth(client, admin_headers):
    """INV-076 [C] defense en profondeur : meme avec un JWT admin valide,
    `/api/test/reset` doit renvoyer 404 quand ENABLE_TEST_ENDPOINTS=false.

    Verifie que le guard `_require_test_endpoints()` est appele AVANT
    `Depends(get_current_admin)` (sinon un admin authentifie aurait acces).

    Vecteur d'attaque verrouille : compromis credentials admin (vol JWT,
    leak password) ne doit PAS suffire a appeler `/api/test/reset` en prod.
    Le drapeau ENABLE_TEST_ENDPOINTS est la barriere ultime.
    """
    r = client.post("/api/test/reset", headers=admin_headers)
    assert r.status_code == 404, (
        f"INV-076 [C] : POST /api/test/reset avec JWT admin valide doit AUSSI "
        f"renvoyer 404 quand ENABLE_TEST_ENDPOINTS=false. Got {r.status_code}. "
        f"Si 200 : `_require_test_endpoints` est apres `Depends(get_current_admin)` "
        f"dans la chaine FastAPI → un admin compromis peut vider les alarmes prod."
    )


def test_non_test_endpoints_still_work_when_test_disabled(client, admin_headers):
    """INV-076 (anti-faux-positif) : le drapeau ENABLE_TEST_ENDPOINTS=false ne
    doit casser QUE les `/api/test/*`. Les endpoints metier (auth, alarms,
    config) doivent rester fonctionnels.

    Tue un mutant "global kill switch" qui ferait retourner 404 a TOUT le
    backend quand ENABLE_TEST_ENDPOINTS=false. Sans ce test, un mutant qui
    plante le backend entier passerait inapercu (les 7 autres tests verifient
    juste l'absence d'acces aux test endpoints, pas la presence du reste).
    """
    # /api/config/escalation est public (sans auth) ET production
    r = client.get("/api/config/escalation")
    assert r.status_code == 200, (
        f"INV-076 anti-faux-positif : GET /api/config/escalation doit rester "
        f"accessible meme avec ENABLE_TEST_ENDPOINTS=false. Got {r.status_code}. "
        f"Si 404 : la desactivation des test endpoints a casse la prod (mutant "
        f"'kill switch global')."
    )

    # /api/cluster expose un truc, doit repondre (200 ou 503 selon Patroni dispo,
    # mais pas 404 — la route doit exister)
    r = client.get("/api/cluster")
    assert r.status_code != 404, (
        f"INV-076 anti-faux-positif : GET /api/cluster doit exister "
        f"(200 ou 503 acceptable, pas 404). Got {r.status_code}."
    )
