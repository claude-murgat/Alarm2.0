"""
Tier 2 integration tests : INV-078 — DELETE /api/devices/fcm-token retire le
token FCM de la base, plus aucun push ne doit cibler ce token apres logout.

Source : tests/INVARIANTS.md INV-078 [M]
  "POST /devices/fcm-token DELETE -> retire de la base, plus de push recu."

Et CLAUDE.md (Decisions techniques) :
  "Logout supprime le token FCM cote backend (plus de push apres deconnexion)".

Pourquoi : l'app Android appelle DELETE /api/devices/fcm-token au logout pour
que FCM cesse de viser cet appareil. Sans cette suppression, un user qui se
deconnecte (pret a un collegue, par ex.) continuerait a recevoir les pushs
d'alarme, brouillant la chaine d'astreinte et exposant des donnees sensibles.

Statut catalogue avant ce PR : "partiellement couvert" — un test E2E existant
(`tests/test_fcm.py::test_delete_fcm_token`) ne verifie QUE le HTTP 200, pas
la suppression effective en base. Si l'on commente le `.delete()` dans
`backend/app/api/devices.py::delete_fcm_token`, l'endpoint continue a renvoyer
200 et ce test passerait — autrement dit, il ne verrouille rien en regression.

Verification de sensibilite (P2 — le test prouve quelque chose) :
  Commenter mentalement `.delete()` (ligne `backend/app/api/devices.py:136`)
  -> le bloc devient `deleted = (db.query(DeviceToken).filter(...))`, qui
  renvoie un Query, pas un int, et NE supprime rien. db.commit() s'execute
  sur une session sans pending change. La ligne reste en base.
  -> Assertions ci-dessous (count == 0 apres DELETE) echouent franchement.

Aucune modification du code de production : le code est deja conforme a
l'invariant (audit confirme). Ces tests existent pour verrouiller la
propriete contre des regressions futures (cf. pattern PRs #89/#90/#91).

Budget P4 : 2 tests cibles.
  - Test 1 : presence avant / absence apres (le coeur de l'invariant).
  - Test 2 : la suppression est scopee au device_id passe (defense en
    profondeur — empeche un futur refactor de retirer le filtre device_id
    et de wiper tous les devices d'un meme user en bloc).
"""
import pytest

pytestmark = pytest.mark.integration


def _login(client, name: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"login {name} failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _user_id_by_name(client, admin_headers, name: str) -> int:
    r = client.get("/api/users/", headers=admin_headers)
    assert r.status_code == 200, r.text
    for u in r.json():
        if u["name"] == name:
            return u["id"]
    raise AssertionError(f"user {name} not in seed")


def _count_device_tokens(user_id: int, device_id: str) -> int:
    """Compte les lignes DeviceToken pour (user_id, device_id) en lecture
    directe (pas via API : la verification doit etre independante du chemin
    `delete_fcm_token` que l'on cherche a verrouiller).
    """
    from backend.app.database import SessionLocal
    from backend.app.models import DeviceToken

    db = SessionLocal()
    try:
        return (
            db.query(DeviceToken)
            .filter(
                DeviceToken.user_id == user_id,
                DeviceToken.device_id == device_id,
            )
            .count()
        )
    finally:
        db.close()


def _fcm_token_value(user_id: int, device_id: str) -> str | None:
    """Renvoie la valeur fcm_token courante en base pour (user_id, device_id),
    None si absent. Utilise pour distinguer 'pas de ligne' vs 'ligne avec
    autre token' (defense contre une regression subtile)."""
    from backend.app.database import SessionLocal
    from backend.app.models import DeviceToken

    db = SessionLocal()
    try:
        row = (
            db.query(DeviceToken)
            .filter(
                DeviceToken.user_id == user_id,
                DeviceToken.device_id == device_id,
            )
            .first()
        )
        return row.fcm_token if row is not None else None
    finally:
        db.close()


# Suffixe unique pour eviter toute collision avec d'autres tests qui
# partagent la meme DB (session-scope) et ne reseteraient pas device_tokens.
_DEV_PREFIX = "inv078-fcm-logout"


def test_delete_fcm_token_removes_db_row_inv078(client, admin_headers):
    """INV-078 (coeur) : apres DELETE /api/devices/fcm-token, la ligne
    DeviceToken correspondante a (user_id, device_id) n'existe plus en base.

    Pourquoi cette assertion : la consequence operationnelle ("plus de push
    apres logout") repose sur le fait que `send_fcm_to_user`
    (`backend/app/fcm_service.py:82`) lit la table DeviceToken pour decider
    a qui pusher. Si la ligne reste, le user continuera a recevoir des pushs
    meme apres logout. Verifier l'absence en base = verifier la cause
    racine, pas un effet secondaire mockable.

    Sensibilite (mutation locale, non commit) : commenter `.delete()` dans
    `delete_fcm_token` -> ligne persiste -> assert count == 0 echoue.

    Sequence :
      1. user1 login
      2. POST /api/devices/fcm-token (token=t1, device_id=d1)
      3. Sanity : 1 ligne en base avec fcm_token=t1
      4. DELETE /api/devices/fcm-token (device_id=d1)
      5. INV-078 : 0 ligne en base pour (user1, d1)
    """
    device_id = f"{_DEV_PREFIX}-single-d1"
    token_value = "fcm-token-t1-inv078-core"
    user1_id = _user_id_by_name(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    # Nettoyage defensif : un run precedent peut avoir laisse une ligne
    # (la DB tier 2 est session-scope et /api/test/reset ne purge pas
    # device_tokens). On veut un etat de depart deterministe.
    client.request(
        "DELETE",
        "/api/devices/fcm-token",
        json={"device_id": device_id},
        headers=user1_headers,
    )
    assert _count_device_tokens(user1_id, device_id) == 0, (
        f"setup invalide : une ligne residuelle existe deja pour user1/{device_id}"
    )

    # 1) Enregistrer le token (pre-condition de l'invariant : il y a quelque
    #    chose a supprimer).
    r = client.post(
        "/api/devices/fcm-token",
        json={"token": token_value, "device_id": device_id},
        headers=user1_headers,
    )
    assert r.status_code == 200, f"register fcm-token failed: {r.status_code} {r.text}"

    # Sanity : la ligne est bien en base avec la valeur attendue. Sans ce
    # sanity check, le test final pourrait passer "par hasard" parce que
    # l'enregistrement initial aurait silencieusement echoue.
    assert _fcm_token_value(user1_id, device_id) == token_value, (
        f"sanity : token doit etre enregistre avec valeur {token_value!r} "
        f"avant le DELETE, got {_fcm_token_value(user1_id, device_id)!r}"
    )

    # 2) Logout cote backend : suppression du token FCM.
    r = client.request(
        "DELETE",
        "/api/devices/fcm-token",
        json={"device_id": device_id},
        headers=user1_headers,
    )
    assert r.status_code == 200, f"DELETE fcm-token failed: {r.status_code} {r.text}"

    # 3) Assertion centrale INV-078 : aucune ligne ne subsiste pour ce
    #    (user_id, device_id). C'est la propriete qui empeche un push de
    #    cibler ce token apres logout.
    remaining = _count_device_tokens(user1_id, device_id)
    assert remaining == 0, (
        f"INV-078 viole : apres DELETE /api/devices/fcm-token avec "
        f"device_id={device_id!r}, il reste {remaining} ligne(s) "
        f"DeviceToken pour user1 (user_id={user1_id}). Le token FCM "
        f"continuera donc a recevoir des pushs apres logout. "
        f"Cause probable : la ligne `db.query(DeviceToken).filter(...).delete()` "
        f"dans backend/app/api/devices.py::delete_fcm_token n'execute plus "
        f"reellement le DELETE (ex: chainage casse, filtre faux, commit "
        f"absent)."
    )


def test_delete_fcm_token_only_removes_target_device_inv078(client, admin_headers):
    """INV-078 (defense en profondeur) : DELETE /api/devices/fcm-token
    supprime UNIQUEMENT le device cible (filtre par device_id), pas les
    autres devices du meme user.

    Pourquoi : un user peut avoir plusieurs appareils enregistres (tablette
    + telephone). Logout sur l'un ne doit pas decabler les autres — sinon
    on perd la chaine d'astreinte sur les devices encore connectes.

    Sensibilite : si un refactor retire le `DeviceToken.device_id == data.device_id`
    du filtre (ex: simplification accidentelle en `DeviceToken.user_id == ...`),
    les DEUX lignes seraient supprimees -> assertion echoue.

    Sequence :
      1. user1 enregistre 2 devices (d1, d2) avec 2 tokens differents
      2. Sanity : 2 lignes en base
      3. DELETE /api/devices/fcm-token avec device_id=d1
      4. Assert : ligne d1 absente, ligne d2 toujours presente avec son token
    """
    device_a = f"{_DEV_PREFIX}-multi-da"
    device_b = f"{_DEV_PREFIX}-multi-db"
    token_a = "fcm-token-ta-inv078-multi"
    token_b = "fcm-token-tb-inv078-multi"
    user1_id = _user_id_by_name(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    # Cleanup defensif (DB session-scope, voir test 1).
    for did in (device_a, device_b):
        client.request(
            "DELETE",
            "/api/devices/fcm-token",
            json={"device_id": did},
            headers=user1_headers,
        )
        assert _count_device_tokens(user1_id, did) == 0, (
            f"setup invalide : ligne residuelle pour user1/{did}"
        )

    # 1) Enregistrer 2 devices distincts.
    for did, tok in ((device_a, token_a), (device_b, token_b)):
        r = client.post(
            "/api/devices/fcm-token",
            json={"token": tok, "device_id": did},
            headers=user1_headers,
        )
        assert r.status_code == 200, f"register {did} failed: {r.status_code} {r.text}"

    # Sanity : les 2 lignes sont en base, chacune avec son token.
    assert _fcm_token_value(user1_id, device_a) == token_a
    assert _fcm_token_value(user1_id, device_b) == token_b

    # 2) Logout sur device_a uniquement.
    r = client.request(
        "DELETE",
        "/api/devices/fcm-token",
        json={"device_id": device_a},
        headers=user1_headers,
    )
    assert r.status_code == 200, f"DELETE {device_a} failed: {r.status_code} {r.text}"

    # 3) Le device cible est parti...
    assert _count_device_tokens(user1_id, device_a) == 0, (
        f"INV-078 viole : DELETE device_id={device_a!r} doit retirer cette "
        f"ligne ; elle est toujours presente."
    )
    # ...mais l'autre device subsiste avec son token intact.
    surviving = _fcm_token_value(user1_id, device_b)
    assert surviving == token_b, (
        f"INV-078 (defense scope filtre) : DELETE device_id={device_a!r} a "
        f"ete trop large — la ligne pour device_id={device_b!r} a disparu ou "
        f"a ete modifiee (attendu fcm_token={token_b!r}, got {surviving!r}). "
        f"Cause probable : le filtre `DeviceToken.device_id == data.device_id` "
        f"a ete retire de delete_fcm_token (backend/app/api/devices.py), ce "
        f"qui supprimerait TOUS les devices du user au moindre logout."
    )

    # Cleanup : retirer la ligne survivante pour ne pas polluer d'autres tests.
    client.request(
        "DELETE",
        "/api/devices/fcm-token",
        json={"device_id": device_b},
        headers=user1_headers,
    )
