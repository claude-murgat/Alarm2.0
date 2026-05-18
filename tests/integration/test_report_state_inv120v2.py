"""
Tier 2 integration tests : INV-120 V2 (refonte) + INV-122 + INV-123.

Source : tests/INVARIANTS.md (section 13 V2, DRAFT 2026-05-18) + issue #112.

Couverts ici (7 tests, budget P4 calibré pour 3 nouveaux INV) :
  - INV-120 / INV-065 : auth X-Gateway-Key obligatoire → 401 sans clé
  - INV-120 : POST /report-state state=open sans alarme → CREATE alarme
              `source="gateway_dry_contact"`
  - INV-120 : POST /report-state state=open quand alarme gateway active → no-op
              (idempotence, pas de 409 — le gateway poll en boucle)
  - INV-120 : POST /report-state state=closed (toutes alive closed) → RESOLVE
              de l'alarme gateway
  - INV-122 : 2 gateways, 1 open + 1 closed → alarme active (politique OR)
  - INV-123 : dissensus > 5 min → sensor_dissensus_since rempli + email
              sysadmin envoyé + sensor_dissensus_email_sent_at set
  - INV-123 : dissensus résolu (cohérence retrouvée) → reset des 2 champs,
              pas de 2e email même si re-dissensus immédiat

Pas couvert ici (relève d'autres INV ou du gateway-side) :
  - Refonte côté gateway (poll → POST report-state) : couvert en PR2
  - FCM réel (best-effort, déjà couvert ailleurs)
  - Frontend badge dissensus : PR3

Phase : RED — ces tests DOIVENT ÉCHOUER sur le code actuel (endpoint absent),
puis passer après implémentation GREEN dans backend/app/api/alarms_internal.py
+ backend/app/database.py (migrations).
"""
import pytest

pytestmark = pytest.mark.integration


GATEWAY_KEY = "test-gateway-key-INV-120-V2"
GATEWAY_HEADERS = {"X-Gateway-Key": GATEWAY_KEY}


@pytest.fixture(autouse=True)
def _set_gateway_key(monkeypatch):
    """Le check du header lit os.getenv('GATEWAY_KEY') à chaque requête."""
    monkeypatch.setenv("GATEWAY_KEY", GATEWAY_KEY)


@pytest.fixture(autouse=True)
def _clean_gateway_states():
    """Reset table gateway_states entre tests (indépendant de l'API pour
    éviter couplage avec un endpoint qui n'existerait pas encore en RED)."""
    from backend.app.database import SessionLocal
    from sqlalchemy import text

    db = SessionLocal()
    try:
        # Idempotent : si la table n'existe pas encore (RED initial), on swallow.
        try:
            db.execute(text("DELETE FROM gateway_states"))
            db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()
    yield
    db = SessionLocal()
    try:
        try:
            db.execute(text("DELETE FROM gateway_states"))
            db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


def _reset_alarms(client, admin_headers):
    r = client.post("/api/alarms/reset", headers=admin_headers)
    assert r.status_code == 200, r.text


def _reset_clock(client):
    r = client.post("/api/test/reset-clock")
    assert r.status_code == 200, r.text


def _advance_clock(client, *, seconds: float = 0, minutes: float = 0):
    r = client.post(
        "/api/test/advance-clock",
        params={"seconds": seconds, "minutes": minutes},
    )
    assert r.status_code == 200, r.text


def _report(client, gateway_id: str, state: str, headers=GATEWAY_HEADERS):
    """Helper : POST /internal/alarms/report-state."""
    return client.post(
        "/internal/alarms/report-state",
        headers=headers,
        json={"gateway_id": gateway_id, "state": state},
    )


def _get_active(client, admin_headers):
    r = client.get("/api/alarms/active", headers=admin_headers)
    assert r.status_code == 200
    return r.json()


def _fetch_alarm_orm(alarm_id: int):
    """Lecture DB directe — utilisée pour vérifier les champs `source` et
    `sensor_dissensus_*` qui ne sont pas (encore) exposés par AlarmResponse."""
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm

    db = SessionLocal()
    try:
        a = db.query(Alarm).filter(Alarm.id == alarm_id).first()
        if a is None:
            return None
        return {
            "id": a.id,
            "status": a.status,
            "source": getattr(a, "source", None),
            "sensor_dissensus_since": getattr(a, "sensor_dissensus_since", None),
            "sensor_dissensus_email_sent_at": getattr(
                a, "sensor_dissensus_email_sent_at", None
            ),
        }
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# INV-120 V2 — endpoint /report-state, auth, reconciliation
# ─────────────────────────────────────────────────────────────────────────────


def test_report_state_without_key_returns_401(client, admin_headers):
    """INV-120 V2 + INV-065 : pas de X-Gateway-Key → 401, aucun effet de
    bord (pas d'upsert dans gateway_states, pas d'alarme créée)."""
    _reset_alarms(client, admin_headers)

    r = client.post(
        "/internal/alarms/report-state",
        json={"gateway_id": "onsite-1", "state": "open"},
        # PAS de header X-Gateway-Key
    )
    assert r.status_code == 401, (
        f"INV-065 : endpoint /internal/* sans X-Gateway-Key doit renvoyer 401, "
        f"got {r.status_code} {r.text}"
    )

    # Vérifier qu'aucune alarme n'a été créée
    assert _get_active(client, admin_headers) == [], (
        "INV-120 : un report refusé ne doit JAMAIS créer d'alarme"
    )


def test_report_open_creates_alarm_when_none_active(client, admin_headers):
    """INV-120 V2 : 1 gateway alive reportant 'open' + aucune alarme active
    → CREATE alarme `source="gateway_dry_contact"`, assignée pos 1, severity
    critical (CLAUDE.md). Body de réponse contient `alarm_active=True`."""
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    r = _report(client, "onsite-1", "open")
    assert r.status_code == 200, (
        f"INV-120 V2 : report avec clé valide doit renvoyer 200, got "
        f"{r.status_code} {r.text}"
    )
    body = r.json()
    assert body.get("alarm_active") is True, (
        f"INV-120 V2 : response doit indiquer alarm_active=True, got {body}"
    )

    active = _get_active(client, admin_headers)
    assert len(active) == 1, (
        f"INV-120 V2 : exactement 1 alarme active doit être créée, got {active}"
    )
    alarm = active[0]
    assert alarm["status"] == "active"
    assert alarm["severity"] == "critical"
    assert alarm["assigned_user_id"] is not None, (
        "INV-120 V2 : chaîne non vide → assignation au pos 1"
    )

    snap = _fetch_alarm_orm(alarm["id"])
    assert snap is not None
    assert snap["source"] == "gateway_dry_contact", (
        f"INV-120 V2 : alarme créée doit avoir source='gateway_dry_contact' "
        f"(pour permettre la reconcile/resolve sélective), got {snap['source']}"
    )


def test_report_open_no_op_when_gateway_alarm_already_active(
    client, admin_headers
):
    """INV-120 V2 : la gateway poll en boucle (toutes les 5 s). Tant que
    l'état physique reste 'open', chaque POST report-state doit être un
    no-op (alarme existante conservée, pas de 409 — c'est la grosse
    différence avec l'ancien /trigger qui levait 409). On vérifie qu'on
    a toujours UNE seule alarme avec le même id après 3 POSTs."""
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    r1 = _report(client, "onsite-1", "open")
    assert r1.status_code == 200
    alarm_id = _get_active(client, admin_headers)[0]["id"]

    # 2 reports supplémentaires (état stable physique)
    r2 = _report(client, "onsite-1", "open")
    r3 = _report(client, "onsite-1", "open")
    assert r2.status_code == 200, (
        f"INV-120 V2 : re-report 'open' alors qu'une alarme gateway tourne "
        f"doit être no-op (200), pas 409. Got {r2.status_code} {r2.text}"
    )
    assert r3.status_code == 200, r3.text

    active = _get_active(client, admin_headers)
    assert len(active) == 1 and active[0]["id"] == alarm_id, (
        f"INV-120 V2 + INV-001 : la même alarme doit persister, pas de "
        f"duplicate. Got {active}"
    )


def test_report_closed_resolves_gateway_alarm(client, admin_headers):
    """INV-120 V2 : quand toutes les gateways alive reportent 'closed', le
    backend RESOLVE l'alarme gateway active (recovery automatique)."""
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    # 1. Création par 'open'
    _report(client, "onsite-1", "open")
    active = _get_active(client, admin_headers)
    assert len(active) == 1
    alarm_id = active[0]["id"]

    # 2. Retour repos : 'closed' → resolve
    r = _report(client, "onsite-1", "closed")
    assert r.status_code == 200
    body = r.json()
    assert body.get("alarm_active") is False, (
        f"INV-120 V2 : après 'closed', response doit indiquer "
        f"alarm_active=False, got {body}"
    )

    # L'alarme doit avoir disparu de /active (status=resolved)
    assert _get_active(client, admin_headers) == [], (
        "INV-120 V2 : 'closed' doit RESOLVE l'alarme gateway active"
    )
    snap = _fetch_alarm_orm(alarm_id)
    assert snap["status"] == "resolved", (
        f"INV-120 V2 : alarm.status doit être 'resolved' après reconcile "
        f"closed, got {snap['status']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# INV-122 — redondance multi-gateway, politique OR fail-to-alarm
# ─────────────────────────────────────────────────────────────────────────────


def test_or_policy_one_open_one_closed_keeps_alarm_active(client, admin_headers):
    """INV-122 : 2 gateways alive, 1 reporte 'open' + 1 reporte 'closed'.
    Politique OR fail-to-alarm → alarme active (au moins une = open).
    Vérifie aussi qu'un 'closed' isolé (par gateway 2) après création par
    gateway 1 ne résout PAS l'alarme."""
    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    # Gateway 1 voit 'open' → alarme créée
    r1 = _report(client, "onsite-1", "open")
    assert r1.status_code == 200
    assert r1.json().get("alarm_active") is True
    assert len(_get_active(client, admin_headers)) == 1

    # Gateway 2 voit 'closed' (câblage divergent ou retard de propagation)
    # Politique OR : alarme reste active car gateway 1 alive + 'open'.
    r2 = _report(client, "onsite-2", "closed")
    assert r2.status_code == 200
    assert r2.json().get("alarm_active") is True, (
        f"INV-122 : politique OR fail-to-alarm — 1 gateway 'open' + 1 'closed' "
        f"doit garder alarme active, got alarm_active={r2.json()}"
    )
    assert len(_get_active(client, admin_headers)) == 1, (
        "INV-122 : 1 alarme persiste tant qu'AU MOINS UNE gateway alive voit 'open'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# INV-123 — détection dissensus + email sysadmin + anti-spam
# ─────────────────────────────────────────────────────────────────────────────


def test_dissensus_over_5min_sets_flag_and_sends_email(
    client, admin_headers, monkeypatch
):
    """INV-123 : 2 gateways alive divergentes (1 open + 1 closed) pendant
    > 5 min → sensor_dissensus_since rempli (premier détecté) puis email
    sysadmin envoyé après le seuil + sensor_dissensus_email_sent_at set."""
    # Élargir liveness_window à 1h pour ce test : on advance le clock de 6min
    # sans re-poller chaque gateway toutes les 5s (en prod chacune polle ~72
    # fois pendant cette fenêtre). Sans cet override, g1 puis g2 deviennent
    # alternativement "silencieuses" lors des re-POSTs et le backend reset
    # le since (cohérence apparente avec 1 seule alive).
    monkeypatch.setenv("GATEWAY_LIVENESS_WINDOW_SECONDS", "3600")

    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    # Config alert_email pour vérification du destinataire
    r = client.post(
        "/api/config/system",
        headers=admin_headers,
        json={"key": "alert_email", "value": "direction_technique@charlesmurgat.com"},
    )
    assert r.status_code == 200, r.text

    # Reset l'état email pour ne pas hériter d'un test précédent
    # (Pas d'endpoint dédié — on inspecte via /api/test/last-email-sent.
    #  Si un email précédent existait, le test ci-dessous va échouer ; donc
    #  on accepte ce couplage faible et on vérifie body+to spécifiques.)

    # t=0 : dissensus instantané (les 2 gateways arrivent quasi simultanément)
    _report(client, "onsite-1", "open")
    _report(client, "onsite-2", "closed")

    # L'alarme est créée par gateway 1 ('open'). On la récupère.
    active = _get_active(client, admin_headers)
    assert len(active) == 1, f"INV-122 : 1 alarme attendue, got {active}"
    alarm_id = active[0]["id"]

    # Au 2e report (gateway 2), le backend a détecté le dissensus.
    snap0 = _fetch_alarm_orm(alarm_id)
    assert snap0["sensor_dissensus_since"] is not None, (
        "INV-123 : sensor_dissensus_since doit être rempli dès le 1er "
        f"épisode de divergence, got {snap0}"
    )
    assert snap0["sensor_dissensus_email_sent_at"] is None, (
        "INV-123 : pas d'email avant 5 min de dissensus, got "
        f"sensor_dissensus_email_sent_at={snap0['sensor_dissensus_email_sent_at']}"
    )

    # Avancer +6 min ET re-POST les 2 gateways. En prod chacune poll toutes
    # les 5 s donc ~72 polls auraient eu lieu pendant ces 6 min — ici on
    # simule juste le minimum pour rester "alive" (sinon liveness_window=15s
    # marque g2 comme silencieuse et le backend ne voit plus de dissensus).
    _advance_clock(client, minutes=6)
    _report(client, "onsite-1", "open")
    _report(client, "onsite-2", "closed")

    snap1 = _fetch_alarm_orm(alarm_id)
    assert snap1["sensor_dissensus_email_sent_at"] is not None, (
        "INV-123 : après 5+ min de dissensus continu, sensor_dissensus_email_"
        f"sent_at doit être rempli, got {snap1}"
    )

    # Vérifier l'email envoyé
    r = client.get("/api/test/last-email-sent")
    assert r.status_code == 200, r.text
    email = r.json()
    assert email.get("sent") is True, f"INV-123 : email doit être envoyé, got {email}"
    assert "direction_technique@charlesmurgat.com" in email.get("to", ""), (
        f"INV-123 : email à direction technique, got to={email.get('to')}"
    )
    # Sanity sur le contenu — doit mentionner dissensus/discordance pour ne
    # pas confondre avec l'email INV-080 "chaîne vide".
    body_lower = (email.get("body", "") + " " + email.get("subject", "")).lower()
    assert ("dissensus" in body_lower) or ("discordance" in body_lower) or (
        "divergent" in body_lower
    ), (
        f"INV-123 : email doit mentionner dissensus/discordance (pas un "
        f"email INV-080), got subject={email.get('subject')} body={email.get('body')}"
    )


def test_dissensus_resolved_resets_flag_and_no_second_email(
    client, admin_headers, monkeypatch
):
    """INV-123 : quand la cohérence est retrouvée (les 2 gateways
    reportent la même chose), sensor_dissensus_since ET _email_sent_at
    sont reset à NULL. Anti-spam : 1 email par épisode — si un 2e
    épisode démarre immédiatement après reset, il doit attendre 5 min à
    nouveau."""
    # Cf test précédent : élargir liveness pour éviter les faux silences
    # entre re-POSTs séquentiels sous advance-clock.
    monkeypatch.setenv("GATEWAY_LIVENESS_WINDOW_SECONDS", "3600")

    _reset_alarms(client, admin_headers)
    _reset_clock(client)

    # Setup dissensus
    _report(client, "onsite-1", "open")
    _report(client, "onsite-2", "closed")
    active = _get_active(client, admin_headers)
    assert len(active) == 1
    alarm_id = active[0]["id"]

    snap = _fetch_alarm_orm(alarm_id)
    assert snap["sensor_dissensus_since"] is not None

    # Cohérence retrouvée : gateway 2 reporte 'open' aussi
    _report(client, "onsite-2", "open")

    snap = _fetch_alarm_orm(alarm_id)
    assert snap["sensor_dissensus_since"] is None, (
        f"INV-123 : cohérence retrouvée → sensor_dissensus_since reset à "
        f"NULL, got {snap['sensor_dissensus_since']}"
    )
    assert snap["sensor_dissensus_email_sent_at"] is None, (
        f"INV-123 : reset _email_sent_at en même temps que _since, got "
        f"{snap['sensor_dissensus_email_sent_at']}"
    )

    # 2e épisode : redivergence immédiate. Pas de 2e email avant 5 min.
    # Re-POST les 2 gateways après chaque advance pour les garder "alive"
    # (cf liveness_window=15s, en prod elles polleraient toutes les 5s).
    _report(client, "onsite-2", "closed")
    _advance_clock(client, minutes=2)
    _report(client, "onsite-1", "open")
    _report(client, "onsite-2", "closed")

    snap = _fetch_alarm_orm(alarm_id)
    assert snap["sensor_dissensus_since"] is not None
    assert snap["sensor_dissensus_email_sent_at"] is None, (
        f"INV-123 anti-spam : nouvel épisode ne doit pas réémettre d'email "
        f"avant 5 min de dissensus continu, got "
        f"{snap['sensor_dissensus_email_sent_at']}"
    )
