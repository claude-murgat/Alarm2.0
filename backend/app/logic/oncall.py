"""Logique pure : email "personne en ligne" (INV-053).

Invariant couvert :
- INV-053 : pos 1 offline >= delay + tous offline → email direction technique
  (au plus une fois par épisode, marker effacé dès qu'un user repasse online).

Décision 2026-05-26 (cf tests/INVARIANTS.md §5 encadré "Changement de stratégie") :
les invariants INV-050 (création d'alarme oncall_offline), INV-051 (auto-résolution),
INV-052 (assignation au suivant) et INV-054 (anti-doublon) sont DÉPRÉCIÉS et leur
code a été retiré ici. Le tracking statistique des transitions online/offline est
désormais porté par INV-056 (table `connectivity_events`).

L'appelant applique les Actions retournées en DB + SMTP.
"""
from datetime import datetime

from .models import (
    AlarmSnapshot,
    DirectionTechniqueEmail,
    EscalationChainEntry,
    OncallActions,
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
    """Retourne les Actions à appliquer pour ce tick.

    Logique INV-053 :
    - Si pos 1 offline depuis >= delay ET aucun user online ET marker pas déjà posé
      → email + email_marker_set.
    - Si au moins un user est online et que le marker est posé → email_marker_clear
      (fin d'épisode, autorisation à émettre un nouvel email au prochain épisode).
    - Sinon → rien.

    Le paramètre `existing_alarms` n'est plus consulté pour la création d'alarme
    (INV-050 déprécié) mais reste dans la signature pour stabilité d'API et pour
    permettre une future réintroduction si nécessaire.

    `email_already_sent` (issue #116) : True si l'email "personne online" a déjà
    été envoyé pour l'épisode courant (marker persistant côté caller). Dans ce cas
    on n'envoie PAS un nouvel email tant qu'au moins un user n'est pas revenu
    online (qui clear le marker via `email_marker_clear`).
    """
    del existing_alarms  # paramètre conservé pour stabilité d'API ; plus utilisé

    if not chain:
        return OncallActions()

    oncall_entry = chain[0]  # position 1
    users_by_id = {u.id: u for u in users}
    oncall_user = users_by_id.get(oncall_entry.user_id)
    if oncall_user is None:
        # Cas dégénéré : chain pointe vers un user supprimé
        return OncallActions()

    online_users = [u for u in users if u.is_online]
    # Marker clear dès qu'au moins un user est online (fin d'épisode INV-053).
    # Aucun no-op gratuit : on n'émet le flag clear que si le marker est posé.
    should_clear_marker = email_already_sent and bool(online_users)

    # Oncall online : juste éventuellement clear le marker.
    if oncall_user.is_online:
        return OncallActions(email_marker_clear=should_clear_marker)

    # Oncall offline — vérifier depuis combien de temps
    if oncall_user.last_heartbeat is None:
        # Cas dégénéré : jamais de heartbeat, on ne sait pas
        return OncallActions(email_marker_clear=should_clear_marker)

    offline_duration_minutes = (now - oncall_user.last_heartbeat).total_seconds() / 60.0
    if offline_duration_minutes < oncall_offline_delay_minutes:
        return OncallActions(email_marker_clear=should_clear_marker)

    # INV-053 : pas d'user online → email direction technique (au plus une fois par épisode).
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

    # Au moins un user online : pas de raison d'émettre l'email. Si marker posé, le clear.
    return OncallActions(email_marker_clear=should_clear_marker)
