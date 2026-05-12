"""
Tier 2 integration test : INV-110 — `GET /api/alarms/?days=N` clampe N dans [1, 90].

Source : tests/INVARIANTS.md INV-110 [L] (statut "partiellement couvert")
  "GET /alarms/?days=N -> N clampe a [1, 90]."

Pourquoi : sans clamp, un client mal configure (ou malveillant) peut envoyer
days=0 ou days=-5 -> filtre `created_at >= clock_now` (ou clock_now+5j) -> la
reponse est vide alors qu'il y a des alarmes recentes. A l'oppose, days=10000
laisse passer toute alarme historique (cout DB + payload).

Le clamp implementé (`backend/app/api/alarms.py:123` : `days = max(1, min(days, 90))`)
doit garantir le contrat retro-compatible :
- days <= 0  -> comportement de days=1
- days >= 91 -> comportement de days=90
- 1 <= days <= 90 -> comportement normal

Verification anti-figeage du bug (RED -> GREEN) : en commentant temporairement
la ligne `days = max(1, min(days, 90))` dans `alarms.py`, les cas `days=0`,
`days=-5` et `days=10000` du parametrize echouent (alarme attendue absente, ou
alarme non attendue presente). Apres restauration, tous passent (GREEN).

Budget P4 : 1 test parametre avec 4 cas (bas x2, in-range x1, haut x1).
"""
import pytest

pytestmark = pytest.mark.integration


def _login(client, name: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"login {name} failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def _user_id(client, admin_headers, name: str) -> int:
    r = client.get("/api/users/", headers=admin_headers)
    assert r.status_code == 200, r.text
    for u in r.json():
        if u["name"] == name:
            return u["id"]
    raise AssertionError(f"user {name} not in seed")


def _reset_alarms(client, admin_headers):
    r = client.post("/api/alarms/reset", headers=admin_headers)
    assert r.status_code == 200, f"reset alarms failed: {r.status_code} {r.text}"


def _reset_clock(client):
    # peer=false : pas de propagation cluster en tier 2 (instance unique).
    r = client.post("/api/test/reset-clock", params={"peer": "false"})
    assert r.status_code == 200, f"reset-clock failed: {r.status_code} {r.text}"


def _advance_clock_days(client, days: int):
    if days <= 0:
        return
    r = client.post(
        "/api/test/advance-clock",
        params={"seconds": days * 24 * 3600, "peer": "false"},
    )
    assert r.status_code == 200, f"advance-clock failed: {r.status_code} {r.text}"


@pytest.mark.parametrize(
    "days_query,age_alarm_days,alarm_expected,zone",
    [
        # Out-of-range bas : clamp -> 1. Alarme fraiche -> visible.
        # Sans clamp: filtre `created_at >= clock_now` (days=0) ou
        # `>= clock_now + 5j` (days=-5) -> alarme exclue -> test FAIL (RED).
        (0, 0, True, "out-of-range-bas (0)"),
        (-5, 0, True, "out-of-range-bas (negatif)"),
        # In-range : pas de clamp applique, comportement normal.
        (10, 0, True, "in-range (10)"),
        # Out-of-range haut : clamp -> 90. Alarme agee de 100j (via
        # advance-clock) -> au-dela de la fenetre 90j -> exclue.
        # Sans clamp: days=10000 -> since = now - 10000j -> alarme incluse
        # -> test FAIL (RED).
        (10000, 100, False, "out-of-range-haut (10000)"),
    ],
    ids=[
        "days=0 clampe a 1 -> alarme fraiche visible",
        "days=-5 clampe a 1 -> alarme fraiche visible",
        "days=10 in-range -> alarme fraiche visible",
        "days=10000 clampe a 90 -> alarme 100j exclue",
    ],
)
def test_get_alarms_days_clamp_to_valid_range(
    client, admin_headers, days_query, age_alarm_days, alarm_expected, zone
):
    """INV-110 : GET /api/alarms/?days=N doit clamper N dans [1, 90].

    Setup :
      1. Reset alarms + reset clock (offset=0).
      2. Cree 1 alarme (created_at = now reel via default SQLAlchemy).
      3. Eventuellement avance l'horloge de `age_alarm_days` jours pour vieillir
         l'alarme par rapport a clock_now (cf models.py:38 Alarm.created_at
         default=datetime.utcnow, indépendant de clock_now).
      4. Appelle GET /api/alarms/?days={days_query} et verifie inclusion ou
         non de l'alarme.

    Le comportement attendu (clamp present) :
      - days=0, days=-5 -> equivalent days=1 : alarme fraiche visible.
      - days=10        -> normal : alarme fraiche visible.
      - days=10000     -> equivalent days=90 : alarme agee de 100j EXCLUE.
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    try:
        # Cree l'alarme AVANT advance-clock pour que son created_at corresponde
        # bien a "maintenant reel", puis advance-clock la fait apparaitre comme
        # plus ancienne du point de vue de clock_now (utilise par le filtre).
        r = client.post(
            "/api/alarms/send",
            json={
                "title": f"INV-110 {zone}",
                "message": f"clamp test days={days_query}",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, f"send-alarm failed: {r.status_code} {r.text}"
        alarm_id = r.json()["id"]

        _advance_clock_days(client, age_alarm_days)

        r = client.get(
            f"/api/alarms/?days={days_query}", headers=admin_headers
        )
        assert r.status_code == 200, (
            f"GET /api/alarms/?days={days_query} doit renvoyer 200 (le clamp "
            f"protege aussi contre un timedelta absurde). Got: {r.status_code} {r.text}"
        )
        ids = {a["id"] for a in r.json()}

        if alarm_expected:
            assert alarm_id in ids, (
                f"INV-110 [{zone}] : avec days={days_query} et alarme agee de "
                f"{age_alarm_days}j, l'alarme {alarm_id} DOIT etre listee "
                f"(clamp [1,90] -> equivalent days={'1' if days_query <= 0 else days_query}). "
                f"Got ids={ids}. Sans clamp, days<=0 filtre `created_at >= clock_now"
                f"{'+|days|j' if days_query < 0 else ''}` et exclut l'alarme."
            )
        else:
            assert alarm_id not in ids, (
                f"INV-110 [{zone}] : avec days={days_query} et alarme agee de "
                f"{age_alarm_days}j, l'alarme {alarm_id} NE DOIT PAS etre listee "
                f"(clamp [1,90] -> equivalent days=90, et 100j > 90j). "
                f"Got ids={ids}. Sans clamp, days={days_query} ouvrirait une "
                f"fenetre de {days_query}j et l'alarme y serait incluse."
            )
    finally:
        # Cleanup deterministe (INV-902) : remettre offset=0 pour les tests
        # suivants de la session, et resoudre l'alarme pour ne pas violer INV-001.
        _reset_clock(client)
        try:
            client.post(
                f"/api/alarms/{alarm_id}/resolve", headers=admin_headers
            )
        except Exception:
            pass
