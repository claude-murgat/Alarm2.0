from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import timedelta
from ..clock import now as clock_now
from ..database import get_db
from ..models import Alarm, User, EscalationConfig
from ..schemas import AlarmCreate, AlarmResponse
from ..auth import get_current_user

router = APIRouter(prefix="/api/alarms", tags=["alarms"])


def _add_notified_user(alarm, user_id: int):
    """Ajoute un user_id à la liste notified_user_ids s'il n'y est pas déjà."""
    raw = alarm.notified_user_ids or ""
    current_ids = [int(x) for x in raw.split(",") if x.strip()]
    if user_id not in current_ids:
        current_ids.append(user_id)
    alarm.notified_user_ids = ",".join(str(x) for x in current_ids)


def _alarm_response(alarm, db):
    """Construit une AlarmResponse avec les noms résolus."""
    return AlarmResponse.from_alarm(alarm, db)


@router.post("/send", response_model=AlarmResponse)
def send_alarm(alarm_data: AlarmCreate, db: Session = Depends(get_db)):
    """Send a new alarm. Only one active alarm at a time."""
    existing = db.query(Alarm).filter(Alarm.status.in_(["active", "escalated"])).first()
    if existing:
        raise HTTPException(status_code=409, detail="Une alarme est déjà active")

    assigned_user_id = alarm_data.assigned_user_id

    if not assigned_user_id:
        first_escalation = (
            db.query(EscalationConfig)
            .order_by(EscalationConfig.position)
            .first()
        )
        if first_escalation:
            assigned_user_id = first_escalation.user_id
        else:
            from ..models import SystemConfig
            from ..email_service import send_alert_email

            config = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
            recipient = config.value if config else "direction_technique@charlesmurgat.com"

            send_alert_email(
                subject="Alerte: chaîne d'escalade vide",
                body=(
                    f"Une alarme a été envoyée mais la chaîne d'escalade est vide.\n"
                    f"Titre: {alarm_data.title}\n"
                    f"Message: {alarm_data.message}\n"
                    f"Sévérité: {alarm_data.severity}"
                ),
                to=recipient,
            )

            first_user = db.query(User).first()
            if first_user:
                assigned_user_id = first_user.id

    alarm = Alarm(
        title=alarm_data.title,
        message=alarm_data.message,
        severity=alarm_data.severity,
        assigned_user_id=assigned_user_id,
    )
    # Ajouter le premier utilisateur assigné à la liste des notifiés
    if assigned_user_id:
        _add_notified_user(alarm, assigned_user_id)

    db.add(alarm)
    db.commit()
    db.refresh(alarm)
    return _alarm_response(alarm, db)


@router.get("/", response_model=List[AlarmResponse])
def list_alarms(db: Session = Depends(get_db)):
    alarms = db.query(Alarm).order_by(Alarm.created_at.desc()).all()
    return [_alarm_response(a, db) for a in alarms]


@router.get("/active", response_model=List[AlarmResponse])
def active_alarms(db: Session = Depends(get_db)):
    alarms = (
        db.query(Alarm)
        .filter(Alarm.status.in_(["active", "escalated"]))
        .order_by(Alarm.created_at.desc())
        .all()
    )
    return [_alarm_response(a, db) for a in alarms]


@router.get("/mine", response_model=List[AlarmResponse])
def my_alarms(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get active alarms where the current user has been notified (cumulative escalation)."""
    now = clock_now()
    # Chercher toutes les alarmes actives/escaladées
    alarms = (
        db.query(Alarm)
        .filter(Alarm.status.in_(["active", "escalated"]))
        .all()
    )
    result = []
    for a in alarms:
        # Vérifier si l'utilisateur est dans la liste des notifiés
        raw = a.notified_user_ids or ""
        notified = [int(x) for x in raw.split(",") if x.strip()]
        if current_user.id in notified:
            # Exclure les alarmes en suspension
            if not a.suspended_until or a.suspended_until <= now:
                result.append(_alarm_response(a, db))
    return result


@router.post("/{alarm_id}/ack", response_model=AlarmResponse)
def acknowledge_alarm(
    alarm_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")

    now = clock_now()
    alarm.status = "acknowledged"
    alarm.acknowledged_at = now
    alarm.acknowledged_by = current_user.id
    alarm.acknowledged_by_name = current_user.name
    alarm.suspended_until = now + timedelta(minutes=30)
    db.commit()
    db.refresh(alarm)
    return _alarm_response(alarm, db)


@router.post("/{alarm_id}/resolve", response_model=AlarmResponse)
def resolve_alarm(alarm_id: int, db: Session = Depends(get_db)):
    alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")

    alarm.status = "resolved"
    db.commit()
    db.refresh(alarm)
    return _alarm_response(alarm, db)


@router.post("/reset")
def reset_alarms(db: Session = Depends(get_db)):
    """Reset all alarms - for testing."""
    from ..models import SystemConfig
    from ..email_service import reset_last_email
    db.query(Alarm).delete()
    alert_config = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
    if alert_config:
        db.delete(alert_config)
    db.commit()
    reset_last_email()
    return {"status": "all alarms reset"}
