"""Logique pure : contact sec d'un site (INV-122, INV-123, INV-157, INV-194).

Une passerelle `no_sensor` (vivante, sans Arduino lisible) n'est ni ouverte ni fermee : elle est hors
de l'ensemble des passerelles vivantes avec capteur.
"""


def aggregate_dry_contact(alive_states: list[str]) -> tuple[bool, bool, bool]:
    """Etats des passerelles vivantes d'un site -> (alarme voulue, a resoudre, desaccord). OU fail-to-alarm ;
    sans passerelle avec capteur, ni creation ni resolution (comme sans passerelle vivante, INV-120)."""
    sensors = set(alive_states) - {"no_sensor"}
    return "open" in sensors, sensors == {"closed"}, sensors == {"open", "closed"}
