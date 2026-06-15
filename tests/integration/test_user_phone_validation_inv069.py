"""
Tier 2 integration tests : INV-069 §3 — defense-in-depth de la validation
du numero de telephone cote backend.

Contexte (issue follow-up PR #165) : la validation regex `^\\+?[0-9]{6,15}$`
existe cote JS frontend (`savePhone` dans `index.html`), mais le backend
acceptait n'importe quel string :

- `UserCreate._normalize_phone` (schemas.py) ne faisait que strip les espaces.
- `PATCH /api/users/{id}` (users.py) recevait un `payload: dict` sans schema
  Pydantic du tout.

Probleme : un client direct (script admin, autre frontend, faute de frappe
contournant le JS) pouvait persister un `phone_number="abcd!!"`. Le modem
SIM7600 echoue alors silencieusement a composer le numero — l'escalade SMS/
appel (INV-061) est cassee pour cet operateur sans alerte cote serveur.

L'invariant INV-069 §3 dit : "Une valeur non vide doit matcher
`^\\+?[0-9]{6,15}$` (format composable par le modem SIM7600)". Le mot
"avant envoi" peut se lire au niveau JS ou au niveau API. Defense-in-depth :
on applique aux deux. La validation reste fidele au texte de l'invariant
(meme regex, meme tolerance pour les valeurs vides qui effacent vers NULL).

Strategie :
  - POST /api/auth/register et PATCH /api/users/{id} : phone invalide → 422.
  - Le path legitime (phone valide ou vide) doit toujours renvoyer 200.
  - On verifie aussi la persistance DB du PATCH (gap pointe par le bonus P3
    de l'issue : test_save_phone ne validait que la requete, pas la persistance).

Budget P4 : 4 tests.
"""

import uuid

import pytest

pytestmark = pytest.mark.integration


def _fresh_name(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _admin_user_id(client, admin_headers):
    users = client.get("/api/users/", headers=admin_headers).json()
    for u in users:
        if u["name"] == "admin":
            return u["id"]
    raise AssertionError("admin user not found")


def test_register_with_invalid_phone_returns_422(client):
    """INV-069 §3 defense-in-depth : POST /api/auth/register avec un
    phone_number qui ne matche pas `^\\+?[0-9]{6,15}$` DOIT renvoyer 422.

    Attrape la regression ou un client direct (script, autre UI, copier-coller
    rate) reussit a persister un numero non composable. Le canal SMS/appel
    (INV-061) deviendrait alors silencieusement mort pour cet operateur :
    le modem SIM7600 rejette la commande AT mais aucune alerte serveur ne
    remonte → l'admin ne voit que `phone_number` non NULL et croit que le
    canal est OK (le badge "pas de tel" INV-069 §4 ne se declenche pas).
    """
    body = {
        "name": _fresh_name("badphone"),
        "password": "pass1234",
        "phone_number": "abcd!!",
    }
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 422, (
        f"POST /api/auth/register avec phone='abcd!!' doit etre rejete au "
        f"boundary API (defense-in-depth INV-069 §3). Got {r.status_code}: "
        f"{r.text}"
    )


def test_register_with_valid_phone_and_empty_phone_accepted(client):
    """INV-069 §3 : phone_number vide ('') DOIT etre accepte et normalise
    vers None (efface, cf INV-061 « pas de SMS/appel si NULL »). Un format
    valide `+33612345678` DOIT passer et etre persiste tel quel.

    Attrape la regression ou la validation regex ajoutee rejette aussi le
    cas legitime (faux positif sur "") — sans ce test, un fix trop strict
    casserait l'efface-numero qui est un cas valide du formulaire.
    """
    n_valid = _fresh_name("okphone")
    r = client.post("/api/auth/register", json={
        "name": n_valid, "password": "pass1234", "phone_number": "+33612345678",
    })
    assert r.status_code == 200, (
        f"phone valide rejete: {r.status_code} {r.text}"
    )
    assert r.json()["phone_number"] == "+33612345678"

    n_empty = _fresh_name("nophone")
    r = client.post("/api/auth/register", json={
        "name": n_empty, "password": "pass1234", "phone_number": "",
    })
    assert r.status_code == 200, (
        f"phone vide rejete (doit etre accepte, INV-061): {r.status_code} {r.text}"
    )
    assert r.json()["phone_number"] is None, (
        f"phone vide doit etre normalise vers None, got "
        f"{r.json()['phone_number']!r}"
    )


def test_patch_user_with_invalid_phone_returns_422(client, admin_headers):
    """INV-069 §3 defense-in-depth : PATCH /api/users/{id} avec
    phone_number='abcd!!' DOIT renvoyer 422.

    Avant ce fix, `update_user` declarait `payload: dict` sans schema
    Pydantic → tout body raw etait accepte. Un appel direct a l'API
    (ex: `curl -X PATCH ... -d '{"phone_number":"abcd!!"}'`) pouvait
    contourner la validation JS et planter le canal SMS/appel pour cet
    operateur (INV-061).
    """
    admin_id = _admin_user_id(client, admin_headers)

    r = client.patch(
        f"/api/users/{admin_id}",
        json={"phone_number": "abcd!!"},
        headers=admin_headers,
    )
    assert r.status_code == 422, (
        f"PATCH /api/users/{admin_id} avec phone='abcd!!' doit etre rejete "
        f"(defense-in-depth INV-069 §3). Got {r.status_code}: {r.text}"
    )


def test_patch_user_with_valid_phone_persists_and_returns_200(
    client, admin_headers,
):
    """INV-069 §3 : PATCH avec un numero valide DOIT renvoyer 200, et la
    nouvelle valeur DOIT etre persistee en DB (verifiable via un GET
    suivant).

    Couvre aussi le gap pointe par le bonus P3 de l'issue follow-up :
    test_save_phone (test_frontend.py) ne validait que le contenu de la
    requete PATCH, pas la reponse 200 ni la persistance DB. Sans cette
    verification, un fix qui rendrait `update_user` no-op (mais accepterait
    le body sans erreur) passerait silencieusement.
    """
    admin_id = _admin_user_id(client, admin_headers)
    valid = "+33611223344"

    try:
        r = client.patch(
            f"/api/users/{admin_id}",
            json={"phone_number": valid},
            headers=admin_headers,
        )
        assert r.status_code == 200, (
            f"PATCH valide rejete: {r.status_code} {r.text}"
        )
        assert r.json()["phone_number"] == valid, (
            f"Reponse PATCH doit refleter la nouvelle valeur, got "
            f"{r.json().get('phone_number')!r}"
        )

        # Persistance : GET /api/users/ doit voir le meme numero.
        users = client.get("/api/users/", headers=admin_headers).json()
        admin = next(u for u in users if u["name"] == "admin")
        assert admin["phone_number"] == valid, (
            f"phone_number non persiste apres PATCH: got "
            f"{admin['phone_number']!r}"
        )
    finally:
        # Cleanup : remettre a None pour ne pas polluer les autres tests
        # qui s'attendent au seed (admin.phone_number = NULL).
        client.patch(
            f"/api/users/{admin_id}",
            json={"phone_number": None},
            headers=admin_headers,
        )
