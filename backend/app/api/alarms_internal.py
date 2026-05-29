"""
INV-120 V2 — Endpoint interne de reconciliation d'état depuis les gateways on-site.

Source : tests/INVARIANTS.md section 13 (INV-120 V2 + INV-122 + INV-123)
+ issue GitHub #112.

Architecture : level-based reconciliation. Chaque gateway poll son contact
sec NC à intervalle régulier (5 s par défaut côté gateway) et POST son état
courant ("open" | "closed"). Le backend :
  1. UPSERT dans gateway_states (état courant par gateway).
  2. Compute should_be_active = any(g.state=="open" for g in alive_gateways)
     où alive = last_seen > now - GATEWAY_LIVENESS_WINDOW_SECONDS (défaut 15s).
  3. Reconcile : CREATE/RESOLVE alarme source="gateway_dry_contact" selon
     should_be_active. Touche UNIQUEMENT les alarmes gateway (les alarmes
     "api"/"oncall" actives bloquent la création mais ne sont jamais resolved
     automatiquement par la reconcile — décision MathieuP issue #112).
  4. INV-123 : détecte le dissensus (states divergents > 5 min) → flag
     l'alarme + envoie email sysadmin (1 par épisode, anti-spam).

Authentification : X-Gateway-Key (INV-065, même pattern que /internal/sms/*
et /internal/calls/*). Pas d'auth user — la gateway hardware n'a pas de session.

Toutes les écritures de timestamp passent par clock_now() (INV-066).
La création d'alarme gateway respecte INV-001 (unicité), INV-080 (fallback
chaîne vide), INV-018 (original_created_at).
"""
import os
import logging
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..clock import now as clock_now
from ..database import get_db
from ..events import log_event
from ..logic.alarm_creation import evaluate_alarm_creation_plan
from ..logic.models import EscalationChainEntry, UserSnapshot
from ..models import (
    Alarm,
    AlarmNotification,
    EscalationConfig,
    GatewayState,
    SystemConfig,
    User,
)

logger = logging.getLogger("alarms_internal")

router = APIRouter(prefix="/internal/alarms", tags=["alarms-gateway"])


# Defaults appliqués quand la gateway crée une alarme (pas de title/message
# dans le body /report-state — la gateway ne sait que "open" ou "closed").
DEFAULT_TITLE = "Déclenchement contact sec local"
DEFAULT_MESSAGE = (
    "Alarme déclenchée par capteur câblé NC sur la gateway on-site (SIM7600)."
)

# INV-123 : seuil dissensus prolongé avant envoi email sysadmin.
DISSENSUS_EMAIL_THRESHOLD = timedelta(minutes=5)


class ReportStateRequest(BaseModel):
    """Body du POST /report-state. La gateway envoie son état courant."""
    gateway_id: str = Field(..., min_length=1, max_length=128)
    state: Literal["open", "closed"]


def _check_gateway_key(x_gateway_key: Optional[str] = Header(None)):
    """INV-065 — clé gateway obligatoire (même implémentation que sms.py / calls.py)."""
    expected = os.getenv("GATEWAY_KEY", "")
    if not expected or x_gateway_key != expected:
        raise HTTPException(status_code=401, detail="Invalid gateway key")


def _liveness_window_seconds() -> int:
    """INV-122 : fenêtre au-delà de laquelle une gateway est considérée silencieuse.
    Défaut 15 s = 3 polls à 5 s côté gateway. Configurable via env pour tests."""
    return int(os.getenv("GATEWAY_LIVENESS_WINDOW_SECONDS", "15"))


def _alert_email(db: Session) -> str:
    cfg = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
    return cfg.value if cfg else "direction_technique@charlesmurgat.com"


def _create_gateway_alarm(db: Session) -> Alarm:
    """Crée une alarme `source="gateway_dry_contact"`. Logique alignée sur
    POST /api/alarms/send (assignation pos 1 chaîne, INV-080 chaîne vide,
    FCM, audit log, INV-018, INV-066)."""
    chain_orm = db.query(EscalationConfig).order_by(EscalationConfig.position).all()
    chain_snapshot = [
        EscalationChainEntry(position=ec.position, user_id=ec.user_id)
        for ec in chain_orm
    ]
    users_orm = db.query(User).all()
    user_snapshots = [
        UserSnapshot(
            id=u.id,
            name=u.name,
            is_online=u.is_online,
            last_heartbeat=u.last_heartbeat,
        )
        for u in users_orm
    ]

    plan = evaluate_alarm_creation_plan(
        requested_assigned_user_id=None,
        chain=chain_snapshot,
        users=user_snapshots,
    )

    # INV-080 : chaîne vide → email direction technique (sujet/contenu différencié
    # du dissensus INV-123 pour ne pas confondre — cf assert dans tests/integration/
    # test_report_state_inv120v2.py::test_dissensus_over_5min_*).
    if plan.needs_direction_technique_email:
        from ..email_service import send_alert_email

        send_alert_email(
            subject="Alerte: chaîne d'escalade vide (gateway dry contact)",
            body=(
                "Une alarme a été déclenchée par contact sec local mais "
                "la chaîne d'escalade est vide.\n"
                f"Source: gateway on-site (X-Gateway-Key)"
            ),
            to=_alert_email(db),
        )

    _now = clock_now()
    alarm = Alarm(
        title=DEFAULT_TITLE,
        message=DEFAULT_MESSAGE,
        severity="critical",
        assigned_user_id=plan.assigned_user_id,
        source="gateway_dry_contact",
        original_created_at=_now,
        created_at=_now,
    )
    db.add(alarm)
    db.flush()

    if plan.assigned_user_id is not None:
        db.add(
            AlarmNotification(
                alarm_id=alarm.id,
                user_id=plan.assigned_user_id,
                notified_at=_now,
            )
        )

    log_event(
        "alarm_created",
        db=db,
        alarm_id=alarm.id,
        user_id=None,
        source="gateway_dry_contact",
        assigned_to=alarm.assigned_user_id,
    )

    if plan.assigned_user_id is not None:
        try:
            from ..fcm_service import send_fcm_to_user

            send_fcm_to_user(
                db,
                plan.assigned_user_id,
                alarm.title,
                alarm.message,
                {"alarm_id": str(alarm.id), "severity": alarm.severity},
            )
        except Exception:
            logger.warning("FCM best-effort failed for gateway alarm", exc_info=True)

    return alarm


def _resolve_gateway_alarm(db: Session, alarm: Alarm) -> None:
    """RESOLVE auto d'une alarme `source="gateway_dry_contact"` quand toutes
    les gateways alive reportent 'closed'."""
    _now = clock_now()
    alarm.status = "resolved"
    alarm.updated_at = _now
    log_event(
        "alarm_resolved",
        db=db,
        alarm_id=alarm.id,
        user_id=None,
        source="gateway_dry_contact_auto_resolve",
    )


def _send_dissensus_email(
    db: Session,
    alive_gateways: list,
    elapsed: timedelta,
) -> None:
    """INV-123 : email sysadmin (1 seul par épisode, garde-fou anti-spam
    porté par sensor_dissensus_email_sent_at côté caller)."""
    from ..email_service import send_alert_email

    body_lines = [
        f"Les gateways on-site reportent des états divergents depuis "
        f"{int(elapsed.total_seconds() / 60)} minutes.",
        "",
        "Gateways alive :",
    ]
    for g in alive_gateways:
        body_lines.append(
            f"  - {g.gateway_id} : state={g.state}, last_seen={g.last_seen.isoformat()}"
        )
    body_lines.append("")
    body_lines.append(
        "Action attendue : vérifier le câblage du contact sec (un fil "
        "probablement débranché côté une des cartes) et l'état des cartes "
        "SIM7600. Tant que le dissensus persiste, la politique OR fail-to-alarm "
        "(INV-122) garde une alarme active dès qu'au moins une carte voit 'open'."
    )

    send_alert_email(
        subject="Alarme Murgat : discordance capteurs HW — intervention requise",
        body="\n".join(body_lines),
        to=_alert_email(db),
    )


@router.post("/report-state")
def report_state(
    payload: ReportStateRequest,
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """INV-120 V2 : reconciliation level-based de l'état physique avec
    l'état backend. Cf docstring du module pour la logique complète."""
    _now = clock_now()

    # 1. UPSERT gateway_states (la gateway dit son état courant)
    gs = (
        db.query(GatewayState)
        .filter(GatewayState.gateway_id == payload.gateway_id)
        .first()
    )
    if gs is None:
        gs = GatewayState(
            gateway_id=payload.gateway_id,
            state=payload.state,
            last_seen=_now,
        )
        db.add(gs)
    else:
        gs.state = payload.state
        gs.last_seen = _now
    db.flush()

    # 2. Compute alive_gateways + should_be_active (politique OR fail-to-alarm)
    liveness = _liveness_window_seconds()
    cutoff = _now - timedelta(seconds=liveness)
    alive_gateways = (
        db.query(GatewayState).filter(GatewayState.last_seen > cutoff).all()
    )
    alive_states = [g.state for g in alive_gateways]
    should_be_active = any(s == "open" for s in alive_states)

    # 3. Reconcile (touche UNIQUEMENT les alarmes source="gateway_dry_contact").
    # "acknowledged" est inclus avec "active"/"escalated" : tant que l'alarme
    # n'est pas terminale (resolved/cancelled), un re-poll du contact ouvert
    # doit être no-op — sinon l'opérateur subit un cycle sonnerie/ack par tick
    # tant que la cause physique persiste (issue #118, INV-120 V2 + ack).
    any_active_alarm = (
        db.query(Alarm)
        .filter(Alarm.status.in_(["active", "escalated", "acknowledged"]))
        .first()
    )
    gateway_alarm = None
    if any_active_alarm is not None and any_active_alarm.source == "gateway_dry_contact":
        gateway_alarm = any_active_alarm

    if should_be_active:
        if any_active_alarm is None:
            # Pas d'alarme active → CREATE
            gateway_alarm = _create_gateway_alarm(db)
        # Sinon : no-op (idempotence boucle si gateway_alarm, ou INV-001 si autre source)
    else:
        if gateway_alarm is not None:
            # Toutes alive closed + alarme gateway existante → RESOLVE
            _resolve_gateway_alarm(db, gateway_alarm)
            gateway_alarm = None  # plus en (active, escalated)
        # Sinon : no-op (pas d'alarme gateway à résoudre ; on ne touche pas aux
        # alarmes "api"/"oncall" actives)

    db.flush()

    # 4. INV-123 : détection dissensus (sur l'alarme gateway active si elle existe)
    divergent = (len(alive_gateways) >= 2) and (
        set(alive_states) == {"open", "closed"}
    )

    # Re-fetch l'alarme gateway active après la phase reconcile (peut avoir été
    # créée ou résolue dans le bloc précédent).
    gateway_alarm = (
        db.query(Alarm)
        .filter(
            Alarm.status.in_(["active", "escalated"]),
            Alarm.source == "gateway_dry_contact",
        )
        .first()
    )

    if gateway_alarm is not None:
        if divergent:
            if gateway_alarm.sensor_dissensus_since is None:
                gateway_alarm.sensor_dissensus_since = _now
            else:
                elapsed = _now - gateway_alarm.sensor_dissensus_since
                if (
                    elapsed > DISSENSUS_EMAIL_THRESHOLD
                    and gateway_alarm.sensor_dissensus_email_sent_at is None
                ):
                    _send_dissensus_email(db, alive_gateways, elapsed)
                    gateway_alarm.sensor_dissensus_email_sent_at = _now
                    log_event(
                        "sensor_dissensus_email_sent",
                        db=db,
                        alarm_id=gateway_alarm.id,
                        user_id=None,
                        elapsed_seconds=int(elapsed.total_seconds()),
                    )
        else:
            # Cohérence retrouvée (ou alive < 2) → reset les 2 champs
            if gateway_alarm.sensor_dissensus_since is not None:
                gateway_alarm.sensor_dissensus_since = None
                gateway_alarm.sensor_dissensus_email_sent_at = None

    db.commit()

    return {"alarm_active": should_be_active, "dissensus": divergent}
