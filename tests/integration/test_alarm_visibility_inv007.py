"""
Tier 2 integration tests : INV-007 — alarme resolved exclue de /active et /mine.

Source : tests/INVARIANTS.md INV-007 [M]
  "GET /api/alarms/active et GET /api/alarms/mine excluent les alarmes
   status = 'resolved'."

Pourquoi : UX app mobile — une alarme cloturee n'a aucune raison de continuer
a apparaitre dans la liste active de l'operateur ni dans son flux personnel.
Sans ce filtre, le user verrait des alarmes resolved s'accumuler.

Statut catalogue avant ce PR : "partiellement couvert" (aucun test ne verrouille
explicitement l'exclusion sur /active ni /mine). Ces 2 tests verrouillent
l'invariant en regression.

Verification anti-figeage du bug : avant de committer, le filtre actuel a ete
temporairement etendu pour inclure "resolved" dans alarms.py, et les 2 tests
ont casse (RED). Apres restauration, les 2 tests passent (GREEN).

Budget P4 : 2 tests (resolved_excluded_from_active + resolved_excluded_from_mine).
"""
import pytest

pytestmark = pytest.mark.integration


def _login(client, name: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"login {name} failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _reset_alarms(client, admin_headers):
    """Vide la table Alarm (et alarm_notifications) pour un test deterministe."""
    r = client.post("/api/alarms/reset", headers=admin_headers)
    assert r.status_code == 200, f"reset failed: {r.status_code} {r.text}"


def _user_id(client, admin_headers, name: str) -> int:
    r = client.get("/api/users/", headers=admin_headers)
    assert r.status_code == 200, r.text
    for u in r.json():
        if u["name"] == name:
            return u["id"]
    raise AssertionError(f"user {name} not in seed")


def test_resolved_alarm_excluded_from_active(client, admin_headers):
    """INV-007 : apres POST /alarms/{id}/resolve, GET /alarms/active ne contient
    plus cette alarme.

    Sequence :
      1. POST /alarms/send -> alarme A creee, status=active
      2. Sanity check : GET /active contient A
      3. POST /alarms/{A.id}/resolve -> status=resolved
      4. GET /alarms/active ne doit PAS contenir A
    """
    _reset_alarms(client, admin_headers)
    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    r = client.post(
        "/api/alarms/send",
        json={
            "title": "INV-007 active",
            "message": "exclusion resolved /active",
            "severity": "critical",
            "assigned_user_id": user1_id,
        },
        headers=user1_headers,
    )
    assert r.status_code == 200, r.text
    alarm_id = r.json()["id"]

    # Sanity : tant que c'est active, /active la liste (preuve que le test
    # observe bien le bon endpoint).
    r = client.get("/api/alarms/active", headers=admin_headers)
    assert r.status_code == 200, r.text
    ids_before = {a["id"] for a in r.json()}
    assert alarm_id in ids_before, (
        f"sanity check : alarme {alarm_id} doit etre dans /active avant resolve, "
        f"got {ids_before}"
    )

    r = client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"

    # INV-007 : apres resolve, /active n'expose plus l'alarme.
    r = client.get("/api/alarms/active", headers=admin_headers)
    assert r.status_code == 200, r.text
    ids_after = {a["id"] for a in r.json()}
    assert alarm_id not in ids_after, (
        f"INV-007 : alarme {alarm_id} status=resolved ne doit PAS apparaitre dans "
        f"/api/alarms/active, got {ids_after}"
    )


def test_resolved_alarm_excluded_from_mine(client, admin_headers):
    """INV-007 : apres POST /alarms/{id}/resolve, GET /alarms/mine (user notifie)
    ne contient plus cette alarme.

    Sequence :
      1. POST /alarms/send avec assigned_user_id=user1 -> user1 notifie
      2. Sanity check : GET /mine (user1) contient l'alarme
      3. POST /alarms/{id}/resolve
      4. GET /mine (user1) ne doit PAS contenir l'alarme
    """
    _reset_alarms(client, admin_headers)
    user1_id = _user_id(client, admin_headers, "user1")
    user1_token = _login(client, "user1", "user123")
    user1_headers = {"Authorization": f"Bearer {user1_token}"}

    r = client.post(
        "/api/alarms/send",
        json={
            "title": "INV-007 mine",
            "message": "exclusion resolved /mine",
            "severity": "critical",
            "assigned_user_id": user1_id,
        },
        headers=user1_headers,
    )
    assert r.status_code == 200, r.text
    alarm_id = r.json()["id"]

    # Sanity : tant que c'est active, /mine la liste cote user1.
    r = client.get("/api/alarms/mine", headers=user1_headers)
    assert r.status_code == 200, r.text
    ids_before = {a["id"] for a in r.json()}
    assert alarm_id in ids_before, (
        f"sanity check : user1 doit voir alarme {alarm_id} dans /mine avant "
        f"resolve, got {ids_before}"
    )

    r = client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"

    # INV-007 : apres resolve, /mine n'expose plus l'alarme cote user notifie.
    r = client.get("/api/alarms/mine", headers=user1_headers)
    assert r.status_code == 200, r.text
    ids_after = {a["id"] for a in r.json()}
    assert alarm_id not in ids_after, (
        f"INV-007 : alarme {alarm_id} status=resolved ne doit PAS apparaitre dans "
        f"/api/alarms/mine pour user1 (notifie), got {ids_after}"
    )
