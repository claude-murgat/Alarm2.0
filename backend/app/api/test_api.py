from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from ..database import get_db
from ..models import Alarm, Device, User, EscalationConfig

router = APIRouter(prefix="/api/test", tags=["test"])


@router.post("/send-alarm")
def send_test_alarm(db: Session = Depends(get_db)):
    """Send a test alarm to the first user in escalation chain."""
    first_escalation = db.query(EscalationConfig).order_by(EscalationConfig.position).first()
    user_id = first_escalation.user_id if first_escalation else None

    if not user_id:
        first_user = db.query(User).first()
        user_id = first_user.id if first_user else None

    alarm = Alarm(
        title="TEST ALARM",
        message="This is a test alarm triggered from the admin panel.",
        severity="critical",
        assigned_user_id=user_id,
    )
    db.add(alarm)
    db.commit()
    db.refresh(alarm)
    return {"status": "sent", "alarm_id": alarm.id, "assigned_to": user_id}


@router.post("/simulate-watchdog-failure")
def simulate_watchdog_failure(db: Session = Depends(get_db)):
    """Simulate watchdog failure by setting all device heartbeats to old."""
    devices = db.query(Device).all()
    old_time = datetime.utcnow() - timedelta(minutes=5)
    for device in devices:
        device.last_heartbeat = old_time
        device.is_online = False
    db.commit()
    return {"status": "simulated", "devices_affected": len(devices)}


@router.post("/simulate-connection-loss")
def simulate_connection_loss(db: Session = Depends(get_db)):
    """Simulate connection loss by marking all devices offline."""
    devices = db.query(Device).all()
    for device in devices:
        device.is_online = False
    db.commit()
    return {"status": "simulated", "devices_affected": len(devices)}


@router.post("/reset")
def reset_all(db: Session = Depends(get_db)):
    """Reset all alarms and device states."""
    db.query(Alarm).delete()
    devices = db.query(Device).all()
    for device in devices:
        device.is_online = True
        device.last_heartbeat = datetime.utcnow()
    db.commit()
    return {"status": "reset complete"}


@router.get("/status")
def get_status(db: Session = Depends(get_db)):
    """Get overall system status."""
    total_users = db.query(User).count()
    total_devices = db.query(Device).count()
    online_devices = db.query(Device).filter(Device.is_online == True).count()
    active_alarms = db.query(Alarm).filter(Alarm.status.in_(["active", "escalated"])).count()
    ack_alarms = db.query(Alarm).filter(Alarm.status == "acknowledged").count()
    resolved_alarms = db.query(Alarm).filter(Alarm.status == "resolved").count()
    escalation_rules = db.query(EscalationConfig).count()

    return {
        "users": total_users,
        "devices": {"total": total_devices, "online": online_devices},
        "alarms": {"active": active_alarms, "acknowledged": ack_alarms, "resolved": resolved_alarms},
        "escalation_rules": escalation_rules,
    }
