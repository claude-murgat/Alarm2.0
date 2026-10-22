from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import timedelta
from ..clock import now as clock_now
from ..database import get_db
from ..models import Alarm, AlarmNotification, User, EscalationConfig, SystemConfig, SmsQueue, CallQueue
from ..schemas import AlarmCreate, AlarmResponse
from ..auth import get_current_user, get_current_admin
from ..events import log_event
from ..logic.alarm_creation import evaluate_alarm_creation_plan
from ..escalation import CANCELLED_SMS_ERROR
from ..logic.ack_authorization import evaluate_ack_authorization
from ..logic.models import EscalationChainEntry, UserSnapshot

router = APIRouter(prefix="/api/alarms", tags=["alarms"])


def _add_notified_user(db, alarm, user_id: int):
    """Ajoute un user_id à la table alarm_notifications s'il n'y est pas déjà.
    INV-066 : notified_at = clock_now() pour coherence avec l'horloge injectable."""
    existing = (
        db.query(AlarmNotification)
        .filter(AlarmNotification.alarm_id == alarm.id, AlarmNotification.user_id == user_id)
        .first()
    )
    if not existing:
        db.add(AlarmNotification(alarm_id=alarm.id, user_id=user_id, notified_at=clock_now()))


def _alarm_response(alarm, db):
    """Construit une AlarmResponse avec les noms résolus."""
    return AlarmResponse.from_alarm(alarm, db)


@router.post("/send", response_model=AlarmResponse)
def send_alarm(
    alarm_data: AlarmCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a new alarm. Only one active alarm at a time.
    Logique de decision (INV-080 chaine vide, fallback assignation) extraite
    dans logic/alarm_creation.py (testee unit)."""
    existing = db.query(Alarm).filter(Alarm.status.in_(["active", "escalated"])).first()
    if existing:
        raise HTTPException(status_code=409, detail="Une alarme est déjà active")

    # Snapshots pour la fonction pure
    chain_orm = db.query(EscalationConfig).order_by(EscalationConfig.position).all()
    chain_snapshot = [
        EscalationChainEntry(position=ec.position, user_id=ec.user_id)
        for ec in chain_orm
    ]
    users_orm = db.query(User).all()
    user_snapshots = [
        UserSnapshot(id=u.id, name=u.name, is_online=u.is_online, last_heartbeat=u.last_heartbeat)
        for u in users_orm
    ]

    plan = evaluate_alarm_creation_plan(
        requested_assigned_user_id=alarm_data.assigned_user_id,
        chain=chain_snapshot,
        users=user_snapshots,
    )

    # INV-080 : email direction technique si chaine vide
    if plan.needs_direction_technique_email:
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

    # INV-018 : original_created_at fige t0 (jamais modifie ensuite)
    _now = clock_now()
    alarm = Alarm(
        title=alarm_data.title,
        message=alarm_data.message,
        severity=alarm_data.severity,
        assigned_user_id=plan.assigned_user_id,
        original_created_at=_now,
        created_at=_now,
    )
    db.add(alarm)
    db.flush()  # Obtenir l'ID avant d'ajouter la notification

    # Ajouter l'utilisateur assigné à la table des notifiés
    if plan.assigned_user_id is not None:
        _add_notified_user(db, alarm, plan.assigned_user_id)

    db.commit()
    db.refresh(alarm)
    log_event("alarm_created", db=db, alarm_id=alarm.id, user_id=current_user.id,
              assigned_to=alarm.assigned_user_id)

    # Envoyer FCM a l'utilisateur assigne (fire-and-forget)
    if plan.assigned_user_id is not None:
        try:
            from ..fcm_service import send_fcm_to_user
            send_fcm_to_user(db, plan.assigned_user_id, alarm.title, alarm.message,
                             {"alarm_id": str(alarm.id), "severity": alarm.severity})
        except Exception:
            pass  # FCM est best-effort

    return _alarm_response(alarm, db)


@router.get("/", response_model=List[AlarmResponse])
def list_alarms(
    days: int = 10,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Liste les alarmes des N derniers jours (defaut 10, max 90)."""
    from ..clock import now as clock_now
    from datetime import timedelta
    days = max(1, min(days, 90))
    since = clock_now() - timedelta(days=days)
    # INV-018b : lectures historiques sur original_created_at (date d'evenement
    # immuable), pas created_at (timer interne reset a chaque palier d'escalade).
    alarms = (
        db.query(Alarm)
        .filter(Alarm.original_created_at >= since)
        .order_by(Alarm.original_created_at.desc())
        .all()
    )
    return [_alarm_response(a, db) for a in alarms]


@router.get("/active", response_model=List[AlarmResponse])
def active_alarms(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # INV-018b : ORDER BY sur original_created_at (date d'evenement, pas timer).
    alarms = (
        db.query(Alarm)
        .filter(Alarm.status.in_(["active", "escalated"]))
        .order_by(Alarm.original_created_at.desc())
        .all()
    )
    return [_alarm_response(a, db) for a in alarms]


def _iso(dt):
    return dt.isoformat() if dt else None


def _config_minutes(db, key: str, default: float) -> float:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    return float(row.value) if row else default


@router.get("/active/escalation")
def active_escalation(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """INV-136 : etat de l'escalade de l'alarme courante, pour le schema temps reel.
    Aucun champ ne bouge sans evenement (le WebSocket INV-135 diffuse sur changement) :
    les comptes a rebours sont calcules par le navigateur."""
    escalation_delay = _config_minutes(db, "escalation_delay_minutes", 15.0)
    sms_call_delay = _config_minutes(db, "sms_call_delay_minutes", 2.0)
    alarm = (
        db.query(Alarm)
        .filter(Alarm.status.in_(["active", "escalated", "acknowledged"]))
        .order_by(Alarm.original_created_at.desc(), Alarm.id.desc())
        .first()
    )
    notifs = {}
    if alarm:
        notifs = {n.user_id: n for n in db.query(AlarmNotification).filter(AlarmNotification.alarm_id == alarm.id)}

    steps = []
    for ec in db.query(EscalationConfig).order_by(EscalationConfig.position).all():
        user = db.query(User).filter(User.id == ec.user_id).first()
        notif = notifs.get(ec.user_id)
        sms = call = "none"
        if notif and not (user and user.phone_number):
            sms = call = "no_phone"
        elif notif:
            sms_row = (
                db.query(SmsQueue)
                .filter(SmsQueue.to_number == user.phone_number, SmsQueue.created_at >= notif.notified_at)
                .order_by(SmsQueue.created_at.desc())
                .first()
            )
            if not sms_row:
                sms = "pending" if notif.sms_sent else "scheduled"
            else:
                sms = ("sent" if sms_row.sent_at else "cancelled" if sms_row.error == CANCELLED_SMS_ERROR
                       else "failed" if sms_row.retries >= 3 else "pending")
            call_row = (
                db.query(CallQueue)
                .filter(CallQueue.alarm_id == alarm.id, CallQueue.user_id == ec.user_id,
                        CallQueue.created_at >= notif.notified_at)
                .order_by(CallQueue.created_at.desc())
                .first()
            )
            if not call_row:
                call = "pending" if notif.call_sent else "scheduled"
            else:
                call = call_row.result or ("error" if call_row.retries >= 3 else "pending")
        steps.append({
            "position": ec.position,
            "user_id": ec.user_id,
            "name": user.name if user else "?",
            "has_phone": bool(user and user.phone_number),
            "is_online": bool(user and user.is_online),
            "has_device": bool(user and user.has_device),  # INV-143
            "role": "upcoming" if not notif else "current" if ec.user_id == alarm.assigned_user_id else "notified",
            "notified_at": _iso(notif.notified_at) if notif else None,
            "contact_at": _iso(notif.notified_at + timedelta(minutes=sms_call_delay)) if notif else None,
            "sms": sms,
            "call": call,
            "acked": bool(alarm and alarm.status == "acknowledged" and alarm.acknowledged_by == ec.user_id),
        })

    running = alarm is not None and alarm.status != "acknowledged"
    return {
        "alarm": None if not alarm else {
            "id": alarm.id,
            "title": alarm.title,
            "status": alarm.status,
            "assigned_user_id": alarm.assigned_user_id,
            "escalation_count": alarm.escalation_count,
            "original_created_at": _iso(alarm.original_created_at),
            "created_at": _iso(alarm.created_at),
            "acknowledged_by": alarm.acknowledged_by,
            "acknowledged_by_name": alarm.acknowledged_by_name,
            "acknowledged_at": _iso(alarm.acknowledged_at),
            "suspended_until": _iso(alarm.suspended_until),
            # INV-016 : redevenue active apres un ack -> acquittement oublie
            "ack_expired": running and alarm.acknowledged_at is not None,
        },
        "escalation_delay_minutes": escalation_delay,
        "sms_call_delay_minutes": sms_call_delay,
        "next_escalation_at": _iso(alarm.created_at + timedelta(minutes=escalation_delay)) if running else None,
        "steps": steps,
    }


@router.get("/mine", response_model=List[AlarmResponse])
def my_alarms(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get alarms where the current user has been notified (cumulative escalation).
    Includes acknowledged alarms so clients can display ack status and countdown."""
    alarms = (
        db.query(Alarm)
        .join(AlarmNotification, AlarmNotification.alarm_id == Alarm.id)
        .filter(
            Alarm.status.in_(["active", "escalated", "acknowledged"]),
            AlarmNotification.user_id == current_user.id,
        )
        # L'app affiche le 1er element : les alarmes a acquitter d'abord, la plus recente en tete.
        .order_by(Alarm.status == "acknowledged", Alarm.original_created_at.desc(), Alarm.id.desc())
        .all()
    )
    return [_alarm_response(a, db) for a in alarms]


@router.post("/{alarm_id}/ack", response_model=AlarmResponse)
def acknowledge_alarm(
    alarm_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Acquitter une alarme.
    INV-031 : seuls les users notifies (alarm_notifications) peuvent ACK.
    Un user tiers (admin non notifie, mauvaise URL) recoit 403."""
    alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")

    # INV-031 : verification d'autorisation (logic/ack_authorization.py)
    notified_ids = [
        n[0] for n in db.query(AlarmNotification.user_id)
        .filter(AlarmNotification.alarm_id == alarm_id).all()
    ]
    auth = evaluate_ack_authorization(
        notified_user_ids=notified_ids,
        current_user_id=current_user.id,
    )
    if not auth.allowed:
        raise HTTPException(
            status_code=403,
            detail="Seuls les utilisateurs notifies peuvent acquitter cette alarme",
        )

    now = clock_now()
    alarm.status = "acknowledged"
    alarm.acknowledged_at = now
    alarm.acknowledged_by = current_user.id
    alarm.acknowledged_by_name = current_user.name
    alarm.suspended_until = now + timedelta(minutes=30)
    db.commit()
    db.refresh(alarm)
    log_event("alarm_acknowledged", db=db, alarm_id=alarm.id, by_user=current_user.id)
    return _alarm_response(alarm, db)


@router.post("/escalate-now")
def escalate_now(
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """INV-138 : palier d'escalade immediat (bouton de l'onglet Tests, prod comprise)."""
    from ..escalation import escalate_active_alarms_now
    return {"status": "ok", "escalated": escalate_active_alarms_now(db, by_user=current_user.id)}


@router.post("/{alarm_id}/resolve", response_model=AlarmResponse)
def resolve_alarm(
    alarm_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    alarm = db.query(Alarm).filter(Alarm.id == alarm_id).first()
    if not alarm:
        raise HTTPException(status_code=404, detail="Alarm not found")

    alarm.status = "resolved"
    db.commit()
    db.refresh(alarm)
    log_event("alarm_resolved", db=db, alarm_id=alarm.id, user_id=current_user.id)
    from ..fcm_service import push_alarm_resolved
    push_alarm_resolved(db, alarm)
    return _alarm_response(alarm, db)


@router.post("/reset")
def reset_alarms(
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Reset all alarms - for testing."""
    from ..models import SystemConfig
    from ..email_service import reset_last_email
    db.query(AlarmNotification).delete()
    db.query(Alarm).delete()
    alert_config = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
    if alert_config:
        db.delete(alert_config)
    db.commit()
    reset_last_email()
    return {"status": "all alarms reset"}
