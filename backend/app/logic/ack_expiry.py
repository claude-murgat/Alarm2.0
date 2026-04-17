"""Logique pure : détection des alarmes dont l'acquittement a expiré.

Invariant INV-016 : "Si suspended_until < now et status='acknowledged',
au prochain tick l'alarme doit être réactivée (status → 'active', created_at = now)."

Cette fonction retourne la liste des alarmes qui doivent être réactivées.
L'appelant applique les changements (voir AckReactivation.docstring).
"""
from datetime import datetime

from .models import AlarmSnapshot, AckReactivation


def evaluate_ack_expiry(
    alarms: list[AlarmSnapshot],
    now: datetime,
) -> list[AckReactivation]:
    """Retourne les alarmes dont l'acquittement a expiré et qui doivent être réactivées.

    Critère : status='acknowledged' ET suspended_until non-NULL ET suspended_until < now.

    Les alarmes dans d'autres états (active, escalated, resolved) ou acknowledged
    sans suspended_until (cas dégénéré, ne devrait pas arriver) sont ignorées.
    """
    return [
        AckReactivation(alarm_id=a.id)
        for a in alarms
        if a.status == "acknowledged"
        and a.suspended_until is not None
        and a.suspended_until < now
    ]
