"""Dataclasses frozen pour snapshots et Actions de la logique pure.

Principe : ces types sont ce que voient les fonctions pures. Aucun lien avec
SQLAlchemy — l'appelant convertit les objets ORM en snapshots avant d'appeler,
et applique les Actions retournées sur la DB.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class AlarmSnapshot:
    """État d'une alarme à un instant donné, vu par la logique pure.

    Pas de relation ORM : on passe les IDs, l'appelant charge les liens si besoin.
    """
    id: int
    status: str                         # active | acknowledged | resolved | escalated
    created_at: datetime                # timer d'escalade (réécrit à chaque escalade)
    suspended_until: Optional[datetime]  # si status=acknowledged
    assigned_user_id: Optional[int]
    escalation_count: int
    is_oncall_alarm: bool


@dataclass(frozen=True)
class AckReactivation:
    """Action : réactiver une alarme dont l'ack a expiré.

    L'appelant doit appliquer :
    - alarm.status = 'active'
    - alarm.suspended_until = None
    - alarm.created_at = now  (reset timer d'escalade pour le nouveau cycle)
    - pour chaque AlarmNotification liée :
        notif.sms_sent = False
        notif.call_sent = False
        notif.notified_at = now
    - log_event("escalation_timeout", alarm_id=alarm_id)
    """
    alarm_id: int


@dataclass(frozen=True)
class EscalationChainEntry:
    """Entrée de la chaîne d'escalade, triée par position croissante."""
    position: int
    user_id: int


@dataclass(frozen=True)
class EscalationDecision:
    """Action : escalader une alarme du user courant vers le suivant.

    L'appelant doit appliquer :
    - alarm.assigned_user_id = to_user_id
    - _add_notified_user(alarm, to_user_id)  (si pas déjà dans la table)
    - alarm.status = 'escalated'
    - alarm.escalation_count += 1
    - alarm.created_at = now  (reset timer pour le nouveau palier)
    - log_event("alarm_escalated", alarm_id, from_user=from_user_id, to_user=to_user_id)
    - send_fcm_to_user(uid, ...) pour chaque uid dans notified_user_ids (cumulative)
    """
    alarm_id: int
    from_user_id: int
    to_user_id: int


@dataclass(frozen=True)
class FCMWakeUp:
    """Action : envoyer un FCM high-priority pour réveiller un user offline
    AVANT de passer au suivant dans la chaîne d'escalade (INV-015b).

    L'appelant doit appliquer :
    - send_fcm_to_user(user_id, alarm.title, alarm.message, data={...}) avec priorité HIGH
    """
    alarm_id: int
    user_id: int


@dataclass(frozen=True)
class EscalationActions:
    """Retour de evaluate_escalation : liste des décisions d'escalade et des FCM de réveil.

    L'appelant applique d'abord les wake_ups puis les escalations (même tick).
    """
    escalations: tuple["EscalationDecision", ...]
    wake_ups: tuple["FCMWakeUp", ...]
