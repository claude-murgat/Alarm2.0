"""Unit tests (tier 1) — backend/app/logic/dry_contact.py (INV-194, regles d'INV-122/123/157).

Etats des passerelles vivantes d'un site -> (alarme voulue, a resoudre, desaccord). Une passerelle
`no_sensor` (vivante, sans Arduino lisible) n'est ni ouverte ni fermee : hors de l'ensemble des
passerelles vivantes avec capteur.
"""
import pytest

from backend.app.logic.dry_contact import aggregate_dry_contact

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("states, expected", [
    # Sans no_sensor : regles d'INV-122/123 inchangees
    (["open"], (True, False, False)),
    (["closed"], (False, True, False)),
    (["closed", "closed"], (False, True, False)),
    (["open", "closed"], (True, False, True)),
    # no_sensor ne cree pas, ne bloque pas, ne resout pas, ne cree pas de desaccord
    (["open", "no_sensor"], (True, False, False)),
    (["closed", "no_sensor"], (False, True, False)),
    (["open", "closed", "no_sensor"], (True, False, True)),
    # Aucune passerelle avec capteur : ni creation ni resolution (fail-to-alarm, INV-120)
    (["no_sensor"], (False, False, False)),
    (["no_sensor", "no_sensor"], (False, False, False)),
    ([], (False, False, False)),
])
def test_no_sensor_is_outside_the_alive_sensor_set(states, expected):
    assert aggregate_dry_contact(states) == expected
