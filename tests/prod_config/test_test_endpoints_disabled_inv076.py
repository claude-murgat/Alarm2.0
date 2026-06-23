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


def _discover_test_api_routes():
    """Introspecte l'app FastAPI pour decouvrir TOUTES les routes `/api/test/*`.

    Renvoie `(routes, query_params_by_path)` ou :
    - `routes` : liste de tuples `(method, path)` triee, deduplique HEAD/OPTIONS ;
    - `query_params_by_path` : dict `path -> [noms des query params declares]`,
      utilise pour forger une requete qui PASSE la validation Pydantic et
      atteint le guard `_require_test_endpoints()` en tete de handler.

    Pourquoi introspection plutot qu'une liste hardcodee : si un dev ajoute
    `@router.get("/api/test/nouveau-endpoint")` mais OUBLIE le
    `_require_test_endpoints()` en tete du handler, l'introspection le couvre
    AUTOMATIQUEMENT. La liste hardcodee laissait 15 handlers (sur 21) muets,
    dont des endpoints destructeurs (`reset-sms-queue`, `configure-smtp`,
    `insert-sms`, `insert-call`, `reset-call-queue`, `reset-fcm`). Un mutant
    qui retire le guard de l'un de ces 15 ne tuait personne -> trou de
    couverture mutation-aware.

    Pourquoi capturer aussi les query params : un endpoint avec un query param
    REQUIS (ex: `revoke-user-refresh-tokens` -> `user_id: int = Query(...)`)
    renvoie 422 AVANT d'entrer dans le handler si on ne fournit pas le param.
    Or 422 ne prouve RIEN pour INV-076 : l'endpoint renvoie 422 que
    ENABLE_TEST_ENDPOINTS soit true OU false (la validation court-circuite le
    guard dans les deux cas). Pour que le 404 (flag=false) soit distinguable
    du 200 (flag=true), il faut d'abord franchir la validation -> on fournit
    une valeur factice a chaque query param declare (cf test ci-dessous).

    Cf issue #140 (hardening mutation-proof INV-076).
    """
    from backend.app.main import app
    routes = []
    query_params_by_path = {}
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        path = getattr(route, "path", "")
        if not path.startswith("/api/test/"):
            continue
        dependant = getattr(route, "dependant", None)
        query_params_by_path[path] = [
            qp.name for qp in getattr(dependant, "query_params", [])
        ]
        for method in methods - {"HEAD", "OPTIONS"}:
            routes.append((method, path))
    return sorted(routes), query_params_by_path


_TEST_API_ROUTES, _QUERY_PARAMS_BY_PATH = _discover_test_api_routes()


def test_test_api_route_count_meta_at_least_21():
    """INV-076 meta-test : l'introspection FastAPI doit decouvrir AU MOINS
    21 routes `/api/test/*` (compte au 2026-05-29).

    Pourquoi : si l'introspection silencieusement renvoie 0 ou 5 routes (bug
    de signature `app.routes`, refactor mounting, version FastAPI change),
    le test parametre `test_test_endpoint_returns_404_when_disabled` passerait
    sur les routes effectivement decouvertes mais laisserait silencieusement
    les autres non testees. Ce meta-test attrape ce mode de defaillance.

    Si plus de routes sont ajoutees a `backend/app/api/test_api.py`, ce test
    continue de passer (assertion >=, pas ==). Si un endpoint est supprime
    legitimement et le total descend sous 21, mettre a jour ce seuil ICI ET
    confirmer dans la PR que la suppression est intentionnelle.
    """
    assert len(_TEST_API_ROUTES) >= 21, (
        f"INV-076 meta : introspection FastAPI a decouvert {len(_TEST_API_ROUTES)} "
        f"routes /api/test/* mais on en attend >= 21. Soit l'introspection est "
        f"cassee (verifier `_discover_test_api_routes` dans ce fichier), soit "
        f"des endpoints ont ete supprimes legitimement (mettre a jour le seuil "
        f"si intentionnel). Routes vues : {_TEST_API_ROUTES}"
    )


@pytest.mark.parametrize("method,path", _TEST_API_ROUTES)
def test_test_endpoint_returns_404_when_disabled(client, method, path):
    """INV-076 [C] : tout `/api/test/*` doit retourner 404 quand
    ENABLE_TEST_ENDPOINTS=false (env de prod).

    Si ce test echoue → le guard `_require_test_endpoints()` a ete retire
    de l'endpoint correspondant, OU le mecanisme de chargement de la variable
    a regresse. C'est un VECTEUR D'ATTAQUE direct en prod (cf docstring module).

    Couverture mutation-aware [C] : toutes les routes introspectees = autant
    de mutants tues (un par handler). Cf `_discover_test_api_routes` ci-dessus.
    """
    # Forger une requete qui FRANCHIT la validation Pydantic pour atteindre le
    # guard `_require_test_endpoints()` (1ere ligne de chaque handler) :
    #  - query params : valeur factice "1" pour CHAQUE param declare. Sinon un
    #    param requis (ex: revoke-user-refresh-tokens `user_id: int = Query(...)`)
    #    -> 422 avant le guard. "1" est coercible en int/float/bool/str, donc
    #    valide pour tous les types scalaires utilises par ces endpoints ;
    #    fournir une valeur a un param OPTIONNEL est inoffensif.
    #  - `json={}` : inoffensif pour les endpoints sans body, necessaire pour
    #    ceux qui declarent `payload: dict` (insert-sms, insert-call).
    # Le guard renvoyant 404 AVANT toute logiqe metier, ces valeurs factices
    # n'ont aucun effet de bord quand ENABLE_TEST_ENDPOINTS=false.
    params = {name: "1" for name in _QUERY_PARAMS_BY_PATH.get(path, [])}
    if method == "GET":
        r = client.get(path, params=params)
    elif method == "POST":
        r = client.post(path, params=params, json={})
    else:  # pragma: no cover
        raise ValueError(f"method non geree: {method}")

    assert r.status_code == 404, (
        f"INV-076 [C] : {method} {path} doit renvoyer 404 quand "
        f"ENABLE_TEST_ENDPOINTS=false. Got {r.status_code} : {r.text[:200]}. "
        f"Si 200 : le guard `_require_test_endpoints()` est manquant sur ce "
        f"handler → VULNERABILITE PROD (endpoint dangereux exposé). "
        f"Si 422 : la validation Pydantic a court-circuite le guard (un nouveau "
        f"param requis n'est pas couvert par `_QUERY_PARAMS_BY_PATH` / `json={{}}` "
        f"— adapter la generation de requete dans `_discover_test_api_routes`)."
    )


def test_enable_test_endpoints_default_is_false_when_env_var_absent():
    """INV-076 [C] : le defaut hardcode de `ENABLE_TEST_ENDPOINTS` dans
    `backend/app/api/test_api.py` doit etre `"false"` — pas `"true"`, `"1"`
    ni `"yes"`.

    Pourquoi un test dedie : la conftest force `os.environ['ENABLE_TEST_ENDPOINTS']
    = 'false'` AVANT l'import du module. Un mutant qui changerait le defaut de
    `os.getenv(..., 'false')` en `os.getenv(..., 'true')` survivrait — la
    valeur captee a l'import resterait `false` grace au conftest, masquant la
    mutation. En prod, l'env var est typiquement ABSENTE et le defaut prend
    le relais : un defaut a `true` exposerait tous les `/api/test/*`.

    Strategie : inspection statique de la source de `test_api.py` (regex sur
    la ligne `ENABLE_TEST_ENDPOINTS = os.getenv(...)`). Pas besoin de
    subprocess / reload — le AST source est la source de verite.

    Vecteur d'attaque tue : "dev a inverse le defaut pour tests locaux et a
    oublie de restorer". Le job CI `prod_config_check` echoue immediatement.
    """
    import inspect
    import re
    from backend.app.api import test_api

    src = inspect.getsource(test_api)
    match = re.search(
        r'ENABLE_TEST_ENDPOINTS\s*=\s*os\.getenv\(\s*["\']ENABLE_TEST_ENDPOINTS["\']\s*,\s*["\']([^"\']+)["\']\s*\)',
        src,
    )
    assert match, (
        "INV-076 meta : impossible de localiser le pattern "
        "`ENABLE_TEST_ENDPOINTS = os.getenv(\"ENABLE_TEST_ENDPOINTS\", \"...\")` "
        "dans backend/app/api/test_api.py. Le mecanisme de chargement a-t-il "
        "ete refactore (variable d'env -> SystemConfig, deplace ailleurs) ? "
        "Si oui, adapter ce test pour pointer vers la nouvelle source de verite."
    )
    default_literal = match.group(1).lower()
    assert default_literal not in ("true", "1", "yes"), (
        f"INV-076 [C] VULNERABILITE PROD : le defaut hardcode de "
        f"ENABLE_TEST_ENDPOINTS est '{default_literal}'. En prod sans env var "
        f"explicit (cas normal), ce defaut s'applique → tous les /api/test/* "
        f"seraient EXPOSES (endpoints destructeurs `reset`, `send-alarm`, "
        f"`advance-clock`, `configure-smtp`, `insert-sms`, `insert-call`...). "
        f"Restaurer 'false' dans backend/app/api/test_api.py."
    )


def test_test_endpoint_returns_404_even_with_admin_auth(client, admin_headers):
    """INV-076 [C] defense en profondeur : un JWT admin valide ne doit PAS
    bypass le 404 sur `/api/test/reset` quand ENABLE_TEST_ENDPOINTS=false.

    Ce que prouve ce test : le 404 est INCONDITIONNEL, il n'est pas
    contournable par presentation de credentials (ni admin, ni utilisateur).
    L'attaquant qui vole / leak un JWT admin valide tombe quand meme sur 404
    en prod — le drapeau ENABLE_TEST_ENDPOINTS est la barriere ultime,
    independante de l'auth.

    Ce que ce test NE prouve PAS : un ordre `_require_test_endpoints` AVANT
    `Depends(get_current_admin)`. La raison : `/api/test/reset` n'a PAS de
    `Depends(get_current_admin)` dans le code prod actuel (uniquement
    `Depends(get_db)` au 2026-05-29). Le test envoie un header Bearer pour
    documenter le scenario d'attaque (admin compromis) ; le header est ignore
    par le handler. Si un futur refactor ajoute `Depends(get_current_admin)`
    a `/api/test/reset`, ce test verrouillera mecaniquement l'ordre : un
    guard auth applique avant `_require_test_endpoints()` renverrait 401/403
    en plus du 404, et l'assertion ci-dessous attraperait ce changement de
    comportement.
    """
    r = client.post("/api/test/reset", headers=admin_headers)
    assert r.status_code == 404, (
        f"INV-076 [C] : POST /api/test/reset avec JWT admin valide doit AUSSI "
        f"renvoyer 404 quand ENABLE_TEST_ENDPOINTS=false. Got {r.status_code}. "
        f"Si 200 : `_require_test_endpoints` a ete retire OU place apres une "
        f"dependance auth qui passe -> un admin compromis peut vider les alarmes "
        f"prod. Si 401/403 : un guard auth a ete ajoute AVANT "
        f"`_require_test_endpoints()` -> l'attaquant sans creds verrait 401, "
        f"l'attaquant avec creds (cas ici) verrait 200 -> regression."
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
