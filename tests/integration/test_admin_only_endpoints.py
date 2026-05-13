"""
Tier 2 integration tests : INV-077 — endpoints admin-only renvoient 403 pour
les non-admins.

Couvre INV-077 (tests/INVARIANTS.md, section 7) :
  Les endpoints DELETE /api/users/{id}, POST /api/alarms/reset,
  POST /api/config/escalation, POST /api/config/escalation/bulk et
  POST /api/config/system exigent `current_user.is_admin = True`.
  Pour un user non-admin → **403 Forbidden**.

Pourquoi ce test :
  La regle existe deja dans le code (`Depends(get_current_admin)` sur chacun
  des handlers), mais aucun test ne la verrouille. Sans test, un refactor qui
  retirerait accidentellement la dependency passerait silencieusement — un
  user1 (non-admin) pourrait alors supprimer un user ou modifier la chaine.

  C'est exactement le scenario du backlog "INV-077 ⚠️ partiellement couvert"
  (INVARIANTS.md). Le test agit comme un garde-fou en regression.

Strategie du test :
  - Pour chacun des 5 endpoints, on envoie le MEME body en deux temps :
    1. Avec un token `user1` (non-admin) → attendu **exactement 403**.
    2. Avec un token `admin` → attendu **non-403** (peut etre 200, 404, 409
       ou 422 selon le payload, mais jamais 403).
  - Le body est choisi pour etre PYDANTIQUEMENT VALIDE (sinon FastAPI
    renvoie 422 avant meme d'evaluer la dependency d'auth, et le test ne
    prouverait rien sur la protection admin).
  - Le body est choisi pour qu'admin n'ait pas d'effet de bord destructif
    (id inexistants, payload invalide metier → 404/409/422 cote admin).

Budget P4 : 1 test parametre, 5 cases (1 par endpoint critique).
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def user1_headers(client):
    """JWT user1 (non-admin) — seed `main.py:45`."""
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    assert r.status_code == 200, f"login user1 failed: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _admin_user_id(client, admin_headers):
    r = client.get("/api/users/", headers=admin_headers)
    assert r.status_code == 200, r.text
    for u in r.json():
        if u["name"] == "admin":
            return u["id"]
    raise AssertionError("admin user not found in GET /api/users/")


# (method, path, body_factory(client, admin_headers) -> body|None, label)
# body_factory permet de calculer un payload qui depend de l'etat seed (ex: admin user_id).
_ENDPOINTS = [
    (
        "DELETE",
        "/api/users/999999",  # id inexistant : admin -> 404, jamais 403
        lambda c, h: None,
        "DELETE /api/users/{id}",
    ),
    (
        "POST",
        "/api/alarms/reset",
        lambda c, h: None,  # pas de body
        "POST /api/alarms/reset",
    ),
    (
        "POST",
        "/api/config/escalation",
        # user_id = admin (deja dans la chaine seed pos 3) + position libre 999
        # -> admin recoit 409 (INV-020 conflict user_id), jamais 403.
        # Payload pydantiquement valide (EscalationConfigCreate).
        lambda c, h: {"position": 999, "user_id": _admin_user_id(c, h), "delay_minutes": 15.0},
        "POST /api/config/escalation",
    ),
    (
        "POST",
        "/api/config/escalation/bulk",
        lambda c, h: {"user_ids": []},  # admin -> 422 (liste vide), jamais 403
        "POST /api/config/escalation/bulk",
    ),
    (
        "POST",
        "/api/config/system",
        lambda c, h: {"key": "_inv077_probe", "value": "x"},  # admin -> 200, jamais 403
        "POST /api/config/system",
    ),
]


@pytest.mark.parametrize("method,path,body_factory,label", _ENDPOINTS)
def test_admin_only_endpoint_returns_403_for_non_admin_and_not_403_for_admin(
    client, admin_headers, user1_headers, method, path, body_factory, label
):
    """INV-077 : sur chaque endpoint admin-only, un token non-admin DOIT recevoir
    403, et un token admin DOIT recevoir autre chose (peu importe quoi).

    Attrape un retrait accidentel de `Depends(get_current_admin)` sur l'un de
    ces handlers : sans cette protection, le user non-admin obtiendrait
    typiquement 200/404/409 — le test echoue alors avec "expected 403, got 2xx".
    """
    body = body_factory(client, admin_headers)

    # 1) Non-admin -> 403 attendu.
    r_user = client.request(method, path, json=body, headers=user1_headers)
    assert r_user.status_code == 403, (
        f"INV-077 viole sur {label} : un user non-admin a obtenu {r_user.status_code} "
        f"(body: {r_user.text!r}). L'endpoint doit refuser les non-admins avec 403. "
        f"Verifier `Depends(get_current_admin)` sur le handler."
    )

    # 2) Admin -> NON-403 attendu (le statut exact depend du payload, mais
    #    le seul interdit est 403, qui signalerait un faux negatif : la
    #    dependency d'auth a echoue meme pour un admin).
    r_admin = client.request(method, path, json=body, headers=admin_headers)
    assert r_admin.status_code != 403, (
        f"INV-077 faux negatif sur {label} : admin recoit 403 ({r_admin.text!r}). "
        f"L'assertion non-admin ci-dessus ne prouve rien si admin aussi est rejete."
    )
