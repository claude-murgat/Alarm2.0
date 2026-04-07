import os
import urllib.parse
import urllib.request
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from datetime import timedelta, datetime
from ..clock import now as clock_now
from .. import clock as clock_module
from ..database import get_db
from ..models import Alarm, AlarmNotification, User, EscalationConfig, SmsQueue
from ..events import log_event

ENABLE_TEST_ENDPOINTS = os.getenv("ENABLE_TEST_ENDPOINTS", "false").lower() in ("true", "1", "yes")

router = APIRouter(prefix="/api/test", tags=["test"])


def _require_test_endpoints():
    """Guard: raise 404 if test endpoints are disabled."""
    if not ENABLE_TEST_ENDPOINTS:
        raise HTTPException(status_code=404, detail="Test endpoints are disabled")

# URLs des noeuds pairs pour broadcaster les operations de test
# (horloge, simulate-connection-loss, reset). Comma-separated.
_PEER_TEST_URLS = [
    u.strip().rstrip("/") for u in os.getenv("PEER_TEST_URLS", "").split(",") if u.strip()
]
# Backward compat : si PEER_TEST_URL (singulier) est set, l'utiliser
_legacy = os.getenv("PEER_TEST_URL", "").strip().rstrip("/")
if _legacy and _legacy not in _PEER_TEST_URLS:
    _PEER_TEST_URLS.append(_legacy)


def _broadcast(path: str, params: dict | None = None):
    """Envoie la meme requete POST a tous les noeuds pairs (best-effort).
    Le parametre peer=false est ajoute pour eviter les boucles infinies."""
    import logging
    _log = logging.getLogger("broadcast")
    for peer_url in _PEER_TEST_URLS:
        try:
            p = dict(params or {})
            p["peer"] = "false"
            qs = urllib.parse.urlencode(p)
            url = f"{peer_url}{path}?{qs}"
            req = urllib.request.Request(url, data=b"", method="POST")
            urllib.request.urlopen(req, timeout=2)
            _log.info(f"Broadcast OK: {url}")
        except Exception as e:
            _log.warning(f"Broadcast FAIL: {peer_url}{path} -> {e}")


@router.post("/send-alarm")
def send_test_alarm(db: Session = Depends(get_db)):
    """Send a test alarm. Resolves any existing active alarm first (single alarm mode)."""
    _require_test_endpoints()
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
    )
    db.add(alarm)
    db.flush()
    if user_id:
        db.add(AlarmNotification(alarm_id=alarm.id, user_id=user_id))
    db.commit()
    db.refresh(alarm)
    log_event("alarm_created", alarm_id=alarm.id, assigned_to=user_id, source="test")
    return {"status": "sent", "alarm_id": alarm.id, "assigned_to": user_id}


@router.post("/simulate-watchdog-failure")
def simulate_watchdog_failure(db: Session = Depends(get_db),
                              peer: bool = Query(True)):
    """Simulate watchdog failure by setting all user heartbeats to old."""
    _require_test_endpoints()
    users = db.query(User).all()
    old_time = clock_now() - timedelta(minutes=5)
    for user in users:
        user.last_heartbeat = old_time
        user.is_online = False
    db.commit()
    if peer:
        _broadcast("/api/test/simulate-watchdog-failure")
    return {"status": "simulated", "users_affected": len(users)}


@router.post("/simulate-connection-loss")
def simulate_connection_loss(db: Session = Depends(get_db),
                             peer: bool = Query(True)):
    """Simulate connection loss by marking all users offline.
    Stocke aussi un timestamp Unix pour bloquer les heartbeats des tokens anciens
    (ex: app Android en arrière-plan) — seuls les fresh logins peuvent rétablir le heartbeat."""
    _require_test_endpoints()
    import time as _time
    from . import devices as devices_module
    users = db.query(User).all()
    for user in users:
        user.is_online = False
    db.commit()
    # Bloquer les tokens émis STRICTEMENT AVANT cette seconde
    devices_module.connection_loss_time_int = int(_time.time())
    if peer:
        _broadcast("/api/test/simulate-connection-loss")
    return {"status": "simulated", "users_affected": len(users)}


@router.post("/reset")
def reset_all(db: Session = Depends(get_db),
              peer: bool = Query(True)):
    """Reset all alarms, user states, and restore default escalation chain."""
    _require_test_endpoints()
    db.query(AlarmNotification).delete()
    db.query(Alarm).delete()

    # Reset heartbeat pause et connection loss simulation
    from . import devices as devices_module
    devices_module.heartbeat_paused = False
    devices_module.connection_loss_time_int = None

    # Reset FCM test list
    from ..fcm_service import reset_last_fcm
    reset_last_fcm()

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

    if peer:
        _broadcast("/api/test/reset")
    return {"status": "reset complete"}


@router.post("/toggle-heartbeat-pause")
def toggle_heartbeat_pause():
    """Toggle the heartbeat pause flag. When paused, heartbeat endpoint returns success
    but does NOT update the last_heartbeat timestamp."""
    _require_test_endpoints()
    from . import devices as devices_module
    devices_module.heartbeat_paused = not devices_module.heartbeat_paused
    return {"status": "ok", "paused": devices_module.heartbeat_paused}


@router.post("/trigger-escalation")
def trigger_escalation(db: Session = Depends(get_db)):
    """Exécute un cycle d'escalade forcé (pour tests déterministes).
    Escalade toutes les alarmes actives vers l'utilisateur suivant dans la chaîne,
    indépendamment du délai configuré. Saute les utilisateurs offline."""
    _require_test_endpoints()
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

        # Trouver l'utilisateur suivant (sans filtre online — FCM reveille)
        next_user = _find_next_user(
            db, escalation_chain, current_position, alarm.assigned_user_id
        )

        if next_user and next_user.user_id != alarm.assigned_user_id:
            prev_user = alarm.assigned_user_id
            alarm.assigned_user_id = next_user.user_id
            # Ajouter le nouvel utilisateur à la table des notifiés
            existing_notif = (
                db.query(AlarmNotification)
                .filter(AlarmNotification.alarm_id == alarm.id,
                        AlarmNotification.user_id == next_user.user_id)
                .first()
            )
            if not existing_notif:
                db.add(AlarmNotification(alarm_id=alarm.id, user_id=next_user.user_id))
                db.flush()  # Flush pour que la query notified_ids voie le nouvel ajout
            alarm.status = "escalated"
            alarm.escalation_count += 1
            escalated_count += 1
            notified_ids = [
                n[0] for n in db.query(AlarmNotification.user_id)
                .filter(AlarmNotification.alarm_id == alarm.id).all()
            ]
            log_event("alarm_escalated", alarm_id=alarm.id,
                      from_user=prev_user, to_user=next_user.user_id,
                      notified_user_ids=notified_ids)

            # FCM a tous les notifies (cumulative)
            from ..fcm_service import send_fcm_to_user
            for uid in notified_ids:
                try:
                    send_fcm_to_user(db, uid, alarm.title, alarm.message,
                                     {"alarm_id": str(alarm.id), "severity": alarm.severity})
                except Exception:
                    pass

    db.commit()
    return {"status": "ok", "escalated": escalated_count}


def _find_next_user(db, escalation_chain, current_position, current_user_id):
    """Find the next user in the escalation chain after current_position.
    Wraps around if needed. Ne filtre PAS sur is_online — FCM reveille les users."""
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
        return ec

    return None


@router.get("/last-email-sent")
def get_last_email_sent():
    """Return the last email sent by the system (for testing)."""
    _require_test_endpoints()
    from ..email_service import get_last_email
    return get_last_email()


@router.get("/last-fcm")
def get_last_fcm():
    """Return all FCM notifications sent (for testing)."""
    _require_test_endpoints()
    from ..fcm_service import get_last_fcm_list
    return get_last_fcm_list()


@router.post("/reset-fcm")
def reset_fcm():
    """Reset FCM notification list (for testing)."""
    _require_test_endpoints()
    from ..fcm_service import reset_last_fcm
    reset_last_fcm()
    return {"status": "ok"}


@router.get("/status")
def get_status(db: Session = Depends(get_db)):
    """Get overall system status."""
    _require_test_endpoints()
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
def advance_clock(seconds: float = 0, minutes: float = 0,
                  peer: bool = Query(True)):
    """Avance l'horloge du serveur (pour tests d'escalade avec timing réel).
    Broadcasté au nœud pair pour que la boucle d'escalade — quel que soit le primaire
    courant — voie le même décalage horaire."""
    _require_test_endpoints()
    total = seconds + minutes * 60
    clock_module.advance(total)
    if peer:
        _broadcast("/api/test/advance-clock", {"seconds": seconds, "minutes": minutes})
    return {
        "status": "ok",
        "advanced_seconds": total,
        "total_offset_seconds": clock_module.get_offset_seconds(),
    }


@router.post("/reset-clock")
def reset_clock(peer: bool = Query(True)):
    """Remet l'horloge à l'heure réelle."""
    _require_test_endpoints()
    clock_module.reset()
    if peer:
        _broadcast("/api/test/reset-clock")
    return {"status": "ok", "offset_seconds": 0}


@router.post("/reset-sms-queue")
def reset_sms_queue(db: Session = Depends(get_db)):
    """Vide la table sms_queue (pour les tests)."""
    _require_test_endpoints()
    db.query(SmsQueue).delete()
    db.commit()
    return {"status": "ok"}


@router.post("/insert-sms")
def insert_sms(payload: dict, db: Session = Depends(get_db)):
    """Insère un SMS directement dans sms_queue (pour les tests)."""
    _require_test_endpoints()
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
    _require_test_endpoints()
    from .. import escalation as esc_module
    esc_module.last_tick_at = datetime(2020, 1, 1)
    return {"status": "ok", "last_tick_at": "2020-01-01T00:00:00"}
