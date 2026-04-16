import os
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import CallQueue, Alarm, AlarmNotification, User
from ..clock import now as clock_now

router = APIRouter(prefix="/internal", tags=["call-gateway"])


def _check_gateway_key(x_gateway_key: Optional[str] = Header(None)):
    """Verifie que la cle gateway est correcte."""
    expected = os.getenv("GATEWAY_KEY", "")
    if not expected or x_gateway_key != expected:
        raise HTTPException(status_code=401, detail="Invalid gateway key")


@router.get("/calls/pending")
def get_pending_calls(
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """Retourne les appels en attente (non traites, retries < 3)."""
    rows = (
        db.query(CallQueue)
        .filter(CallQueue.called_at == None, CallQueue.retries < 3)
        .order_by(CallQueue.created_at)
        .all()
    )
    return [
        {
            "id": r.id,
            "to_number": r.to_number,
            "alarm_id": r.alarm_id,
            "user_id": r.user_id,
            "tts_message": r.tts_message,
            "retries": r.retries,
            "created_at": str(r.created_at),
        }
        for r in rows
    ]


@router.post("/calls/{call_id}/result")
def post_call_result(
    call_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """Enregistre le resultat d'un appel.
    result: ack_dtmf | ack_sms | no_answer | busy | error | escalate
    """
    row = db.query(CallQueue).filter(CallQueue.id == call_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Call not found")

    result = payload.get("result", "unknown")
    row.result = result
    row.called_at = clock_now()

    if result in ("ack_dtmf", "ack_sms"):
        # Acquitter l'alarme
        alarm = db.query(Alarm).filter(Alarm.id == row.alarm_id).first()
        if alarm and alarm.status in ("active", "escalated"):
            now = clock_now()
            ack_minutes = 30  # Default ack suspension
            alarm.status = "acknowledged"
            alarm.acknowledged_at = now
            alarm.acknowledged_by = row.user_id
            user = db.query(User).filter(User.id == row.user_id).first()
            alarm.acknowledged_by_name = user.name if user else str(row.user_id)
            from datetime import timedelta
            alarm.suspended_until = now + timedelta(minutes=ack_minutes)
        db.commit()
        return {"status": "acked", "id": call_id, "result": result, "retries": row.retries}

    elif result == "escalate":
        # Forcer l'escalade : on change assigned_user_id vers le prochain
        alarm = db.query(Alarm).filter(Alarm.id == row.alarm_id).first()
        if alarm:
            from ..escalation import _find_next_user
            from ..models import EscalationConfig
            chain = db.query(EscalationConfig).order_by(EscalationConfig.position).all()
            current_pos = -1
            for ec in chain:
                if ec.user_id == alarm.assigned_user_id:
                    current_pos = ec.position
                    break
            next_user = _find_next_user(db, chain, current_pos, alarm.assigned_user_id)
            if next_user:
                alarm.assigned_user_id = next_user.user_id
                alarm.status = "escalated"
                alarm.escalation_count += 1
                alarm.created_at = clock_now()
                # Ajouter le nouvel utilisateur aux notifies
                from ..escalation import _add_notified_user
                _add_notified_user(db, alarm, next_user.user_id)
        db.commit()
        return {"status": "escalated", "id": call_id, "result": result, "retries": row.retries}

    else:
        # no_answer, busy, error → incrementer retries
        row.retries += 1
        db.commit()
        return {"status": "retry", "id": call_id, "result": result, "retries": row.retries}


@router.post("/alarms/active/ack-by-phone")
def ack_by_phone(
    payload: dict,
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """Acquitte l'alarme active en matchant le numero de telephone → utilisateur.
    Idempotent : si deja acquittee, retourne 200."""
    phone = payload.get("phone_number", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone_number required")

    # Trouver l'utilisateur par numero
    user = db.query(User).filter(User.phone_number == phone).first()
    if not user:
        raise HTTPException(status_code=404, detail="Unknown phone number")

    # Trouver l'alarme active pour cet utilisateur (via AlarmNotification)
    active_alarms = (
        db.query(Alarm)
        .filter(Alarm.status.in_(["active", "escalated", "acknowledged"]))
        .all()
    )

    target_alarm = None
    for alarm in active_alarms:
        notif = (
            db.query(AlarmNotification)
            .filter(AlarmNotification.alarm_id == alarm.id, AlarmNotification.user_id == user.id)
            .first()
        )
        if notif:
            target_alarm = alarm
            break

    if not target_alarm:
        raise HTTPException(status_code=404, detail="No active alarm for this user")

    # Acquitter (idempotent si deja acknowledged)
    if target_alarm.status in ("active", "escalated"):
        now = clock_now()
        from datetime import timedelta
        ack_minutes = 30
        target_alarm.status = "acknowledged"
        target_alarm.acknowledged_at = now
        target_alarm.acknowledged_by = user.id
        target_alarm.acknowledged_by_name = user.name
        target_alarm.suspended_until = now + timedelta(minutes=ack_minutes)
        db.commit()

    return {"status": "acked", "alarm_id": target_alarm.id, "user": user.name}
