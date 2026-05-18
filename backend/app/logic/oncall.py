"""Logique pure : surveillance de l'utilisateur d'astreinte (position 1).

Invariants couverts :
- INV-050 : oncall offline >= delay → alarme auto créée
- INV-051 : oncall revient online → alarme oncall auto-résolue
- INV-052 : alarme oncall assignée au SUIVANT online, pas au #1
- INV-053 : personne en ligne → email direction technique (pas d'alarme créée)
- INV-054 : pas de doublon d'alarme oncall (si une existe active/escalated → skip)
- INV-055 : seul position 1 de la chaîne déclenche le check
- INV-001 : pas de création si une autre alarme active existe (contrainte unicité)

L'appelant applique les Actions retournées en DB + SMTP.
"""
from datetime import datetime

from .models import (
    AlarmSnapshot,
    DirectionTechniqueEmail,
    EscalationChainEntry,
    OncallActions,
    OncallAlarmCreation,
    OncallAlarmResolution,
    UserSnapshot,
)


def evaluate_oncall_heartbeat(
    chain: list[EscalationChainEntry],
    users: list[UserSnapshot],
    existing_alarms: list[AlarmSnapshot],
    oncall_offline_delay_minutes: float,
    now: datetime,
    email_already_sent: bool = False,
) -> OncallActions:
    """Retourne les Actions oncall à appliquer pour ce tick.

    La logique actuelle produit au plus UN élément dans chaque tuple par tick :
    - Soit une resolution (oncall revient online + alarme oncall existe)
    - Soit une creation (oncall offline >= delay + au moins 1 user online + pas de doublon)
    - Soit un email (oncall offline >= delay + personne online, AU PLUS UNE FOIS par épisode)
    - Soit rien (conditions non remplies ou cas dégénérés)

    `email_already_sent` (issue #116) : True si l'email "personne online" a déjà
    été envoyé pour l'épisode courant (marker persistant côté caller). Dans ce cas
    on n'envoie PAS un nouvel email tant qu'au moins un user n'est pas revenu
    online (qui clear le marker via `email_marker_clear`).
    """
    if not chain:
        return OncallActions()

    oncall_entry = chain[0]  # position 1
    users_by_id = {u.id: u for u in users}
    oncall_user = users_by_id.get(oncall_entry.user_id)
    if oncall_user is None:
        # Cas dégénéré : chain pointe vers un user supprimé
        return OncallActions()

    # Alarmes oncall actives (active ou escalated)
    existing_oncall_active = next(
        (
            a for a in existing_alarms
            if a.is_oncall_alarm and a.status in ("active", "escalated")
        ),
        None,
    )

    online_users = [u for u in users if u.is_online]
    # Marker clear dès qu'au moins un user est online (fin d'épisode INV-053).
    # Aucun no-op gratuit : on n'émet le flag clear que si le marker est posé.
    should_clear_marker = email_already_sent and bool(online_users)

    # INV-051 : oncall online → résoudre l'alarme oncall si elle existe
    if oncall_user.is_online:
        if existing_oncall_active is not None:
            return OncallActions(
                resolutions=(OncallAlarmResolution(alarm_id=existing_oncall_active.id),),
                email_marker_clear=should_clear_marker,
            )
        return OncallActions(email_marker_clear=should_clear_marker)

    # Oncall offline — vérifier depuis combien de temps
    if oncall_user.last_heartbeat is None:
        # Cas dégénéré : jamais de heartbeat, on ne sait pas
        return OncallActions()

    offline_duration_minutes = (now - oncall_user.last_heartbeat).total_seconds() / 60.0
    if offline_duration_minutes < oncall_offline_delay_minutes:
        return OncallActions()

    # INV-053 : personne online → email direction technique (pas d'alarme).
    # Issue #116 : AU PLUS UNE FOIS par épisode. Si déjà envoyé, on n'envoie plus.
    if not online_users:
        if email_already_sent:
            return OncallActions()
        return OncallActions(
            emails=(DirectionTechniqueEmail(
                oncall_user_name=oncall_user.name,
                offline_duration_minutes=offline_duration_minutes,
            ),),
            email_marker_set=True,
        )

    # INV-054 : alarme oncall déjà existante → skip
    if existing_oncall_active is not None:
        return OncallActions(email_marker_clear=should_clear_marker)

    # INV-001 : une autre alarme active (non-oncall) → skip (contrainte unicité)
    any_active = any(
        a.status in ("active", "escalated") for a in existing_alarms
    )
    if any_active:
        return OncallActions(email_marker_clear=should_clear_marker)

    # INV-052 : trouver le prochain user online dans la chaîne (pas le #1)
    assigned_user_id = _find_next_online_in_chain(chain, users_by_id, oncall_user.id)
    if assigned_user_id is None:
        # Fallback : premier user online hors chaîne
        assigned_user_id = online_users[0].id if online_users else None

    if assigned_user_id is None:
        return OncallActions(email_marker_clear=should_clear_marker)

    return OncallActions(
        creations=(OncallAlarmCreation(
            oncall_user_name=oncall_user.name,
            offline_duration_minutes=offline_duration_minutes,
            assigned_user_id=assigned_user_id,
        ),),
        email_marker_clear=should_clear_marker,
    )


def _find_next_online_in_chain(
    chain: list[EscalationChainEntry],
    users_by_id: dict[int, UserSnapshot],
    exclude_user_id: int,
) -> int | None:
    """Cherche dans la chaîne (dans l'ordre) le premier user online autre que exclude."""
    for entry in chain:
        if entry.user_id == exclude_user_id:
            continue
        u = users_by_id.get(entry.user_id)
        if u is not None and u.is_online:
            return entry.user_id
    return None
