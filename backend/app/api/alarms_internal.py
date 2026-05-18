"""
INV-120 — Endpoint interne de déclenchement d'alarme depuis la gateway on-site.

Source : tests/INVARIANTS.md INV-120 [H] "Trigger par contact sec NC local sur gateway".

Authentification : X-Gateway-Key (cf INV-065, même pattern que /internal/sms/*
et /internal/calls/*). Pas d'auth user — la gateway hardware n'a pas de session.

L'effet métier doit être STRICTEMENT IDENTIQUE à un POST /api/alarms/send :
unicité INV-001, fallback chaîne vide INV-080, FCM aux notifiés, audit log.
Voir backend/app/api/alarms.py:35 (send_alarm) pour la logique de référence —
on duplique volontairement plutôt que de factoriser pour minimiser les risques
de régression sur /api/alarms/send (cf CLAUDE.md "trois lignes similaires
valent mieux qu'une abstraction prématurée").
"""
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import get_current_user  # noqa: F401  (non utilisé ici, on est gateway-auth)
from ..database import get_db
from ..events import log_event
from ..logic.alarm_creation import evaluate_alarm_creation_plan
from ..logic.models import EscalationChainEntry, UserSnapshot
from ..models import Alarm, AlarmNotification, EscalationConfig, User
from ..schemas import AlarmResponse
from ..clock import now as clock_now

router = APIRouter(prefix="/internal/alarms", tags=["alarms-gateway"])


DEFAULT_TITLE = "Déclenchement contact sec local"
DEFAULT_MESSAGE = (
    "Alarme déclenchée par capteur câblé NC sur la gateway on-site (SIM7600)."
)


class TriggerRequest(BaseModel):
    """Body optionnel du trigger. Si absent, defaults appliqués."""
    title: Optional[str] = Field(default=None, max_length=200)
    message: Optional[str] = Field(default=None, max_length=2000)


def _check_gateway_key(x_gateway_key: Optional[str] = Header(None)):
    """Cf INV-065 — clé gateway obligatoire (même implémentation que sms.py / calls.py)."""
    expected = os.getenv("GATEWAY_KEY", "")
    if not expected or x_gateway_key != expected:
        raise HTTPException(status_code=401, detail="Invalid gateway key")


@router.post("/trigger", response_model=AlarmResponse)
def trigger_alarm(
    payload: Optional[TriggerRequest] = None,
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """INV-120 : déclenche une alarme depuis la gateway (contact sec NC local).

    Effet identique à POST /api/alarms/send :
    - 409 si une alarme est déjà active (INV-001)
    - Assignation au 1er user de la chaîne via evaluate_alarm_creation_plan
    - Email direction technique si chaîne vide (INV-080)
    - FCM au notifié (best-effort)
    - Audit log
    """
    # INV-001 : une seule alarme active à la fois
    existing = db.query(Alarm).filter(Alarm.status.in_(["active", "escalated"])).first()
    if existing:
        raise HTTPException(status_code=409, detail="Une alarme est déjà active")

    # Defaults si body absent ou champs absents
    title = (payload.title if payload and payload.title else DEFAULT_TITLE)
    message = (payload.message if payload and payload.message else DEFAULT_MESSAGE)

    # Snapshot de la chaîne + users pour la fonction pure (cf /api/alarms/send)
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
        requested_assigned_user_id=None,  # pas d'assignation manuelle depuis gateway
        chain=chain_snapshot,
        users=user_snapshots,
    )

    # INV-080 : chaîne vide → email direction technique
    if plan.needs_direction_technique_email:
        from ..email_service import send_alert_email
        from ..models import SystemConfig

        config = (
            db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
        )
        recipient = config.value if config else "direction_technique@charlesmurgat.com"
        send_alert_email(
            subject="Alerte: chaîne d'escalade vide (trigger contact sec)",
            body=(
                f"Une alarme a été déclenchée par contact sec local mais "
                f"la chaîne d'escalade est vide.\n"
                f"Titre: {title}\n"
                f"Message: {message}\n"
                f"Source: gateway on-site (X-Gateway-Key)"
            ),
            to=recipient,
        )

    # INV-018 : original_created_at fige t0 (jamais modifie ensuite)
    _now = clock_now()
    alarm = Alarm(
        title=title,
        message=message,
        severity="critical",  # CLAUDE.md : toujours critical
        assigned_user_id=plan.assigned_user_id,
        original_created_at=_now,
        created_at=_now,
    )
    db.add(alarm)
    db.flush()

    if plan.assigned_user_id is not None:
        existing_notif = (
            db.query(AlarmNotification)
            .filter(
                AlarmNotification.alarm_id == alarm.id,
                AlarmNotification.user_id == plan.assigned_user_id,
            )
            .first()
        )
        if not existing_notif:
            db.add(
                AlarmNotification(
                    alarm_id=alarm.id,
                    user_id=plan.assigned_user_id,
                    notified_at=clock_now(),  # INV-066
                )
            )

    db.commit()
    db.refresh(alarm)

    log_event(
        "alarm_created",
        db=db,
        alarm_id=alarm.id,
        user_id=None,  # gateway hardware, pas d'utilisateur
        source="gateway_dry_contact",  # cf INV-120
        assigned_to=alarm.assigned_user_id,
    )

    # FCM best-effort (cf /api/alarms/send)
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
            pass

    return AlarmResponse.from_alarm(alarm, db)
