import asyncio
import logging
from datetime import timedelta
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import Alarm, EscalationConfig, User, SystemConfig
from .clock import now as clock_now
from .email_service import send_alert_email

logger = logging.getLogger("escalation")

ONCALL_OFFLINE_DELAY_MINUTES = 15.0


def _add_notified_user(alarm, user_id: int):
    """Ajoute un user_id à notified_user_ids."""
    raw = alarm.notified_user_ids or ""
    current_ids = [int(x) for x in raw.split(",") if x.strip()]
    if user_id not in current_ids:
        current_ids.append(user_id)
    alarm.notified_user_ids = ",".join(str(x) for x in current_ids)


async def escalation_loop():
    """Background task: ack expiry, escalation, and on-call monitoring."""
    while True:
        try:
            db: Session = SessionLocal()
            try:
                now = clock_now()

                # --- 1. Ack expiry: reactivate acknowledged alarms ---
                ack_alarms = (
                    db.query(Alarm)
                    .filter(
                        Alarm.status == "acknowledged",
                        Alarm.suspended_until != None,
                        Alarm.suspended_until < now,
                    )
                    .all()
                )
                for alarm in ack_alarms:
                    logger.info(f"Ack expired for alarm {alarm.id}, reactivating")
                    alarm.status = "active"
                    alarm.suspended_until = None
                    alarm.created_at = now
                    db.commit()

                # --- 2. Escalation of active/escalated alarms ---
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

                for alarm in active_alarms:
                    if not escalation_chain:
                        continue

                    current_position = -1
                    current_delay = 15.0
                    for ec in escalation_chain:
                        if ec.user_id == alarm.assigned_user_id:
                            current_position = ec.position
                            current_delay = ec.delay_minutes
                            break

                    elapsed = (now - alarm.created_at).total_seconds() / 60.0
                    escalation_threshold = current_delay * (alarm.escalation_count + 1)

                    if elapsed >= escalation_threshold:
                        next_user = _find_next_online_user(
                            db, escalation_chain, current_position, alarm.assigned_user_id
                        )

                        if next_user and next_user.user_id != alarm.assigned_user_id:
                            logger.info(
                                f"Escalating alarm {alarm.id} from user {alarm.assigned_user_id} "
                                f"to user {next_user.user_id} (position {next_user.position})"
                            )
                            alarm.assigned_user_id = next_user.user_id
                            # Ajouter le nouvel utilisateur à la liste cumulative des notifiés
                            _add_notified_user(alarm, next_user.user_id)
                            alarm.status = "escalated"
                            alarm.escalation_count += 1
                            db.commit()

                # --- 3. On-call monitoring: user #1 heartbeat ---
                _check_oncall_heartbeat(db, now, escalation_chain)

            finally:
                db.close()
        except Exception as e:
            logger.error(f"Escalation error: {e}")

        await asyncio.sleep(10)


def _check_oncall_heartbeat(db: Session, now, escalation_chain):
    """Vérifie si l'utilisateur d'astreinte (#1) a perdu son heartbeat.
    Si oui depuis > 15 min, crée une alarme automatique.
    Si #1 revient en ligne, auto-résout l'alarme.
    Si personne n'est connecté, envoie un email."""

    if not escalation_chain:
        return

    # L'utilisateur d'astreinte est le #1 (plus petite position)
    oncall_ec = escalation_chain[0]
    oncall_user = db.query(User).filter(User.id == oncall_ec.user_id).first()
    if not oncall_user:
        return

    # Vérifier s'il y a déjà une alarme d'astreinte active
    existing_oncall = (
        db.query(Alarm)
        .filter(Alarm.is_oncall_alarm == True, Alarm.status.in_(["active", "escalated"]))
        .first()
    )

    # Si l'utilisateur d'astreinte est en ligne → résoudre l'alarme d'astreinte si elle existe
    if oncall_user.is_online:
        if existing_oncall:
            logger.info(f"On-call user {oncall_user.name} back online, resolving oncall alarm {existing_oncall.id}")
            existing_oncall.status = "resolved"
            db.commit()
        return

    # L'utilisateur d'astreinte est offline
    if not oncall_user.last_heartbeat:
        return  # Jamais eu de heartbeat, on ne peut pas savoir

    # Depuis combien de temps est-il offline ?
    offline_duration = (now - oncall_user.last_heartbeat).total_seconds() / 60.0

    if offline_duration < ONCALL_OFFLINE_DELAY_MINUTES:
        return  # Pas encore assez longtemps

    # Vérifier si quelqu'un est connecté
    online_users = db.query(User).filter(User.is_online == True).all()

    if not online_users:
        # Personne connecté → email direction technique
        config = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
        recipient = config.value if config else "direction_technique@charlesmurgat.com"
        send_alert_email(
            subject="Alerte: aucun utilisateur connecté — astreinte perdue",
            body=(
                f"L'utilisateur d'astreinte '{oncall_user.name}' est hors ligne depuis "
                f"{offline_duration:.0f} minutes et aucun autre utilisateur n'est connecté."
            ),
            to=recipient,
        )
        return

    # Il y a des gens connectés mais pas le #1 → créer l'alarme d'astreinte
    if existing_oncall:
        return  # Alarme déjà active, l'escalade se charge du reste

    # Trouver le prochain utilisateur online dans la chaîne (pas le #1)
    assigned_user_id = None
    for ec in escalation_chain:
        if ec.user_id == oncall_user.id:
            continue
        u = db.query(User).filter(User.id == ec.user_id).first()
        if u and u.is_online:
            assigned_user_id = u.id
            break

    if not assigned_user_id and online_users:
        assigned_user_id = online_users[0].id

    if not assigned_user_id:
        return

    # Vérifier qu'il n'y a pas déjà une alarme active (contrainte alarme unique)
    any_active = db.query(Alarm).filter(Alarm.status.in_(["active", "escalated"])).first()
    if any_active:
        return  # Ne pas créer de doublon

    alarm = Alarm(
        title=f"Utilisateur d'astreinte hors connexion ({oncall_user.name})",
        message=f"{oncall_user.name} est hors ligne depuis {offline_duration:.0f} minutes",
        severity="critical",
        assigned_user_id=assigned_user_id,
        is_oncall_alarm=True,
    )
    _add_notified_user(alarm, assigned_user_id)
    db.add(alarm)
    db.commit()
    logger.warning(
        f"On-call alarm created: {oncall_user.name} offline for {offline_duration:.0f}min, "
        f"assigned to user {assigned_user_id}"
    )


def _find_next_online_user(db, escalation_chain, current_position, current_user_id):
    """Find the next online user in the escalation chain after current_position.
    Wraps around if needed. Skips users who are known offline."""
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
        if user.last_heartbeat is not None and not user.is_online:
            continue
        return ec

    return None
