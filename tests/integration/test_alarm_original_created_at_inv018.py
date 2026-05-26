"""
Tier 2 integration tests : INV-018 — `Alarm.original_created_at` immuable.

Source : tests/INVARIANTS.md INV-018 [C] 🐛
  "Nouveau champ `original_created_at` initialise a la creation et jamais
   modifie. `created_at` continue d'etre utilise comme timer d'escalade."

Pourquoi : `created_at` est doublement utilise (date originale + timer escalade
remis a zero a chaque palier). Une alarme escaladee 2h apres creation apparait
"il y a 2min" dans l'historique. Sous-issue #76 ajoute le champ + ecriture a la
creation. Les lectures (filtres, ORDER BY, schemas, stats) sont des sous-issues
suivantes (INV-018b) — HORS scope ici.

Verification anti-figeage du bug (P5 strict) : sans le fix (colonne absente du
modele ou jamais settee), les 5 tests echouent.
  - test_set_at_creation : AttributeError sur alarm.original_created_at (colonne
    absente) ou IntegrityError (NOT NULL viole) — couvre /api/alarms/send
  - test_immutable_after_escalation : meme AttributeError au pre-record —
    couvre /api/alarms/send + trigger_escalation
  - test_uses_clock_now : meme AttributeError + comportement clock-dependant
    non verifiable — couvre /api/alarms/send
  - test_oncall_heartbeat_sets_original_created_at : couvre _apply_oncall_heartbeat
    (escalation.py) — 4e call site Alarm()
  - test_gateway_trigger_alarm_sets_original_created_at : couvre trigger_alarm
    (alarms_internal.py) — 5e call site Alarm()

L'invariant doit tenir sur TOUS les chemins de creation. Ces 5 tests couvrent
les 4 call sites Alarm() recenses : alarms.py::send_alarm,
alarms_internal.py::trigger_alarm, test_api.py::send_test_alarm (par symetrie
de pattern), escalation.py::_apply_oncall_heartbeat.

Budget P4 : 5 tests (3 send_alarm + 1 oncall + 1 gateway).
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
    # peer=false : pas de propagation cluster en tier 2 (instance unique).
    r = client.post("/api/test/reset-clock", params={"peer": "false"})
    assert r.status_code == 200, r.text


def _advance_clock_minutes(client, minutes: float):
    r = client.post(
        "/api/test/advance-clock",
        params={"minutes": minutes, "peer": "false"},
    )
    assert r.status_code == 200, r.text


def _fetch_alarm_orm(alarm_id: int):
    """Charge l'alarme via SessionLocal pour acceder a `original_created_at`,
    qui n'est PAS expose dans AlarmResponse (hors-scope sous-issue suivante)."""
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm
    db = SessionLocal()
    try:
        alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
        # Force le chargement des attributs avant close (sinon DetachedInstanceError).
        if alarm is not None:
            return {
                "id": alarm.id,
                "status": alarm.status,
                "escalation_count": alarm.escalation_count,
                "created_at": alarm.created_at,
                "original_created_at": alarm.original_created_at,
                "is_oncall_alarm": alarm.is_oncall_alarm,
            }
        return None
    finally:
        db.close()


def test_original_created_at_set_at_creation(client, admin_headers):
    """INV-018 : a la creation, original_created_at != None et == created_at.

    Le critere de succes du sous-issue stipule explicitement :
      `alarm.original_created_at == alarm.created_at` (identiques au temps t0).
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    try:
        r = client.post(
            "/api/alarms/send",
            json={
                "title": "INV-018 set-at-creation",
                "message": "verifie ecriture initiale original_created_at",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        alarm_id = r.json()["id"]

        snap = _fetch_alarm_orm(alarm_id)
        assert snap is not None, f"alarme {alarm_id} introuvable apres send"

        assert snap["original_created_at"] is not None, (
            "INV-018 : original_created_at doit etre rempli a la creation "
            "(NOT NULL au niveau modele, fige t0)"
        )
        assert snap["original_created_at"] == snap["created_at"], (
            f"INV-018 : a la creation, original_created_at ({snap['original_created_at']}) "
            f"doit etre egal a created_at ({snap['created_at']}). "
            f"Sans le fix, le champ est NULL ou pris a un instant different."
        )
    finally:
        _reset_clock(client)
        try:
            client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_headers)
        except Exception:
            pass


def test_original_created_at_immutable_after_escalation(client, admin_headers):
    """INV-018 : apres une escalade (et un ack), original_created_at est inchange.

    Sequence :
      1. POST /alarms/send -> alarme A creee, on capture t0 = original_created_at.
      2. advance-clock +20min, POST /test/trigger-escalation -> escalation_count >= 1
         (sanity : l'escalade a bien eu lieu).
      3. POST /alarms/{A}/ack -> mute status, suspended_until, etc.
      4. Verifie : alarm.original_created_at == t0 (UNCHANGED par ces 2 operations).

    Sans le fix, le champ n'existe pas et le pre-record echoue. Avec le fix, le
    champ existe et n'est ecrit nulle part en dehors de la creation.
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_token = _login(client, "user1", "user123")
    user1_headers = {"Authorization": f"Bearer {user1_token}"}

    try:
        r = client.post(
            "/api/alarms/send",
            json={
                "title": "INV-018 immutable",
                "message": "verifie immuabilite apres escalade+ack",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        alarm_id = r.json()["id"]

        snap0 = _fetch_alarm_orm(alarm_id)
        t0 = snap0["original_created_at"]
        assert t0 is not None, "INV-018 : original_created_at doit etre set a la creation"

        _advance_clock_minutes(client, 20)
        r = client.post("/api/test/trigger-escalation")
        assert r.status_code == 200, r.text

        snap1 = _fetch_alarm_orm(alarm_id)
        assert snap1["escalation_count"] >= 1, (
            f"sanity: l'escalade doit avoir eu lieu, "
            f"got escalation_count={snap1['escalation_count']}"
        )
        assert snap1["original_created_at"] == t0, (
            f"INV-018 : original_created_at doit etre IMMUABLE. "
            f"Apres escalade, attendu {t0}, got {snap1['original_created_at']}."
        )

        # Notifier user2 (assigne par l'escalade) doit ack
        # On essaie d'abord user1 (notifie initial), sinon admin (admin est dans
        # la chaine en position 3, donc pas force notifie au 1er escalade ; mais
        # user1 reste notifie cumulativement, donc OK).
        r = client.post(f"/api/alarms/{alarm_id}/ack", headers=user1_headers)
        assert r.status_code == 200, f"ack failed: {r.status_code} {r.text}"

        snap2 = _fetch_alarm_orm(alarm_id)
        assert snap2["status"] == "acknowledged", (
            f"sanity: alarme doit etre ack, got status={snap2['status']}"
        )
        assert snap2["original_created_at"] == t0, (
            f"INV-018 : original_created_at doit rester IMMUABLE apres ack. "
            f"Attendu {t0}, got {snap2['original_created_at']}."
        )
    finally:
        _reset_clock(client)
        try:
            client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_headers)
        except Exception:
            pass


def test_original_created_at_uses_clock_now(client, admin_headers):
    """INV-018 : original_created_at utilise clock_now() (et donc l'offset de test).

    Sans clock_now(), un advance-clock prealable a la creation n'aurait aucun
    effet sur original_created_at (qui suivrait datetime.utcnow reel). Avec
    clock_now(), l'offset est applique.

    Sequence :
      1. Reset clock (offset=0).
      2. Capture real_now = datetime.utcnow() (instant reel, sans offset).
      3. POST /test/advance-clock?minutes=120 -> offset = +2h.
      4. POST /alarms/send.
      5. Verifie : original_created_at - real_now >= 119 minutes (preuve que
         l'offset a ete applique, marge d'1min pour la latence des appels).

    Sans le fix (alternative `datetime.utcnow()` directe), la valeur serait
    real_now exactement et la difference serait ~0 -> test FAIL (RED).
    """
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    user1_id = _user_id(client, admin_headers, "user1")
    user1_headers = {"Authorization": f"Bearer {_login(client, 'user1', 'user123')}"}

    try:
        real_now = datetime.utcnow()
        _advance_clock_minutes(client, 120)

        r = client.post(
            "/api/alarms/send",
            json={
                "title": "INV-018 clock_now",
                "message": "verifie usage clock_now (vs datetime.utcnow)",
                "severity": "critical",
                "assigned_user_id": user1_id,
            },
            headers=user1_headers,
        )
        assert r.status_code == 200, r.text
        alarm_id = r.json()["id"]

        snap = _fetch_alarm_orm(alarm_id)
        delta = snap["original_created_at"] - real_now

        assert delta >= timedelta(minutes=119), (
            f"INV-018 : original_created_at doit utiliser clock_now() (avec offset). "
            f"Apres advance-clock +120min, original_created_at - real_now = {delta}, "
            f"attendu >= 119min. Si delta ~= 0, le code utilise datetime.utcnow() "
            f"au lieu de clock_now() -> bypass de l'horloge injectable (cf INV-066)."
        )
        assert delta <= timedelta(minutes=121), (
            f"INV-018 : original_created_at trop loin dans le futur "
            f"(delta={delta}, attendu <= 121min). Probablement un cumul d'offsets "
            f"d'un test precedent — verifier le reset-clock dans le finally."
        )
    finally:
        _reset_clock(client)
        try:
            client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_headers)
        except Exception:
            pass


def test_inv018_oncall_heartbeat_sets_original_created_at(client, admin_headers):
    """INV-018 : couvre le 4e call site `Alarm(...)` — `_apply_oncall_heartbeat`.

    Pattern reutilise de tests/integration/test_oncall_delay_config.py
    (test_oncall_delay_read_from_system_config_not_hardcoded) :
      1. Reduit oncall_offline_delay_minutes a 2 pour declencher rapidement
      2. user1 (pos 1, de garde) offline depuis 3 min, user2 online
      3. Appel direct de _apply_oncall_heartbeat(db, now, chain)
      4. Verifie l'alarme oncall creee : original_created_at non NULL, ==
         created_at, et dans [before_call, after_call] (preuve clock_now()
         appele a l'interieur de la branche INV-050 du loop)

    Sans le fix sur escalation.py, le constructeur Alarm() omettait
    original_created_at -> IntegrityError (NOT NULL viole) car la colonne est
    NOT NULL au niveau modele.
    """
    from backend.app.clock import now as clock_now
    from backend.app.database import SessionLocal
    import asyncio
    from backend.app.escalation import _apply_oncall_heartbeat
    from backend.app.models import Alarm, EscalationConfig, User

    _reset_clock(client)

    r = client.post(
        "/api/config/system",
        json={"key": "oncall_offline_delay_minutes", "value": "2"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    db = SessionLocal()
    created_alarm_id = None
    try:
        chain = db.query(EscalationConfig).order_by(EscalationConfig.position).all()
        assert len(chain) >= 2, (
            f"seed chain should have >=2 entries (user1 pos1, user2 pos2), "
            f"got {[(c.position, c.user_id) for c in chain]}"
        )
        pos1_user_id = chain[0].user_id
        pos2_user_id = chain[1].user_id

        active = (
            db.query(Alarm)
            .filter(Alarm.status.in_(["active", "escalated"]))
            .all()
        )
        for a in active:
            a.status = "resolved"
        db.commit()

        now_ref = clock_now()
        u1 = db.query(User).filter(User.id == pos1_user_id).first()
        u2 = db.query(User).filter(User.id == pos2_user_id).first()
        assert u1 is not None and u2 is not None
        u1.is_online = False
        u1.last_heartbeat = now_ref - timedelta(minutes=3)
        u2.is_online = True
        u2.last_heartbeat = now_ref
        db.commit()

        before = clock_now()
        # Bug #105 : _apply_oncall_heartbeat est devenu async (envoi SMTP via
        # asyncio.to_thread pour ne pas geler l'event loop). On le pilote ici
        # via asyncio.run depuis un contexte de test sync.
        asyncio.run(_apply_oncall_heartbeat(db, now_ref, chain))
        after = clock_now()
        db.expire_all()

        oncall_alarm = (
            db.query(Alarm)
            .filter(
                Alarm.is_oncall_alarm == True,  # noqa: E712 (SQLAlchemy)
                Alarm.status.in_(["active", "escalated"]),
            )
            .order_by(Alarm.id.desc())
            .first()
        )
        assert oncall_alarm is not None, (
            "sanity : _apply_oncall_heartbeat doit creer une alarme oncall "
            "avec user1 offline 3min > delay 2min (cf test_oncall_delay_config)"
        )
        created_alarm_id = oncall_alarm.id

        assert oncall_alarm.original_created_at is not None, (
            "INV-018 (4e call site, escalation.py:_apply_oncall_heartbeat) : "
            "original_created_at doit etre rempli a la creation de l'alarme "
            "oncall. Sans le fix, ce champ est NULL -> IntegrityError."
        )
        assert oncall_alarm.original_created_at == oncall_alarm.created_at, (
            f"INV-018 : a la creation d'une alarme oncall, original_created_at "
            f"({oncall_alarm.original_created_at}) doit etre identique a "
            f"created_at ({oncall_alarm.created_at})."
        )
        assert before <= oncall_alarm.original_created_at <= after, (
            f"INV-018 : original_created_at ({oncall_alarm.original_created_at}) "
            f"doit etre dans la fenetre [{before}, {after}] de l'appel a "
            f"_apply_oncall_heartbeat — preuve que clock_now() a ete utilise."
        )
    finally:
        if created_alarm_id is not None:
            a = db.query(Alarm).filter(Alarm.id == created_alarm_id).first()
            if a is not None:
                a.status = "resolved"
        for user in db.query(User).all():
            user.is_online = True
            user.last_heartbeat = clock_now()
        db.commit()
        db.close()

        client.post(
            "/api/config/system",
            json={"key": "oncall_offline_delay_minutes", "value": "15"},
            headers=admin_headers,
        )
        _reset_clock(client)


def test_inv018_gateway_report_state_alarm_sets_original_created_at(
    client, admin_headers, monkeypatch
):
    """INV-018 : couvre le 5e call site `Alarm(...)` — `_create_gateway_alarm`
    appelée depuis `report_state` (alarms_internal.py, INV-120 V2 contact sec
    NC local, refonte issue #112).

    Pattern :
      1. Force GATEWAY_KEY env var (le check lit os.getenv() a chaque request)
      2. POST /internal/alarms/report-state avec state="open" + X-Gateway-Key valide
      3. Verifie l'alarme creee : original_created_at non NULL, == created_at,
         et dans [before_call, after_call] (preuve clock_now() appele a
         l'interieur de _create_gateway_alarm)

    Sans le fix sur alarms_internal.py, le constructeur Alarm() omettait
    original_created_at -> IntegrityError au flush() car la colonne est
    NOT NULL au niveau modele.
    """
    from backend.app.clock import now as clock_now

    GATEWAY_KEY = "test-gateway-key-INV-018"
    monkeypatch.setenv("GATEWAY_KEY", GATEWAY_KEY)

    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    alarm_id = None
    try:
        before = clock_now()
        r = client.post(
            "/internal/alarms/report-state",
            headers={"X-Gateway-Key": GATEWAY_KEY},
            json={"gateway_id": "inv018-onsite-1", "state": "open"},
        )
        after = clock_now()
        assert r.status_code == 200, (
            f"INV-120 V2 sanity : report-state gateway avec cle valide doit "
            f"reussir, got {r.status_code} {r.text}"
        )
        assert r.json().get("alarm_active") is True, (
            f"INV-120 V2 : 1 gateway 'open' → alarm_active=True, got {r.json()}"
        )

        # Récupérer l'alarme nouvellement créée (response ne contient pas l'id)
        rg = client.get("/api/alarms/active", headers=admin_headers)
        assert rg.status_code == 200
        active = rg.json()
        assert len(active) == 1, f"INV-120 V2 : 1 alarme attendue, got {active}"
        alarm_id = active[0]["id"]

        snap = _fetch_alarm_orm(alarm_id)
        assert snap is not None, f"alarme {alarm_id} introuvable apres report-state"

        assert snap["original_created_at"] is not None, (
            "INV-018 (5e call site, alarms_internal.py:_create_gateway_alarm) : "
            "original_created_at doit etre rempli a la creation de l'alarme "
            "gateway. Sans le fix, le champ est NULL -> IntegrityError au flush."
        )
        assert snap["original_created_at"] == snap["created_at"], (
            f"INV-018 : a la creation gateway, original_created_at "
            f"({snap['original_created_at']}) doit etre identique a created_at "
            f"({snap['created_at']})."
        )
        assert before <= snap["original_created_at"] <= after, (
            f"INV-018 : original_created_at ({snap['original_created_at']}) "
            f"doit etre dans [{before}, {after}] de l'appel POST /internal/"
            f"alarms/report-state — preuve que clock_now() a ete utilise (vs datetime.utcnow)."
        )
    finally:
        _reset_clock(client)
        if alarm_id is not None:
            try:
                client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_headers)
            except Exception:
                pass
