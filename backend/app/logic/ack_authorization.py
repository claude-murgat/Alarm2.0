"""Logique pure : autorisation d'acquittement d'une alarme (INV-031).

INV-031 : POST /alarms/{id}/ack doit retourner 403 si current_user.id n'est pas
dans alarm.notified_user_ids. Seuls les users notifiés peuvent acquitter.

Évite qu'un user tiers (admin qui n'est pas dans la chaîne active, user qui tape
la mauvaise URL) acquitte par erreur une alarme destinée à quelqu'un d'autre.
"""
from .models import AckAuthorization


def evaluate_ack_authorization(
    notified_user_ids: list[int],
    current_user_id: int,
) -> AckAuthorization:
    """Retourne la décision d'autorisation pour ACK.

    Args:
        notified_user_ids : IDs des users notifiés pour cette alarme (table alarm_notifications).
        current_user_id : ID de l'user qui tente l'ACK.
    """
    if current_user_id in notified_user_ids:
        return AckAuthorization(allowed=True, reason=None)
    return AckAuthorization(allowed=False, reason="not_notified")
