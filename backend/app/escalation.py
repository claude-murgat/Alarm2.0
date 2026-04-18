import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Optional
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import Alarm, AlarmNotification, EscalationConfig, User, SystemConfig, SmsQueue, CallQueue
from .clock import now as clock_now
from .email_service import send_alert_email
from .fcm_service import send_fcm_to_user
from .events import log_event
from .logging_config import correlation_id_var
from .logic.ack_expiry import evaluate_ack_expiry
from .logic.escalation import evaluate_escalation
from .logic.sms_timer import evaluate_sms_call_timers
from .logic.oncall import evaluate_oncall_heartbeat
from .logic.models import (
    AlarmSnapshot, EscalationChainEntry, NotificationSnapshot, UserSnapshot,
)

logger = logging.getLogger("escalation")

ONCALL_OFFLINE_DELAY_MINUTES = 15.0

# Mis à jour à chaque tick — utilisé par /health pour détecter une boucle bloquée
last_tick_at: Optional[object] = None

# Flag de test : quand True, la boucle NE met PAS à jour last_tick_at.
# Permet au endpoint /test/simulate-loop-stall de figer l'état stall de manière déterministe.
# Remis à False par /test/clear-loop-stall.
_simulate_stall: bool = False


def _add_notified_user(db: Session, alarm, user_id: int):
    """Ajoute un user_id à la table alarm_notifications s'il n'y est pas déjà."""
    existing = (
        db.query(AlarmNotification)
        .filter(AlarmNotification.alarm_id == alarm.id, AlarmNotification.user_id == user_id)
        .first()
    )
    if not existing:
        db.add(AlarmNotification(alarm_id=alarm.id, user_id=user_id, notified_at=clock_now()))


def _get_notified_user_ids(db: Session, alarm_id: int) -> list[int]:
    """Retourne la liste des user_ids notifiés pour une alarme."""
    notifs = (
        db.query(AlarmNotification.user_id)
        .filter(AlarmNotification.alarm_id == alarm_id)
        .all()
    )
    return [n[0] for n in notifs]


def _alarm_to_snapshot(alarm: Alarm) -> AlarmSnapshot:
    """Convertit un objet ORM Alarm en AlarmSnapshot pour les fonctions pures.
    L'appelant est responsable d'appliquer les Actions retournees sur l'Alarm ORM."""
    return AlarmSnapshot(
        id=alarm.id,
        status=alarm.status,
        created_at=alarm.created_at,
        suspended_until=alarm.suspended_until,
        assigned_user_id=alarm.assigned_user_id,
        escalation_count=alarm.escalation_count,
        is_oncall_alarm=alarm.is_oncall_alarm,
    )


def _enqueue_sms_for_user(db: Session, user: User, alarm: Alarm):
    """Enqueue un SMS pour un utilisateur qui a un numéro de téléphone.
    Guard anti-doublon : ne crée pas de SMS si un identique non-envoyé existe déjà."""
    if not user.phone_number:
        return
    body = f"ALARME {alarm.severity.upper()}: {alarm.title}"
    existing = (
        db.query(SmsQueue)
        .filter(
            SmsQueue.to_number == user.phone_number,
            SmsQueue.body == body,
            SmsQueue.sent_at == None,
            SmsQueue.retries < 3,
        )
        .first()
    )
    if existing:
        return  # SMS déjà en attente pour cet utilisateur/alarme
    sms = SmsQueue(to_number=user.phone_number, body=body)
    db.add(sms)
    logger.info(f"SMS enqueued for {user.name} ({user.phone_number}) — alarm {alarm.id}")


def _enqueue_call_for_user(db: Session, user: User, alarm: Alarm):
    """Enqueue un appel vocal pour un utilisateur qui a un numero de telephone.
    Guard anti-doublon : ne cree pas de call si un identique non-traite existe deja."""
    if not user.phone_number:
        return
    tts_message = f"Alarme critique: {alarm.title}. Appuyez 1 pour acquitter, 2 pour escalader."
    existing = (
        db.query(CallQueue)
        .filter(
            CallQueue.to_number == user.phone_number,
            CallQueue.alarm_id == alarm.id,
            CallQueue.called_at == None,
            CallQueue.retries < 3,
        )
        .first()
    )
    if existing:
        return  # Call deja en attente pour cet utilisateur/alarme
    call = CallQueue(
        to_number=user.phone_number,
        alarm_id=alarm.id,
        user_id=user.id,
        tts_message=tts_message,
    )
    db.add(call)
    logger.info(f"Call enqueued for {user.name} ({user.phone_number}) — alarm {alarm.id}")


async def escalation_loop():
    """Background task: ack expiry, escalation, and on-call monitoring.

    Ce nœud n'exécute la logique d'escalade que s'il est primaire (advisory lock acquis).
    En secondaire, la coroutine tourne mais dort — last_tick_at est toujours mis à jour
    pour que /health signale correctement que la boucle est vivante.
    """
    global last_tick_at
    from .leader_election import is_leader
    while True:
        try:
            correlation_id_var.set(str(uuid.uuid4()))
            now = clock_now()
            if not _simulate_stall:
                last_tick_at = now  # Toujours mis à jour — permet à /health de détecter un blocage

            if not is_leader.is_set():
                # Nœud secondaire : standby, pas d'escalade
                await asyncio.sleep(10)
                continue

            db: Session = SessionLocal()
            try:

                # --- 1. Ack expiry: reactivate acknowledged alarms ---
                # Logique de decision extraite dans logic/ack_expiry.py (testee unit).
                # Ici on ne fait que charger l'etat + appliquer les Actions retournees.
                all_ack = db.query(Alarm).filter(Alarm.status == "acknowledged").all()
                snapshots = [_alarm_to_snapshot(a) for a in all_ack]
                reactivations = evaluate_ack_expiry(snapshots, now)
                ack_by_id = {a.id: a for a in all_ack}
                for reactivation in reactivations:
                    alarm = ack_by_id[reactivation.alarm_id]
                    logger.info(f"Ack expired for alarm {alarm.id}, reactivating")
                    alarm.status = "active"
                    alarm.suspended_until = None
                    alarm.created_at = now
                    # Reset SMS/Call flags pour permettre un nouveau cycle
                    notifs = (
                        db.query(AlarmNotification)
                        .filter(AlarmNotification.alarm_id == alarm.id)
                        .all()
                    )
                    for notif in notifs:
                        notif.sms_sent = False
                        notif.call_sent = False
                        notif.notified_at = now
                    log_event("escalation_timeout", db=db, alarm_id=alarm.id)
                    db.commit()

                # --- 2. Escalation of active/escalated alarms ---
                # Logique de decision extraite dans logic/escalation.py (testee unit).
                # Delai UNIFORME pour tous (INV-011) + FCM wake-up si offline (INV-015b).
                active_alarms = (
                    db.query(Alarm)
                    .filter(Alarm.status.in_(["active", "escalated"]))
                    .all()
                )

                # escalation_chain charge AU NIVEAU DE LA BOUCLE (pas conditionnel)
                # car utilise aussi par _check_oncall_heartbeat (section 4).
                escalation_chain = (
                    db.query(EscalationConfig)
                    .order_by(EscalationConfig.position)
                    .all()
                )

                if escalation_chain and active_alarms:
                    # Delai uniforme (INV-011) depuis SystemConfig
                    delay_cfg = db.query(SystemConfig).filter(
                        SystemConfig.key == "escalation_delay_minutes"
                    ).first()
                    delay_minutes = float(delay_cfg.value) if delay_cfg else 15.0

                    # Snapshot des users online (pour FCM wake-up INV-015b)
                    users_online = {u.id: u.is_online for u in db.query(User).all()}

                    # Snapshots pour la fonction pure
                    chain_snapshot = [
                        EscalationChainEntry(position=ec.position, user_id=ec.user_id)
                        for ec in escalation_chain
                    ]
                    alarm_snapshots = [_alarm_to_snapshot(a) for a in active_alarms]

                    actions = evaluate_escalation(
                        alarm_snapshots, chain_snapshot, users_online, delay_minutes, now
                    )

                    alarm_by_id = {a.id: a for a in active_alarms}

                    # INV-015b : FCM wake-up aux users courants offline AVANT l'escalade
                    for wake_up in actions.wake_ups:
                        alarm = alarm_by_id[wake_up.alarm_id]
                        try:
                            send_fcm_to_user(db, wake_up.user_id, alarm.title, alarm.message,
                                             {"alarm_id": str(alarm.id), "severity": alarm.severity,
                                              "wake_up": "true"})
                        except Exception:
                            pass  # FCM best-effort

                    # Alarmes qui meritent un FCM cumulative :
                    # - celles qui sont escaladees (decisions)
                    # - celles dont elapsed >= delay meme sans escalade (ex : chaine a 1 user)
                    # Calcul AVANT de reset alarm.created_at sur les escalades.
                    alarm_ids_needing_reminder = {d.alarm_id for d in actions.escalations}
                    for alarm_snap in alarm_snapshots:
                        if alarm_snap.status not in ("active", "escalated"):
                            continue
                        elapsed = (now - alarm_snap.created_at).total_seconds() / 60.0
                        if elapsed >= delay_minutes:
                            alarm_ids_needing_reminder.add(alarm_snap.id)

                    # Escalades
                    for decision in actions.escalations:
                        alarm = alarm_by_id[decision.alarm_id]
                        logger.info(
                            f"Escalating alarm {alarm.id} from user {decision.from_user_id} "
                            f"to user {decision.to_user_id}"
                        )
                        alarm.assigned_user_id = decision.to_user_id
                        _add_notified_user(db, alarm, decision.to_user_id)
                        alarm.status = "escalated"
                        alarm.escalation_count += 1
                        # Reset timer pour le prochain palier
                        alarm.created_at = now
                        notified_ids = _get_notified_user_ids(db, alarm.id)
                        log_event("alarm_escalated", db=db, alarm_id=alarm.id,
                                  from_user=decision.from_user_id, to_user=decision.to_user_id,
                                  notified_user_ids=notified_ids)

                    # FCM cumulative : rappel aux notifies pour toutes les alarmes eligibles.
                    for alarm_id in alarm_ids_needing_reminder:
                        alarm = alarm_by_id.get(alarm_id)
                        if alarm is None:
                            continue
                        notified_ids = _get_notified_user_ids(db, alarm.id)
                        for uid in notified_ids:
                            try:
                                send_fcm_to_user(db, uid, alarm.title, alarm.message,
                                                 {"alarm_id": str(alarm.id),
                                                  "severity": alarm.severity})
                            except Exception:
                                pass  # FCM best-effort
                    db.commit()

                # --- 3. Timer-based SMS/Call enqueue ---
                # Logique de decision extraite dans logic/sms_timer.py (testee unit).
                sms_call_delay_cfg = db.query(SystemConfig).filter(
                    SystemConfig.key == "sms_call_delay_minutes"
                ).first()
                sms_call_delay = float(sms_call_delay_cfg.value) if sms_call_delay_cfg else 2.0

                if active_alarms:
                    active_alarm_ids = [a.id for a in active_alarms]
                    all_notifs_orm = (
                        db.query(AlarmNotification)
                        .filter(AlarmNotification.alarm_id.in_(active_alarm_ids))
                        .all()
                    )
                    notif_snapshots = [
                        NotificationSnapshot(
                            id=n.id,
                            alarm_id=n.alarm_id,
                            user_id=n.user_id,
                            notified_at=n.notified_at,
                            sms_sent=n.sms_sent,
                            call_sent=n.call_sent,
                        )
                        for n in all_notifs_orm
                    ]
                    alarm_snapshots_for_sms = [_alarm_to_snapshot(a) for a in active_alarms]

                    sms_call_actions = evaluate_sms_call_timers(
                        alarm_snapshots_for_sms,
                        notif_snapshots,
                        sms_call_delay,
                        now,
                    )

                    # Appliquer : insert DB + set flag sms_sent / call_sent
                    notif_by_id = {n.id: n for n in all_notifs_orm}
                    alarm_by_id_sms = {a.id: a for a in active_alarms}

                    for sms_enq in sms_call_actions.sms_enqueues:
                        notif = notif_by_id[sms_enq.notification_id]
                        alarm = alarm_by_id_sms[sms_enq.alarm_id]
                        user = db.query(User).filter(User.id == sms_enq.user_id).first()
                        if user:
                            _enqueue_sms_for_user(db, user, alarm)
                            notif.sms_sent = True

                    for call_enq in sms_call_actions.call_enqueues:
                        notif = notif_by_id[call_enq.notification_id]
                        alarm = alarm_by_id_sms[call_enq.alarm_id]
                        user = db.query(User).filter(User.id == call_enq.user_id).first()
                        if user:
                            _enqueue_call_for_user(db, user, alarm)
                            notif.call_sent = True

                    db.commit()

                # --- 4. On-call monitoring: user #1 heartbeat ---
                # Logique de decision extraite dans logic/oncall.py (testee unit).
                _apply_oncall_heartbeat(db, now, escalation_chain)

            finally:
                db.close()
        except Exception as e:
            logger.error(f"Escalation error: {e}")

        await asyncio.sleep(10)


def _apply_oncall_heartbeat(db: Session, now, escalation_chain):
    """Applique les actions retournees par evaluate_oncall_heartbeat (logique pure).

    Charge les snapshots, appelle la fonction pure, applique les Actions en DB + SMTP.
    """
    if not escalation_chain:
        return

    chain_snapshot = [
        EscalationChainEntry(position=ec.position, user_id=ec.user_id)
        for ec in escalation_chain
    ]
    user_snapshots = [
        UserSnapshot(
            id=u.id,
            name=u.name,
            is_online=u.is_online,
            last_heartbeat=u.last_heartbeat,
        )
        for u in db.query(User).all()
    ]
    # Alarmes is_oncall_alarm + alarmes actives (pour l'unicite INV-001)
    alarms_orm = db.query(Alarm).filter(
        Alarm.status.in_(["active", "escalated", "resolved"])
    ).all()
    alarm_snapshots = [_alarm_to_snapshot(a) for a in alarms_orm]

    actions = evaluate_oncall_heartbeat(
        chain_snapshot,
        user_snapshots,
        alarm_snapshots,
        ONCALL_OFFLINE_DELAY_MINUTES,
        now,
    )

    alarm_by_id = {a.id: a for a in alarms_orm}

    # INV-051 : resoudre les alarmes oncall existantes
    for resolution in actions.resolutions:
        alarm = alarm_by_id.get(resolution.alarm_id)
        if alarm is not None:
            logger.info(
                f"On-call user back online, resolving oncall alarm {alarm.id}"
            )
            alarm.status = "resolved"
            db.commit()

    # INV-053 : email direction technique (personne online)
    for email in actions.emails:
        config = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
        recipient = config.value if config else "direction_technique@charlesmurgat.com"
        send_alert_email(
            subject="Alerte: aucun utilisateur connecte - astreinte perdue",
            body=(
                f"L'utilisateur d'astreinte '{email.oncall_user_name}' est hors ligne depuis "
                f"{email.offline_duration_minutes:.0f} minutes et aucun autre utilisateur n'est connecte."
            ),
            to=recipient,
        )

    # INV-050 : creer l'alarme oncall
    for creation in actions.creations:
        alarm = Alarm(
            title=f"Utilisateur d'astreinte hors connexion ({creation.oncall_user_name})",
            message=(
                f"{creation.oncall_user_name} est hors ligne depuis "
                f"{creation.offline_duration_minutes:.0f} minutes"
            ),
            severity="critical",
            assigned_user_id=creation.assigned_user_id,
            is_oncall_alarm=True,
        )
        db.add(alarm)
        db.flush()  # Obtenir l'ID avant d'ajouter la notification
        _add_notified_user(db, alarm, creation.assigned_user_id)
        db.commit()
        logger.warning(
            f"On-call alarm created: {creation.oncall_user_name} offline for "
            f"{creation.offline_duration_minutes:.0f}min, assigned to user {creation.assigned_user_id}"
        )


def _find_next_user(db, escalation_chain, current_position, current_user_id):
    """Find the next user in the escalation chain after current_position.
    Wraps around if needed. Ne filtre PAS sur is_online — le FCM se charge du reveil."""
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
        # Plus de filtre is_online — l'escalade suit l'ordre de la chaine
        return ec

    return None
