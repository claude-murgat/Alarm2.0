"""Logique pure : décisions d'escalade pour les alarmes actives.

Invariants couverts (voir tests/INVARIANTS.md) :
- INV-010 : escalade si elapsed >= delay (comparaison strictement >=)
- INV-011 : délai UNIFORME pour chaque position (plus de split astreinte/veille)
- INV-013 : wrap-around (après le dernier de la chaîne, repart au premier)
- INV-014 : ignore is_online pour choisir le next_user (FCM réveille)
- INV-015 : pas d'escalade si status='acknowledged' ou 'resolved'
- INV-015b : FCM wake-up high-priority au user courant SI offline, avant l'escalade

Cette fonction ne produit PAS d'effets de bord. L'appelant applique :
- Pour chaque FCMWakeUp : send_fcm_to_user (high priority)
- Pour chaque EscalationDecision : update DB + log_event + FCM cumulative
"""
from datetime import datetime
from typing import Optional

from .models import (
    AlarmSnapshot,
    EscalationActions,
    EscalationChainEntry,
    EscalationDecision,
    FCMWakeUp,
)


def evaluate_escalation(
    alarms: list[AlarmSnapshot],
    chain: list[EscalationChainEntry],
    users_online: dict[int, bool],
    delay_minutes: float,
    now: datetime,
) -> EscalationActions:
    """Retourne les escalades et FCM wake-ups à appliquer pour ce tick.

    Args:
        alarms : snapshot de toutes les alarmes (seules les 'active'/'escalated' sont considérées).
        chain : chaîne d'escalade triée par position croissante (pos 1, 2, 3, ...).
        users_online : dict user_id -> is_online. Utilisé UNIQUEMENT pour décider du FCM wake-up
                       (pas pour choisir le next_user, cf INV-014).
        delay_minutes : délai uniforme entre chaque palier (INV-011).
        now : horloge injectable (cf backend/app/clock.py).
    """
    if not chain:
        return EscalationActions(escalations=(), wake_ups=())

    escalations: list[EscalationDecision] = []
    wake_ups: list[FCMWakeUp] = []

    for alarm in alarms:
        if alarm.status not in ("active", "escalated"):
            continue

        elapsed_minutes = (now - alarm.created_at).total_seconds() / 60.0
        if elapsed_minutes < delay_minutes:
            continue

        next_user_id = _find_next_user_id(chain, alarm.assigned_user_id)
        if next_user_id is None or next_user_id == alarm.assigned_user_id:
            continue

        # INV-015b : si le user courant est offline, FCM wake-up avant d'escalader
        if alarm.assigned_user_id is not None and not users_online.get(alarm.assigned_user_id, True):
            wake_ups.append(FCMWakeUp(alarm_id=alarm.id, user_id=alarm.assigned_user_id))

        escalations.append(
            EscalationDecision(
                alarm_id=alarm.id,
                from_user_id=alarm.assigned_user_id,
                to_user_id=next_user_id,
            )
        )

    return EscalationActions(
        escalations=tuple(escalations),
        wake_ups=tuple(wake_ups),
    )


def _find_next_user_id(
    chain: list[EscalationChainEntry],
    current_user_id: Optional[int],
) -> Optional[int]:
    """Trouve le user_id suivant dans la chaîne après celui courant.

    Règles (INV-013, INV-014) :
    - Si current_user_id n'est pas dans la chaîne : retourner le premier de la chaîne.
    - Sinon : retourner le suivant en position, avec wrap-around au premier après le dernier.
    - Skip le current_user_id (évite d'escalader vers soi-même).
    """
    if not chain:
        return None

    # Position actuelle (-1 si current pas dans la chaîne)
    current_position = -1
    for entry in chain:
        if entry.user_id == current_user_id:
            current_position = entry.position
            break

    # Construire la liste ordonnée : d'abord ceux après current_position,
    # puis ceux avant (wrap-around)
    after = [e for e in chain if e.position > current_position]
    before = [e for e in chain if e.position <= current_position]
    candidates = after + before

    for entry in candidates:
        if entry.user_id != current_user_id:
            return entry.user_id

    return None
