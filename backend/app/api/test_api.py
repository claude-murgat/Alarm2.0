from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import timedelta, datetime
from ..clock import now as clock_now
from .. import clock as clock_module
from ..database import get_db
from ..models import Alarm, User, EscalationConfig, SmsQueue

router = APIRouter(prefix="/api/test", tags=["test"])


@router.post("/send-alarm")
def send_test_alarm(db: Session = Depends(get_db)):
    """Send a test alarm. Resolves any existing active alarm first (single alarm mode)."""
    existing = db.query(Alarm).filter(Alarm.status.in_(["active", "escalated"])).first()
    if existing:
        existing.status = "resolved"
        db.commit()

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
        notified_user_ids=str(user_id) if user_id else "",
    )
    db.add(alarm)
    db.commit()
    db.refresh(alarm)
    return {"status": "sent", "alarm_id": alarm.id, "assigned_to": user_id}


@router.post("/simulate-watchdog-failure")
def simulate_watchdog_failure(db: Session = Depends(get_db)):
    """Simulate watchdog failure by setting all user heartbeats to old."""
    users = db.query(User).all()
    old_time = clock_now() - timedelta(minutes=5)
    for user in users:
        user.last_heartbeat = old_time
        user.is_online = False
    db.commit()
    return {"status": "simulated", "users_affected": len(users)}


@router.post("/simulate-connection-loss")
def simulate_connection_loss(db: Session = Depends(get_db)):
    """Simulate connection loss by marking all users offline.
    Stocke aussi un timestamp Unix pour bloquer les heartbeats des tokens anciens
    (ex: app Android en arrière-plan) — seuls les fresh logins peuvent rétablir le heartbeat."""
    import time as _time
    from . import devices as devices_module
    users = db.query(User).all()
    for user in users:
        user.is_online = False
    db.commit()
    # Bloquer les tokens émis STRICTEMENT AVANT cette seconde
    devices_module.connection_loss_time_int = int(_time.time())
    return {"status": "simulated", "users_affected": len(users)}


@router.post("/reset")
def reset_all(db: Session = Depends(get_db)):
    """Reset all alarms, user states, and restore default escalation chain."""
    db.query(Alarm).delete()

    # Reset heartbeat pause et connection loss simulation
    from . import devices as devices_module
    devices_module.heartbeat_paused = False
    devices_module.connection_loss_time_int = None

    users = db.query(User).all()
    for user in users:
        user.is_online = True
        user.last_heartbeat = clock_now()
    db.commit()

    # Restore default escalation chain if empty
    existing_esc = db.query(EscalationConfig).count()
    if existing_esc == 0:
        user1 = db.query(User).filter(User.name == "user1").first()
        user2 = db.query(User).filter(User.name == "user2").first()
        admin = db.query(User).filter(User.name == "admin").first()
        if user1 and user2 and admin:
            db.add_all([
                EscalationConfig(position=1, user_id=user1.id, delay_minutes=15.0),
                EscalationConfig(position=2, user_id=user2.id, delay_minutes=15.0),
                EscalationConfig(position=3, user_id=admin.id, delay_minutes=15.0),
            ])
            db.commit()

    return {"status": "reset complete"}


@router.post("/toggle-heartbeat-pause")
def toggle_heartbeat_pause():
    """Toggle the heartbeat pause flag. When paused, heartbeat endpoint returns success
    but does NOT update the last_heartbeat timestamp."""
    from . import devices as devices_module
    devices_module.heartbeat_paused = not devices_module.heartbeat_paused
    return {"status": "ok", "paused": devices_module.heartbeat_paused}


@router.post("/trigger-escalation")
def trigger_escalation(db: Session = Depends(get_db)):
    """Exécute un cycle d'escalade forcé (pour tests déterministes).
    Escalade toutes les alarmes actives vers l'utilisateur suivant dans la chaîne,
    indépendamment du délai configuré. Saute les utilisateurs offline."""
    from ..models import EscalationConfig

    active_alarms = (
        db.query(Alarm)
        .filter(Alarm.status.in_(["active", "escalated"]))
        .all()
    )

    escalation_chain = (
        db.query(EscalationConfig)
        .order_by(EscalationConfig.position)
        .all()
    )

    escalated_count = 0
    for alarm in active_alarms:
        if not escalation_chain:
            continue

        # Trouver la position actuelle dans la chaîne
        current_position = -1
        for ec in escalation_chain:
            if ec.user_id == alarm.assigned_user_id:
                current_position = ec.position
                break

        # Trouver l'utilisateur suivant ONLINE (avec rebouclage)
        next_user = _find_next_online_user(
            db, escalation_chain, current_position, alarm.assigned_user_id
        )

        if next_user and next_user.user_id != alarm.assigned_user_id:
            alarm.assigned_user_id = next_user.user_id
            # Ajouter le nouvel utilisateur à la liste cumulative des notifiés
            raw = alarm.notified_user_ids or ""
            current_ids = [int(x) for x in raw.split(",") if x.strip()]
            if next_user.user_id not in current_ids:
                current_ids.append(next_user.user_id)
            alarm.notified_user_ids = ",".join(str(x) for x in current_ids)
            alarm.status = "escalated"
            alarm.escalation_count += 1
            escalated_count += 1

    db.commit()
    return {"status": "ok", "escalated": escalated_count}


def _find_next_online_user(db, escalation_chain, current_position, current_user_id):
    """Find the next online user in the escalation chain after current_position.
    Wraps around if needed. Skips users who are known offline (have a last_heartbeat
    but is_online=False). Users who never had a heartbeat are not skipped.
    Returns None if no eligible user found (other than current)."""
    candidates = []
    for ec in escalation_chain:
        if ec.position > current_position:
            candidates.append(ec)
    for ec in escalation_chain:
        if ec.position <= current_position:
            candidates.append(ec)

    for ec in candidates:
        if ec.user_id == current_user_id:
            continue
        user = db.query(User).filter(User.id == ec.user_id).first()
        if not user:
            continue
        # Skip user only if they have been seen before (last_heartbeat not null) and are offline
        if user.last_heartbeat is not None and not user.is_online:
            continue
        return ec

    return None


@router.get("/last-email-sent")
def get_last_email_sent():
    """Return the last email sent by the system (for testing)."""
    from ..email_service import get_last_email
    return get_last_email()


@router.get("/status")
def get_status(db: Session = Depends(get_db)):
    """Get overall system status."""
    total_users = db.query(User).count()
    online_users = db.query(User).filter(User.is_online == True).count()
    active_alarms = db.query(Alarm).filter(Alarm.status.in_(["active", "escalated"])).count()
    ack_alarms = db.query(Alarm).filter(Alarm.status == "acknowledged").count()
    resolved_alarms = db.query(Alarm).filter(Alarm.status == "resolved").count()

    return {
        "users": total_users,
        "connected_users": online_users,
        "alarms": {"active": active_alarms, "acknowledged": ack_alarms, "resolved": resolved_alarms},
    }


@router.post("/advance-clock")
def advance_clock(seconds: float = 0, minutes: float = 0):
    """Avance l'horloge du serveur (pour tests d'escalade avec timing réel)."""
    total = seconds + minutes * 60
    clock_module.advance(total)
    return {
        "status": "ok",
        "advanced_seconds": total,
        "total_offset_seconds": clock_module.get_offset_seconds(),
    }


@router.post("/reset-clock")
def reset_clock():
    """Remet l'horloge à l'heure réelle."""
    clock_module.reset()
    return {"status": "ok", "offset_seconds": 0}


@router.post("/reset-sms-queue")
def reset_sms_queue(db: Session = Depends(get_db)):
    """Vide la table sms_queue (pour les tests)."""
    db.query(SmsQueue).delete()
    db.commit()
    return {"status": "ok"}


@router.post("/insert-sms")
def insert_sms(payload: dict, db: Session = Depends(get_db)):
    """Insère un SMS directement dans sms_queue (pour les tests)."""
    row = SmsQueue(
        to_number=payload["to_number"],
        body=payload["body"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.post("/simulate-loop-stall")
def simulate_loop_stall():
    """Simule une boucle d'escalade bloquée en mettant last_tick_at à une date passée."""
    from .. import escalation as esc_module
    esc_module.last_tick_at = datetime(2020, 1, 1)
    return {"status": "ok", "last_tick_at": "2020-01-01T00:00:00"}
