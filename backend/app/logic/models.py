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
