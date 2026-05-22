"""
Tier 2 integration tests : INV-018b — Lectures historiques utilisent
`original_created_at`, PAS `created_at` (qui est un timer interne).

Source : tests/INVARIANTS.md INV-018b [C] 🐛
  "Toute lecture historique utilise original_created_at. Une alarme escaladee
   2h apres creation apparait 'il y a 2min' dans l'historique."

Scope sous-issue #77 :
  - backend/app/api/alarms.py:131 — filtre /alarms/?days=N
  - backend/app/api/alarms.py:132 — ORDER BY pour /
  - backend/app/api/alarms.py:146 — ORDER BY pour /active
  - backend/app/schemas.py:79 — AlarmResponse expose original_created_at

Hors scope : stats KPI (sous-issue #78), frontend (human-required), usages
timer dans escalation.py/calls.py (a laisser tels quels, cf INVARIANTS.md
"Seul usage qui garde created_at").

Strategie RED : on simule le "reset created_at" que fait escalation_loop en
ack-expiry (escalation.py:186) et escalade (escalation.py:274), en
manipulant directement la colonne via SessionLocal. C'est legitime en tier 2
car ces tests verifient les LECTURES, pas le mecanisme de reset (couvert
ailleurs). /api/test/trigger-escalation ne reset pas created_at — d'ou la
necessite du fetch ORM direct.

Budget P4 : 3 tests (filtre /days, ORDER BY /active, exposition schema).
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


def _force_set_created_at(alarm_id: int, new_value: datetime):
    """Simule le reset de `created_at` que fait escalation_loop (ack-expiry
    escalation.py:186 et escalade escalation.py:274). N'affecte PAS
    `original_created_at`. Pattern : SessionLocal direct (cf
    test_alarm_original_created_at_inv018.py:_fetch_alarm_orm)."""
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm
    db = SessionLocal()
    try:
        alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
        assert alarm is not None, f"alarme {alarm_id} introuvable pour _force_set_created_at"
        alarm.created_at = new_value
        db.commit()
    finally:
        db.close()


def _force_set_status(alarm_id: int, new_status: str):
    """Bascule le status d'une alarme via SessionLocal direct, pour bypasser
    l'invariant applicatif '1 alarme active a la fois' (alarms.py:44-46, 409
    Conflict) quand on a besoin de 2 alarmes simultanees pour tester un
    ORDER BY. Filtre /active accepte status in ('active', 'escalated')."""
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm
    db = SessionLocal()
    try:
        alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
        assert alarm is not None, f"alarme {alarm_id} introuvable pour _force_set_status"
        alarm.status = new_status
        db.commit()
    finally:
        db.close()


def _fetch_alarm_orm(alarm_id: int):
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm
    db = SessionLocal()
    try:
        alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
        if alarm is None:
            return None
        return {
            "id": alarm.id,
            "created_at": alarm.created_at,
            "original_created_at": alarm.original_created_at,
        }
    finally:
        db.close()


def test_inv018b_alarms_days_filter_uses_original_created_at(client, admin_headers):
    """INV-018b : GET /alarms/?days=N filtre sur `original_created_at`,
    PAS sur `created_at` (qui est un timer interne).

    Scenario du bug : une alarme creee il y a 10 jours est escaladee
    aujourd'hui. Le code de prod (escalation_loop) reset alarm.created_at a
    `now` lors de l'escalade. Si /alarms/?days=N filtre sur `created_at`, une
    fenetre etroite (days=7) inclut a tort cette alarme alors que son
    evenement original est ancien.

    Sequence :
      1. POST /alarms/send a t=0 → original_created_at = created_at = t0
      2. advance-clock +10 jours → now = t0 + 10d
      3. Force created_at = now (simule reset par escalade tardive)
         → alarme : original_created_at = t0, created_at = t0 + 10d
      4. GET /alarms/?days=7 → since = now - 7d = t0 + 3d
         - Bug : filter created_at (t0+10d) >= since (t0+3d) → True → inclus ❌
         - Fix : filter original_created_at (t0) >= since (t0+3d) → False → exclu ✅
      5. Sanity GET /alarms/?days=15 → since = now - 15d = t0 - 5d
         - Avec fix : filter original_created_at (t0) >= since (t0-5d) → True → inclus ✅

    Sans le fix (master actuel), step 4 retourne l'alarme → le test FAIL.
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    alarm_id = None
    try:
        r = client.post(
            "/api/alarms/send",
            json={
                "title": "INV-018b days-filter",
                "message": "verifie filtre /alarms/?days=N sur original_created_at",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        alarm_id = r.json()["id"]

        _advance_clock_minutes(client, 10 * 24 * 60)  # +10 jours

        from backend.app.clock import now as clock_now
        now_after_advance = clock_now()
        _force_set_created_at(alarm_id, now_after_advance)

        # Sanity: divergence effective (preuve que la mise en scene a marche)
        snap = _fetch_alarm_orm(alarm_id)
        assert snap["original_created_at"] != snap["created_at"], (
            f"sanity setup : created_at devait etre force a {now_after_advance}, "
            f"original_created_at devait rester a t0, got identical "
            f"{snap['original_created_at']}"
        )
        delta_days = (snap["created_at"] - snap["original_created_at"]).total_seconds() / 86400
        assert 9.9 <= delta_days <= 10.1, (
            f"sanity setup : ecart attendu ~10 jours, got {delta_days:.2f}"
        )

        # Etape clé : filtre sur 7 jours doit EXCLURE l'alarme (date originale 10j)
        r = client.get("/api/alarms/?days=7", headers=user1_headers)
        assert r.status_code == 200, r.text
        ids_in_7days = {a["id"] for a in r.json()}
        assert alarm_id not in ids_in_7days, (
            f"INV-018b : GET /alarms/?days=7 doit filtrer sur original_created_at. "
            f"L'alarme {alarm_id} a original_created_at il y a ~10 jours (hors fenetre 7j) "
            f"mais created_at il y a 0s (a cause du reset escalade simule). "
            f"Si elle apparait, c'est que le filtre utilise created_at -> bug INV-018b non fixe. "
            f"got ids dans /days=7 : {ids_in_7days}"
        )

        # Sanity : avec une fenetre suffisamment large (15j), l'alarme doit apparaitre
        r = client.get("/api/alarms/?days=15", headers=user1_headers)
        assert r.status_code == 200, r.text
        ids_in_15days = {a["id"] for a in r.json()}
        assert alarm_id in ids_in_15days, (
            f"sanity : avec days=15, l'alarme (original 10j) doit apparaitre. "
            f"got ids : {ids_in_15days}"
        )
    finally:
        _reset_clock(client)
        if alarm_id is not None:
            try:
                client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_headers)
            except Exception:
                pass


def test_inv018b_list_order_by_original_created_at(client, admin_headers):
    """INV-018b : GET /alarms/?days=N ORDER BY `original_created_at` DESC,
    PAS `created_at`.

    Scenario : 2 alarmes resolved dans la fenetre. A creee en premier (plus
    vieille), B creee apres. A est ensuite escaladee tardivement (simule
    reset created_at). Sans fix, A apparait en premier dans l'historique
    (created_at recent). Avec fix, B est en premier (date originale plus
    recente).

    Note : on ne peut PAS tester /active ORDER BY directement car
    l'invariant "1 seule alarme active a la fois" (alarms.py:44-46, 409
    Conflict) empeche d'en avoir 2 simultanees via API. La logique ORDER BY
    est identique entre /alarms/?days=N (ligne 132) et /alarms/active
    (ligne 146) — fixer l'une signifie fixer l'autre dans le meme commit.

    Sequence :
      1. POST send → alarme A (t = t0), puis resolve
      2. advance +5min, POST send → alarme B (t = t0+5min), puis resolve
         → A: orig=t0, created=t0 ; B: orig=t0+5min, created=t0+5min
      3. Force A.created_at = t0+20min (simule reset escalade tardive)
         → A: orig=t0, created=t0+20min (recent) ; B: orig=t0+5min, created=t0+5min
      4. GET /alarms/?days=10 :
         - Bug ORDER BY created_at DESC : [A(20min), B(5min)] → A premier ❌
         - Fix ORDER BY original_created_at DESC : [B(5min), A(0)] → B premier ✅

    Sans le fix, response[0]["id"] == A.id → test FAIL.
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    alarm_a_id = None
    alarm_b_id = None
    try:
        # Alarme A : creee a t0, puis resolved pour liberer le slot "1 active"
        r = client.post(
            "/api/alarms/send",
            json={
                "title": "INV-018b A old",
                "message": "creee en premier",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        alarm_a_id = r.json()["id"]

        r = client.post(f"/api/alarms/{alarm_a_id}/resolve", headers=admin_headers)
        assert r.status_code == 200, f"resolve A failed: {r.status_code} {r.text}"

        _advance_clock_minutes(client, 5)

        # Alarme B : creee a t0+5min, puis resolved aussi
        r = client.post(
            "/api/alarms/send",
            json={
                "title": "INV-018b B newer",
                "message": "creee apres A",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        alarm_b_id = r.json()["id"]

        r = client.post(f"/api/alarms/{alarm_b_id}/resolve", headers=admin_headers)
        assert r.status_code == 200, f"resolve B failed: {r.status_code} {r.text}"

        # Force A.created_at a un instant posterieur a B (simule reset par
        # escalade tardive de A apres creation de B).
        from backend.app.clock import now as clock_now
        a_reset_to = clock_now() + timedelta(minutes=15)
        _force_set_created_at(alarm_a_id, a_reset_to)

        # Sanity setup : divergence effective
        snap_a = _fetch_alarm_orm(alarm_a_id)
        snap_b = _fetch_alarm_orm(alarm_b_id)
        assert snap_a["created_at"] > snap_b["created_at"], (
            f"sanity setup : A.created_at ({snap_a['created_at']}) doit etre "
            f"posterieur a B.created_at ({snap_b['created_at']})"
        )
        assert snap_a["original_created_at"] < snap_b["original_created_at"], (
            f"sanity setup : A.original ({snap_a['original_created_at']}) doit "
            f"etre anterieur a B.original ({snap_b['original_created_at']})"
        )

        # Etape clé : ORDER BY doit etre sur original_created_at DESC
        r = client.get("/api/alarms/?days=10", headers=user1_headers)
        assert r.status_code == 200, r.text
        history = r.json()
        ids = [a["id"] for a in history]
        assert alarm_a_id in ids and alarm_b_id in ids, (
            f"sanity : les 2 alarmes doivent etre dans /days=10, got {ids}"
        )
        # B doit etre AVANT A dans la liste (B plus recent en date originale)
        idx_a = ids.index(alarm_a_id)
        idx_b = ids.index(alarm_b_id)
        assert idx_b < idx_a, (
            f"INV-018b : GET /alarms/?days=10 doit etre trie ORDER BY "
            f"original_created_at DESC. B (id={alarm_b_id}, "
            f"original={snap_b['original_created_at']}) doit etre AVANT "
            f"A (id={alarm_a_id}, original={snap_a['original_created_at']}, "
            f"created post-reset={snap_a['created_at']}). Got order : {ids}. "
            f"Si A avant B, le tri utilise created_at -> bug INV-018b non fixe."
        )
        # Verif zero-cost : exposition `original_created_at` aussi via /?days=N.
        # Test 3 prouve l'exposition sur POST /send et GET /active. Cette
        # assertion ajoute le 3eme endpoint /?days=N a la surface verrouillee.
        assert "original_created_at" in history[0], (
            f"INV-018b : GET /alarms/?days=N doit exposer 'original_created_at'. "
            f"Keys : {sorted(history[0].keys())}."
        )
    finally:
        _reset_clock(client)
        for aid in (alarm_a_id, alarm_b_id):
            if aid is not None:
                try:
                    client.post(f"/api/alarms/{aid}/resolve", headers=admin_headers)
                except Exception:
                    pass


def test_inv018b_active_order_by_original_created_at(client, admin_headers):
    """INV-018b : GET /alarms/active ORDER BY `original_created_at` DESC,
    PAS `created_at`. Tueur direct du mutant `alarms.py:149
    .order_by(original_created_at.desc())` → `.order_by(created_at.desc())`.

    L'invariant applicatif 'une seule alarme active a la fois' (alarms.py:44-46
    HTTP 409) bloque la creation de 2 alarmes simultanees via API. Le filtre
    /active accepte status in ('active', 'escalated') (ligne 148) — donc
    basculer A en 'escalated' ne suffit PAS (escalated est aussi dans la liste
    bloquante au POST). On bascule temporairement A en 'acknowledged' (hors
    liste bloquante au POST mais aussi hors filtre /active) pour creer B, puis
    on restore A en 'escalated' pour la rendre visible dans /active.

    Sequence :
      1. POST send → alarme A (t = t0, status='active')
      2. Force A.status = 'acknowledged' (libere le slot bloquant au POST send)
      3. advance +5min, POST send → alarme B (t = t0+5min, status='active')
      4. Restore A.status = 'escalated' (la rend visible dans /active)
         → A: orig=t0, created=t0, escalated ; B: orig=t0+5min, created=t0+5min, active
      5. Force A.created_at = clock_now() + 15min (simule reset escalade tardive)
         → A: orig=t0, created=t0+20min ; B: orig=t0+5min, created=t0+5min
      6. GET /alarms/active :
         - Bug ORDER BY created_at DESC : [A(20min), B(5min)] → A premier ❌
         - Fix ORDER BY original_created_at DESC : [B(5min), A(0)] → B premier ✅

    Tue le mutant identifie par le trancheur de la review locale. Sans le
    fix sur la ligne 149, response[0]["id"] == A.id → test FAIL.
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    alarm_a_id = None
    alarm_b_id = None
    try:
        # Alarme A : creee a t0
        r = client.post(
            "/api/alarms/send",
            json={
                "title": "INV-018b /active A old",
                "message": "creee en premier",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        alarm_a_id = r.json()["id"]
        # Bascule A en 'acknowledged' : libere le slot bloquant 1-active au
        # POST send (qui verifie status in active/escalated, alarms.py:44).
        _force_set_status(alarm_a_id, "acknowledged")

        _advance_clock_minutes(client, 5)

        # Alarme B : creee a t0+5min, status='active' par defaut
        r = client.post(
            "/api/alarms/send",
            json={
                "title": "INV-018b /active B newer",
                "message": "creee apres A",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        alarm_b_id = r.json()["id"]

        # Restore A en 'escalated' : la rend visible dans /active (filter
        # accepte active OR escalated, alarms.py:148).
        _force_set_status(alarm_a_id, "escalated")

        # Force A.created_at posterieur a B (simule reset par escalade tardive)
        from backend.app.clock import now as clock_now
        a_reset_to = clock_now() + timedelta(minutes=15)
        _force_set_created_at(alarm_a_id, a_reset_to)

        # Sanity setup
        snap_a = _fetch_alarm_orm(alarm_a_id)
        snap_b = _fetch_alarm_orm(alarm_b_id)
        assert snap_a["created_at"] > snap_b["created_at"], (
            f"sanity : A.created_at ({snap_a['created_at']}) doit etre posterieur "
            f"a B.created_at ({snap_b['created_at']})"
        )
        assert snap_a["original_created_at"] < snap_b["original_created_at"], (
            f"sanity : A.original ({snap_a['original_created_at']}) doit etre "
            f"anterieur a B.original ({snap_b['original_created_at']})"
        )

        # Etape clé : GET /active retourne 2 alarmes (active + escalated) avec
        # ORDER BY original_created_at DESC → B avant A.
        r = client.get("/api/alarms/active", headers=user1_headers)
        assert r.status_code == 200, r.text
        active = r.json()
        assert len(active) == 2, (
            f"sanity : 2 alarmes attendues en /active (A escalated + B active), "
            f"got {len(active)} : {[(a['id'], a['status']) for a in active]}"
        )
        assert active[0]["id"] == alarm_b_id, (
            f"INV-018b (ligne 149) : GET /alarms/active doit ORDER BY "
            f"original_created_at DESC. B (id={alarm_b_id}, "
            f"original={snap_b['original_created_at']}) doit etre EN PREMIER. "
            f"Got order : {[(a['id'], a['status']) for a in active]}. Si A "
            f"(id={alarm_a_id}, original={snap_a['original_created_at']}, "
            f"created post-reset={snap_a['created_at']}) est en premier, le "
            f"tri utilise created_at -> mutant ligne 149 survit -> bug "
            f"INV-018b non fixe sur /active."
        )
        assert active[1]["id"] == alarm_a_id, (
            f"sanity : A doit etre en position 1, got {active[1]['id']}"
        )
    finally:
        _reset_clock(client)
        for aid in (alarm_a_id, alarm_b_id):
            if aid is not None:
                try:
                    client.post(f"/api/alarms/{aid}/resolve", headers=admin_headers)
                except Exception:
                    pass


def test_inv018b_alarm_response_exposes_original_created_at(client, admin_headers):
    """INV-018b : `AlarmResponse` expose le champ `original_created_at`
    (en plus de `created_at` pour ne pas casser les consumers existants —
    Android/frontend basculeront sur ce champ dans une issue separee).

    Sequence :
      1. POST /alarms/send → response.json() doit contenir "original_created_at"
         egal a "created_at" (a la creation, les deux sont identiques).
      2. Force created_at a un instant futur (simule reset escalade).
      3. GET /alarms/active → response[0] doit avoir "original_created_at" !=
         "created_at" (preuve que le champ est expose INDEPENDAMMENT, pas une
         copie de created_at).
      4. La valeur exposee de original_created_at doit etre celle d'origine
         (la valeur post-reset n'est PAS reflechie dans original_created_at).

    Sans le fix (master actuel), AlarmResponse n'a pas le champ
    "original_created_at" → KeyError sur le step 1 → test FAIL.
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    alarm_id = None
    try:
        r = client.post(
            "/api/alarms/send",
            json={
                "title": "INV-018b expose schema",
                "message": "verifie AlarmResponse.original_created_at",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        alarm_id = body["id"]

        # Etape 1 : le champ est present a la reponse de POST send
        assert "original_created_at" in body, (
            f"INV-018b : AlarmResponse doit exposer 'original_created_at'. "
            f"Keys actuelles : {sorted(body.keys())}. Si absent, schemas.py "
            f"AlarmResponse n'a pas le champ -> bug INV-018b non fixe."
        )
        assert body["original_created_at"] is not None, (
            "INV-018b : original_created_at doit etre une valeur non nulle a la "
            "creation (NOT NULL au niveau modele)."
        )
        assert body["original_created_at"] == body["created_at"], (
            f"INV-018b : a la creation, original_created_at "
            f"({body['original_created_at']}) doit etre identique a created_at "
            f"({body['created_at']})."
        )

        # Etape 2 : force divergence (simule reset par escalade)
        from backend.app.clock import now as clock_now
        new_created = clock_now() + timedelta(minutes=30)
        _force_set_created_at(alarm_id, new_created)

        # Etape 3 : GET /alarms/active expose le champ et il est independant
        r = client.get("/api/alarms/active", headers=user1_headers)
        assert r.status_code == 200, r.text
        active = r.json()
        assert len(active) == 1, f"sanity : 1 alarme attendue, got {len(active)}"
        a = active[0]
        assert "original_created_at" in a, (
            f"INV-018b : GET /alarms/active doit aussi exposer original_created_at. "
            f"Keys : {sorted(a.keys())}."
        )
        assert a["original_created_at"] != a["created_at"], (
            f"INV-018b : apres force_set_created_at, original_created_at "
            f"({a['original_created_at']}) doit etre DIFFERENT de created_at "
            f"({a['created_at']}). Si egal, le schema copie created_at au "
            f"lieu de lire la vraie valeur ORM -> exposition incorrecte."
        )
        # La valeur originale est preservee (== valeur a la creation)
        assert a["original_created_at"] == body["original_created_at"], (
            f"INV-018b : original_created_at doit etre IMMUABLE (cf INV-018). "
            f"Attendu {body['original_created_at']}, got {a['original_created_at']}."
        )
    finally:
        _reset_clock(client)
        if alarm_id is not None:
            try:
                client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_headers)
            except Exception:
                pass
