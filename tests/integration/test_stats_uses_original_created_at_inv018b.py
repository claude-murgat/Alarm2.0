"""Tier 2 integration : INV-018b sous-issue #78 — stats KPI doivent utiliser
`original_created_at` (date d'evenement), PAS `created_at` (timer escalade).

Source : tests/INVARIANTS.md INV-018b [C] 🐛
  "Une alarme escaladee 2h apres creation compte aujourd'hui dans la
   mauvaise semaine, et son MTTR est artificiellement raccourci."

Scope sous-issue #78 (4 call sites de `backend/app/api/stats.py`) :
  - L107-110 : filtre `Alarm.created_at >= since` + ORDER BY (periode)
  - L120     : bucketing par semaine (Python `a.created_at < week_end`)
  - L123     : filtre `_est_hors_heures_ouvrees(a.created_at)` (heures)
  - L139-140 : selector resolved (`a.created_at` is not None)
  - L143     : calcul MTTR (`updated_at - a.created_at`)

Tous doivent passer a `original_created_at` (immuable, INV-018 PR #102).

Strategie RED (miroir de tests/integration/test_alarms_reads_inv018b.py) :
on simule le "reset created_at" que fait escalation_loop en ack-expiry
(escalation.py:186) et escalade (escalation.py:274), en manipulant
directement la colonne via SessionLocal. C'est legitime en tier 2 car ces
tests verifient les LECTURES (les KPI), pas le mecanisme de reset.

Hors scope : sous-issue #85 (frontend timeAgo, human-required).

Budget P4 : 3 tests (bucketing semaine, MTTR, filtre periode).
"""
from datetime import datetime, timedelta

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
    r = client.post("/api/test/reset-clock", params={"peer": "false"})
    assert r.status_code == 200, r.text


def _advance_clock_minutes(client, minutes: float):
    r = client.post(
        "/api/test/advance-clock",
        params={"minutes": minutes, "peer": "false"},
    )
    assert r.status_code == 200, r.text


def _force_alarm_state(
    alarm_id: int,
    created_at: datetime | None = None,
    status: str | None = None,
    acknowledged_at: datetime | None = None,
    updated_at: datetime | None = None,
    escalation_count: int | None = None,
    original_created_at: datetime | None = None,
):
    """Manipule directement les champs d'une alarme via SessionLocal pour mettre
    en scene les scenarios d'escalade tardive / resolution. N'affecte JAMAIS
    `original_created_at` (immuable par contrat INV-018) — SAUF si le test
    fournit explicitement `original_created_at` pour staging d'une alarme
    "nee a tel moment passe" (e.g. dimanche pour test hors_heures L127)."""
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm
    db = SessionLocal()
    try:
        alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
        assert alarm is not None, f"alarme {alarm_id} introuvable"
        if created_at is not None:
            alarm.created_at = created_at
        if status is not None:
            alarm.status = status
        if acknowledged_at is not None:
            alarm.acknowledged_at = acknowledged_at
        if updated_at is not None:
            alarm.updated_at = updated_at
        if escalation_count is not None:
            alarm.escalation_count = escalation_count
        if original_created_at is not None:
            alarm.original_created_at = original_created_at
        db.commit()
    finally:
        db.close()


def _create_alarm(client, headers, user_id: int, title: str) -> int:
    r = client.post(
        "/api/alarms/send",
        json={
            "title": title,
            "message": f"INV-018b stats test: {title}",
            "severity": "critical",
            "assigned_user_id": user_id,
        },
        headers=headers,
    )
    assert r.status_code == 200, f"send alarm failed: {r.status_code} {r.text}"
    return r.json()["id"]


def test_inv018b_kpi_bucketing_uses_original_created_at(client, admin_headers):
    """INV-018b stats : bucketing par semaine sur `original_created_at`.

    Scenario : alarme creee en semaine N, escaladee en semaine N+1 (le code
    de prod reset alarm.created_at a `now`). Sans le fix, le bucketing
    compte l'alarme dans la semaine N+1 (semaine du reset), alors qu'elle
    a ete declenchee en semaine N — KPI fausse, manageur prend de mauvaises
    decisions.

    Etapes :
      1. POST /alarms/send a t=0 (semaine N) → original_created_at = created_at = t0
      2. advance-clock +8 jours → now en semaine N+1
      3. Force created_at = now (simule reset par escalade)
      4. GET /stats/kpi?weeks=2&hors_heures_only=false
      5. Verifier : la semaine la plus ancienne (week_start ~= t0) contient
         l'alarme (count >= 1), pas la semaine recente.
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    alarm_id = _create_alarm(client, user1_headers, user1_id, "INV-018b stats bucketing")

    # +8 jours puis force created_at au now → semaine N+1
    _advance_clock_minutes(client, 8 * 24 * 60)
    from backend.app.clock import now as clock_now
    now_after = clock_now()
    _force_alarm_state(alarm_id, created_at=now_after)

    # weeks=2 → 2 buckets : [now-2w; now-1w[ (semaine ancienne ~ t0)
    #                       [now-1w; now[   (semaine recente = celle du reset)
    r = client.get(
        "/api/stats/kpi",
        params={"weeks": 2, "hors_heures_only": False},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    kpi = r.json()

    assert "weeks" in kpi and len(kpi["weeks"]) == 2, (
        f"sanity : reponse KPI doit avoir 2 buckets de semaine, got {kpi}"
    )

    # Semaine ancienne = index 0 (avant), recente = index 1 (now).
    # Avec fix : l'alarme (original_created_at = t0 = il y a 8j) tombe dans
    # le bucket ancien — au moins 1 alarme attendue dans index 0.
    # Avec bug : created_at = now → tombe dans le bucket recent (index 1).
    older_bucket = kpi["weeks"][0]
    recent_bucket = kpi["weeks"][1]

    assert older_bucket["total"] >= 1, (
        f"INV-018b #78 [C] : alarme avec original_created_at il y a 8 jours "
        f"doit etre comptee dans le bucket ancien (week_start={older_bucket['week_start']}). "
        f"Got total={older_bucket['total']}. "
        f"Si 0 : le code utilise `created_at` (reset par escalade) au lieu de "
        f"`original_created_at` → bucketing fausse, manager voit l'alarme dans la "
        f"mauvaise semaine. Voir backend/app/api/stats.py L120."
    )
    assert recent_bucket["total"] == 0, (
        f"INV-018b #78 [C] : l'alarme ne doit PAS apparaitre dans le bucket "
        f"recent (week_start={recent_bucket['week_start']}, total={recent_bucket['total']}). "
        f"Si > 0 : bucketing utilise created_at qui a ete reset au now."
    )


def test_inv018b_kpi_mttr_uses_original_created_at(client, admin_headers):
    """INV-018b stats : MTTR calcule sur `original_created_at`.

    Scenario : alarme creee a T0, ack et resolved a T0+3h. Entre temps,
    une escalade a T0+2h a reset created_at a T0+2h (logique escalation_loop).

    MTTR attendu : 3h (180 min). MTTR avec bug : 1h (60 min) — diff
    updated_at - created_at = (T0+3h) - (T0+2h) = 1h.

    Plus l'alarme est escaladee tardivement, plus le MTTR bug-affiche est
    sous-evalue : KPI MTTR ne reflete plus la realite operationnelle.
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    alarm_id = _create_alarm(client, user1_headers, user1_id, "INV-018b stats mttr")

    # +3h horloge, on a maintenant T0+3h
    _advance_clock_minutes(client, 180)
    from backend.app.clock import now as clock_now
    t_resolved = clock_now()
    t_escalation_reset = t_resolved - timedelta(hours=1)  # T0+2h (simule reset escalade)

    _force_alarm_state(
        alarm_id,
        created_at=t_escalation_reset,
        status="resolved",
        acknowledged_at=t_resolved,
        updated_at=t_resolved,
    )

    r = client.get(
        "/api/stats/kpi",
        params={"weeks": 4, "hors_heures_only": False},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    kpi = r.json()

    mttr = kpi.get("mttr_minutes")
    assert mttr is not None, f"sanity : reponse KPI doit avoir mttr_minutes, got {kpi}"

    # MTTR attendu ~ 180 min (3h, fenetre original_created_at → updated_at).
    # MTTR bug ~ 60 min (1h, fenetre created_at → updated_at).
    # Tolerance 5 min pour overhead (horloge non-monotone, latence DB).
    assert 175 <= mttr <= 185, (
        f"INV-018b #78 [C] : MTTR doit etre calcule sur "
        f"(updated_at - original_created_at) = ~180 min (3h). "
        f"Got mttr_minutes={mttr}. "
        f"Si ~60 : le code utilise (updated_at - created_at) ou created_at a ete "
        f"reset par escalade simulee → MTTR sous-evalue, KPI ne reflete plus la "
        f"realite operationnelle. Voir backend/app/api/stats.py L143."
    )


def test_inv018b_kpi_period_filter_uses_original_created_at(client, admin_headers):
    """INV-018b stats : filtre `Alarm.created_at >= since` doit etre sur
    `original_created_at`.

    Scenario : alarme creee il y a 10 semaines, escaladee aujourd'hui (le
    code de prod reset created_at a `now`). Avec un filtre KPI sur 4
    semaines, since = now - 4 semaines :
      - Bug : filter created_at (now) >= since (now-4w) → True → incluse
      - Fix : filter original_created_at (now-10w) >= since (now-4w) → False → exclue

    L'inclusion buggy fausse le total_alarms et toutes les metriques agregees
    (taux d'escalade, MTTR, top recurrentes).
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    alarm_id = _create_alarm(client, user1_headers, user1_id, "INV-018b stats period")

    # +10 semaines puis force created_at au now → escalade ultra tardive
    _advance_clock_minutes(client, 10 * 7 * 24 * 60)
    from backend.app.clock import now as clock_now
    now_after = clock_now()
    _force_alarm_state(alarm_id, created_at=now_after)

    # weeks=4 → since = now - 4 semaines
    r = client.get(
        "/api/stats/kpi",
        params={"weeks": 4, "hors_heures_only": False},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    kpi = r.json()

    assert kpi.get("total_alarms") == 0, (
        f"INV-018b #78 [C] : alarme avec original_created_at il y a 10 semaines "
        f"doit etre EXCLUE de la fenetre weeks=4 (since=now-4w). "
        f"Got total_alarms={kpi.get('total_alarms')}. "
        f"Si > 0 : le filtre query utilise `Alarm.created_at` (reset par escalade) "
        f"au lieu de `Alarm.original_created_at` → toutes les agregations sont "
        f"contaminees. Voir backend/app/api/stats.py L108."
    )

    # Sanity : avec weeks=12 (fenetre plus large), l'alarme apparait bien
    # (preuve que la mise en scene de l'escalade tardive a marche et qu'il
    # y a bien UNE alarme en DB).
    r = client.get(
        "/api/stats/kpi",
        params={"weeks": 12, "hors_heures_only": False},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    kpi_wide = r.json()
    assert kpi_wide.get("total_alarms") >= 1, (
        f"sanity : avec weeks=12 (fenetre couvrant les 10 semaines de "
        f"l'original_created_at), l'alarme doit apparaitre. Got "
        f"total_alarms={kpi_wide.get('total_alarms')}. Si 0 : la mise en scene "
        f"de l'escalade tardive a echoue."
    )


def test_inv018b_kpi_hors_heures_filter_uses_original_created_at(client, admin_headers):
    """INV-018b stats : filtre `hors_heures_only=True` doit s'appliquer sur
    `original_created_at` (L127), pas sur `created_at` (reset par escalade).

    Issue #141 : trou de couverture identifie en post-mortem de PR #139.
    Les 3 tests existants exercent L113 (filtre periode), L124 (bucketing) et
    L150 (MTTR), mais aucun ne passe `hors_heures_only=True`. Un mutant qui
    reverte UNIQUEMENT L127 a `a.created_at` survivrait en tier 2.

    Scenario :
      - original_created_at = dimanche 14:00 (weekend → hors heures ouvrees)
      - created_at = mercredi 10:00 (lun-ven 8-12h → heures ouvrees)
        Simule le reset operee par escalation_loop quand l'alarme escalade
        plusieurs jours apres son declenchement initial.

    Avec hors_heures_only=True :
      - Fix (L127 = original_created_at) : dimanche → hors heures → INCLUSE
      - Bug (L127 = created_at)         : mercredi 10h → heures ouvrees → EXCLUSE

    Verifie aussi (cross-check) qu'avec hors_heures_only=False l'alarme
    apparait dans le meme bucket — preuve que c'est bien le filtre L127 qui
    tranche, pas le bucketing L124 ni le filtre periode L113.
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    alarm_id = _create_alarm(client, user1_headers, user1_id, "INV-018b stats hors_heures")

    # Construit deux timestamps stables, independants du jour ou tourne le test :
    #   sunday_dt  = un dimanche 14:00, au moins 14 jours dans le passe
    #   wed_dt     = le mercredi suivant ce dimanche (sunday + 3 jours), 10:00
    # Les deux restent dans la fenetre weeks=8 (8*7=56 jours).
    from backend.app.clock import now as clock_now
    now_dt = clock_now()
    anchor = now_dt - timedelta(days=14)
    # Python weekday(): lundi=0..dimanche=6. Recule jusqu'au dimanche <= anchor.
    days_back_to_sunday = (anchor.weekday() + 1) % 7
    sunday_dt = (anchor - timedelta(days=days_back_to_sunday)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    wed_dt = (sunday_dt + timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )

    # Sanity de l'echafaudage : sinon le test prouverait n'importe quoi
    # (ex : si sunday_dt tombe par accident un jour ferie un mercredi 10h apres
    # un decalage, _est_hors_heures_ouvrees pourrait inverser le verdict).
    from backend.app.api.stats import _est_hors_heures_ouvrees
    assert _est_hors_heures_ouvrees(sunday_dt), (
        f"sanity : sunday_dt={sunday_dt} doit etre hors heures ouvrees"
    )
    assert not _est_hors_heures_ouvrees(wed_dt), (
        f"sanity : wed_dt={wed_dt} doit etre dans heures ouvrees "
        f"(mercredi 10h, hors jour ferie). Si jour ferie tombe : ajuster "
        f"l'offset (anchor) pour eviter la collision."
    )

    _force_alarm_state(
        alarm_id,
        original_created_at=sunday_dt,
        created_at=wed_dt,
    )

    # Cas 1 : hors_heures_only=True → L127 applique le filtre
    r = client.get(
        "/api/stats/kpi",
        params={"weeks": 8, "hors_heures_only": True},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    kpi_filtered = r.json()
    total_filtered = sum(w["total"] for w in kpi_filtered["weeks"])
    assert total_filtered >= 1, (
        f"INV-018b #141 [L] L127 : alarme avec original_created_at=dimanche "
        f"({sunday_dt}, hors heures) doit etre incluse avec hors_heures_only=True. "
        f"Got total sur 8 buckets={total_filtered}. "
        f"Si 0 : le filtre L127 utilise `a.created_at` (mercredi 10h, heures "
        f"ouvrees, reset par escalade) au lieu de `a.original_created_at`. "
        f"Voir backend/app/api/stats.py L127."
    )

    # Cas 2 (cross-check) : hors_heures_only=False → meme alarme apparait
    # → prouve que c'est bien le filtre hors_heures (L127) qui tranche,
    # pas un autre filtre amont (periode L113 ou bucketing L124).
    r = client.get(
        "/api/stats/kpi",
        params={"weeks": 8, "hors_heures_only": False},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    kpi_unfiltered = r.json()
    total_unfiltered = sum(w["total"] for w in kpi_unfiltered["weeks"])
    assert total_unfiltered >= 1, (
        f"sanity : sans filtre hors_heures, l'alarme staged doit apparaitre. "
        f"Got total={total_unfiltered}. Si 0 : echec de la mise en scene "
        f"(timestamps hors fenetre weeks=8 ?) — pas une preuve de L127."
    )
