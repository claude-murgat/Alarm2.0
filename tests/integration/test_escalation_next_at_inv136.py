"""
Tier 2 integration test : INV-136 — `GET /api/alarms/active/escalation`
expose un `next_escalation_at` qui glisse apres chaque palier d'escalade.

Trou identifie pendant triplet review PR #232 : `escalation.py:192, 280` et
`calls.py:96` resetent `alarm.created_at = clock_now()` a chaque palier (le
compte a rebours UI glisse). Mais `POST /api/test/trigger-escalation`
n'alignait PAS ce reset — les tests deterministes (`test_escalation_live.py`
::test_escalation_is_cumulative, `test_e2e.py` TestEscalation...) ne
pouvaient donc pas verifier que la logique prod tient. Si quelqu'un casse
le reset `created_at` dans `escalation.py`, le compte a rebours UI
resterait colle a t0 — regression invisible aujourd'hui.

Fix : aligner `trigger-escalation` sur la prod (`backend/app/api/test_api.py`
:: ligne 260 approx, `alarm.created_at = clock_now()`) et ajouter ce test
qui prouve la propagation jusqu'au payload HTTP `/active/escalation` :
  1. `created_at > original_created_at` apres escalade (le timer a bouge)
  2. `next_escalation_at - created_at == escalation_delay` (expose INV-136)

Budget P4 : 1 test.
"""
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.integration


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _user_headers(client, name: str, password: str) -> dict:
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, f"login {name} failed: {r.status_code} {r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _reset(client, admin_headers: dict) -> None:
    r = client.post("/api/test/reset-clock", params={"peer": "false"})
    assert r.status_code == 200, r.text
    r = client.post("/api/alarms/reset", headers=admin_headers)
    assert r.status_code == 200, r.text


def test_inv136_next_escalation_at_slides_after_trigger_escalation(client, admin_headers):
    """INV-136 : apres `POST /api/test/trigger-escalation`, `alarm.created_at`
    est reset a `clock_now()` (comme en prod, escalation.py:280) → le
    `next_escalation_at` expose par `/active/escalation` glisse, et
    `created_at > original_created_at` (INV-018 reste immuable).

    Sequence :
      1. POST /alarms/send -> original_created_at = created_at = t0.
      2. GET /active/escalation : sanity, les deux timestamps sont egaux.
      3. advance-clock +5min -> clock_now() = t0 + 5min.
      4. POST /test/trigger-escalation : escalade effective (escalated >= 1).
      5. GET /active/escalation :
         - created_at a ete reset (~ t0 + 5min)
         - original_created_at est reste a t0 (INV-018)
         - next_escalation_at == created_at + escalation_delay_minutes

    Sans le fix de trigger-escalation (reset `alarm.created_at = clock_now()`
    ligne approx 260 de test_api.py), `created_at` resterait a t0 → le test
    FAIL sur l'assertion `delta >= 4.9` (le compte a rebours UI serait
    colle a t0 au lieu de glisser).
    """
    user1_headers = _user_headers(client, "user1", "user123")
    _reset(client, admin_headers)

    alarm_id = None
    try:
        r = client.post(
            "/api/alarms/send",
            json={
                "site": "izeron", "title": "INV-136 next_escalation_at slide",
                "message": "verifie que trigger-escalation reset created_at",
                "severity": "critical",
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        alarm_id = r.json()["id"]

        pre = client.get("/api/alarms/active/escalation", headers=admin_headers).json()
        assert pre["alarm"] is not None, f"sanity : alarme attendue, got {pre}"
        t0_created = pre["alarm"]["created_at"]
        t0_original = pre["alarm"]["original_created_at"]
        assert t0_created == t0_original, (
            f"sanity : a la creation created_at == original_created_at. "
            f"created={t0_created}, original={t0_original}"
        )

        r = client.post(
            "/api/test/advance-clock",
            params={"minutes": 5, "peer": "false"},
        )
        assert r.status_code == 200, r.text

        r = client.post("/api/test/trigger-escalation")
        assert r.status_code == 200, r.text
        assert r.json().get("escalated", 0) >= 1, (
            f"sanity : escalade attendue (chaine user1->user2->admin seedee), "
            f"got {r.json()}"
        )

        post = client.get("/api/alarms/active/escalation", headers=admin_headers).json()
        assert post["alarm"] is not None, (
            f"sanity : alarme escaladee encore active (status escalated), got {post}"
        )
        new_created = post["alarm"]["created_at"]
        new_original = post["alarm"]["original_created_at"]
        delay = post["escalation_delay_minutes"]
        next_at = post["next_escalation_at"]

        # INV-018 : original_created_at est immuable (verrouillage zero-cost).
        assert new_original == t0_original, (
            f"INV-018 : original_created_at est immuable. attendu "
            f"{t0_original}, got {new_original}"
        )

        # INV-136 (coeur du test) : le timer a bouge d'au moins ~5min (l'offset
        # applique au clock_now() avant trigger-escalation). Marge 0.1min pour
        # la latence TestClient. Sans le fix, delta ~= 0 → FAIL.
        delta_minutes = (_dt(new_created) - _dt(t0_original)).total_seconds() / 60.0
        assert delta_minutes >= 4.9, (
            f"INV-136 : apres trigger-escalation, alarm.created_at doit etre "
            f"reset a clock_now() (comme prod escalation.py:280). Attendu "
            f"~5 min apres t0, got delta={delta_minutes:.3f} min. "
            f"created_at={new_created}, original={new_original}. Si delta ~ 0, "
            f"trigger-escalation ne reset pas created_at -> next_escalation_at "
            f"resterait colle a t0 -> le compte a rebours UI (INV-136) ne "
            f"glisserait pas apres escalade -> regression silencieuse si "
            f"escalation.py:280 est casse."
        )
        assert _dt(new_created) > _dt(new_original), (
            f"INV-136 : apres escalade, created_at ({new_created}) doit "
            f"etre POSTERIEUR a original_created_at ({new_original})."
        )

        # INV-136 : next_escalation_at = created_at + escalation_delay_minutes.
        # Verrouille la formule du payload (alarms.py:246).
        expected_next = _dt(new_created) + timedelta(minutes=delay)
        assert _dt(next_at) == expected_next, (
            f"INV-136 : next_escalation_at doit valoir created_at + "
            f"escalation_delay_minutes. attendu {expected_next.isoformat()}, "
            f"got {next_at} (created={new_created}, delay={delay}min)."
        )
    finally:
        client.post("/api/test/reset-clock", params={"peer": "false"})
        if alarm_id is not None:
            try:
                client.post(
                    f"/api/alarms/{alarm_id}/resolve", headers=admin_headers
                )
            except Exception:
                pass
