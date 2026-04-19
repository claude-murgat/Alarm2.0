"""Logique pure : décision de création d'alarme.

Invariant couvert :
- INV-080 : chaîne d'escalade vide + alarme → email direction technique
  + assignation fallback au premier user existant

Logique :
- Si requested_assigned_user_id fourni → respecter (override opérateur).
- Sinon, assigner au premier de la chaîne.
- Si chaîne vide → fallback au premier user existant + drapeau email.
"""
from typing import Optional

from .models import (
    AlarmCreationPlan,
    EscalationChainEntry,
    UserSnapshot,
)


def evaluate_alarm_creation_plan(
    requested_assigned_user_id: Optional[int],
    chain: list[EscalationChainEntry],
    users: list[UserSnapshot],
) -> AlarmCreationPlan:
    """Retourne le plan de création d'alarme à appliquer.

    Args:
        requested_assigned_user_id : valeur explicite dans la requête (peut être None).
        chain : chaîne d'escalade triée par position.
        users : liste des users existants (pour fallback si chaîne vide).
    """
    # INV-080 : chaîne vide déclenche un email direction technique.
    chain_empty = not chain
    email_reason: Optional[str] = "chain_empty" if chain_empty else None

    if requested_assigned_user_id is not None:
        # Override explicite : on respecte.
        return AlarmCreationPlan(
            assigned_user_id=requested_assigned_user_id,
            needs_direction_technique_email=chain_empty,
            email_reason=email_reason,
        )

    # Pas d'assignation explicite : prendre le 1er de la chaîne.
    if chain:
        return AlarmCreationPlan(
            assigned_user_id=chain[0].user_id,
            needs_direction_technique_email=False,
            email_reason=None,
        )

    # Chaîne vide : fallback au premier user existant (ou None si aucun).
    fallback_user_id = users[0].id if users else None
    return AlarmCreationPlan(
        assigned_user_id=fallback_user_id,
        needs_direction_technique_email=True,
        email_reason="chain_empty",
    )
