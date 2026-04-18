"""Logique pure : décisions d'enqueue SMS + Call basées sur le timer notified_at.

Invariants couverts :
- INV-060 : SMS + Call enqueued quand (now - notified_at) >= sms_call_delay_minutes
- INV-062/063 : anti-doublon via flags sms_sent / call_sent
- Ne PAS envoyer pour les alarmes status='acknowledged' ou 'resolved'

L'appelant applique :
- Pour chaque SmsEnqueue : insérer dans SmsQueue + notif.sms_sent = True
- Pour chaque CallEnqueue : insérer dans CallQueue + notif.call_sent = True

Le filtre is_oncall_alarm et phone_number NULL est laissé à l'appelant (il a besoin
de la DB users pour vérifier phone_number — le but ici est la décision de timing,
pas la sélection de destination).
"""
from datetime import datetime

from .models import (
    AlarmSnapshot,
    CallEnqueue,
    NotificationSnapshot,
    SmsCallActions,
    SmsEnqueue,
)


def evaluate_sms_call_timers(
    alarms: list[AlarmSnapshot],
    notifications: list[NotificationSnapshot],
    sms_call_delay_minutes: float,
    now: datetime,
) -> SmsCallActions:
    """Retourne les SMS/Call à enqueue selon le timer notified_at.

    Règles :
    - notif.notified_at doit être non-NULL (cas dégénéré → skip sans crash)
    - alarm correspondante doit exister et être dans (active, escalated)
    - (now - notified_at) >= sms_call_delay_minutes
    - sms_sent=False pour SMS, call_sent=False pour Call (anti-doublon)
    """
    alarms_by_id = {a.id: a for a in alarms}

    sms_enqueues: list[SmsEnqueue] = []
    call_enqueues: list[CallEnqueue] = []

    for notif in notifications:
        if notif.notified_at is None:
            continue

        alarm = alarms_by_id.get(notif.alarm_id)
        if alarm is None:
            continue
        if alarm.status not in ("active", "escalated"):
            continue

        elapsed_minutes = (now - notif.notified_at).total_seconds() / 60.0
        if elapsed_minutes < sms_call_delay_minutes:
            continue

        if not notif.sms_sent:
            sms_enqueues.append(
                SmsEnqueue(
                    notification_id=notif.id,
                    alarm_id=notif.alarm_id,
                    user_id=notif.user_id,
                )
            )
        if not notif.call_sent:
            call_enqueues.append(
                CallEnqueue(
                    notification_id=notif.id,
                    alarm_id=notif.alarm_id,
                    user_id=notif.user_id,
                )
            )

    return SmsCallActions(
        sms_enqueues=tuple(sms_enqueues),
        call_enqueues=tuple(call_enqueues),
    )
