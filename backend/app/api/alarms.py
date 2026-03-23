from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timedelta, timezone
from ..database import get_db
from ..models import Alarm, User, EscalationConfig
from ..schemas import AlarmCreate, AlarmResponse
from ..auth import get_current_user

router = APIRouter(prefix="/api/alarms", tags=["alarms"])


@router.post("/send", response_model=AlarmResponse)
def send_alarm(alarm_data: AlarmCreate, db: Session = Depends(get_db)):
    """Send a new alarm. If no user specified, assign to first in escalation chain."""
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
            first_user = db.query(User).first()
            if first_user:
                assigned_user_id = first_user.id

    alarm = Alarm(
        title=alarm_data.title,
        message=alarm_data.message,
        severity=alarm_data.severity,
        assigned_user_id=assigned_user_id,
    )
    db.add(alarm)
    db.commit()
    db.refresh(alarm)
    return alarm


@router.get("/", response_model=List[AlarmResponse])
def list_alarms(db: Session = Depends(get_db)):
    return db.query(Alarm).order_by(Alarm.created_at.desc()).all()


@router.get("/active", response_model=List[AlarmResponse])
def active_alarms(db: Session = Depends(get_db)):
    return (
        db.query(Alarm)
        .filter(Alarm.status.in_(["active", "escalated"]))
        .order_by(Alarm.created_at.desc())
        .all()
    )


@router.get("/mine", response_model=List[AlarmResponse])
def my_alarms(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get active alarms for the current user, excluding suspended ones."""
    now = datetime.utcnow()
    alarms = (
        db.query(Alarm)
        .filter(
            Alarm.assigned_user_id == current_user.id,
            Alarm.status.in_(["active", "escalated"]),
        )
        .all()
    )
    # Filter out suspended alarms
    return [a for a in alarms if not a.suspended_until or a.suspended_until <= now]


@router.post("/{alarm_id}/ack", response_model=AlarmResponse)
def acknowledge_alarm(
    alarm_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")

    now = datetime.utcnow()
    alarm.status = "acknowledged"
    alarm.acknowledged_at = now
    alarm.acknowledged_by = current_user.id
    alarm.suspended_until = now + timedelta(minutes=30)
    db.commit()
    db.refresh(alarm)
    return alarm


@router.post("/{alarm_id}/resolve", response_model=AlarmResponse)
def resolve_alarm(alarm_id: int, db: Session = Depends(get_db)):
    alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")

    alarm.status = "resolved"
    db.commit()
    db.refresh(alarm)
    return alarm


@router.post("/reset")
def reset_alarms(db: Session = Depends(get_db)):
    """Reset all alarms - for testing."""
    db.query(Alarm).delete()
    db.commit()
    return {"status": "all alarms reset"}
