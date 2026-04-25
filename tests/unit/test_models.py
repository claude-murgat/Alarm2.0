"""Unit tests (tier 1) pour backend/app/logic/models.py.

Vérifie l'invariant de design : tous les snapshots et Actions sont frozen.

Pourquoi : la logique pure repose sur l'immuabilité de ses entrées/sorties.
Une mutation accidentelle d'un snapshot dans un appelant introduirait des
bugs subtils invisibles à la lecture. Mutmut detectait que `frozen=True`
pouvait être passé à `False` sans que ça casse aucun test (17 mutants
survivants au run du 2026-04-25) — ce test pulvérise les 17 d'un coup.

Tests purs : aucune DB, <100ms total.
"""
import dataclasses
from datetime import datetime

import pytest

from backend.app.logic.models import (
    AckAuthorization,
    AckReactivation,
    AlarmCreationPlan,
    AlarmSnapshot,
    CallEnqueue,
    DirectionTechniqueEmail,
    EscalationActions,
    EscalationChainEntry,
    EscalationDecision,
    FCMWakeUp,
    NotificationSnapshot,
    OncallActions,
    OncallAlarmCreation,
    OncallAlarmResolution,
    SmsCallActions,
    SmsEnqueue,
    UserSnapshot,
)


pytestmark = pytest.mark.unit


_NOW = datetime(2026, 4, 25, 12, 0, 0)


# Liste exhaustive : (classe, factory zero-arg renvoyant une instance valide).
# Chaque ajout de dataclass frozen dans models.py doit être ajouté ici.
FROZEN_DATACLASSES = [
    (AlarmSnapshot, lambda: AlarmSnapshot(
        id=1, status="active", created_at=_NOW,
        suspended_until=None, assigned_user_id=None,
        escalation_count=0, is_oncall_alarm=False,
    )),
    (AckReactivation, lambda: AckReactivation(alarm_id=1)),
    (EscalationChainEntry, lambda: EscalationChainEntry(position=1, user_id=1)),
    (EscalationDecision, lambda: EscalationDecision(
        alarm_id=1, from_user_id=1, to_user_id=2,
    )),
    (FCMWakeUp, lambda: FCMWakeUp(alarm_id=1, user_id=1)),
    (EscalationActions, lambda: EscalationActions(escalations=(), wake_ups=())),
    (NotificationSnapshot, lambda: NotificationSnapshot(
        id=1, alarm_id=1, user_id=1, notified_at=None,
        sms_sent=False, call_sent=False,
    )),
    (SmsEnqueue, lambda: SmsEnqueue(notification_id=1, alarm_id=1, user_id=1)),
    (CallEnqueue, lambda: CallEnqueue(notification_id=1, alarm_id=1, user_id=1)),
    (SmsCallActions, lambda: SmsCallActions(sms_enqueues=(), call_enqueues=())),
    (UserSnapshot, lambda: UserSnapshot(
        id=1, name="x", is_online=True, last_heartbeat=None,
    )),
    (OncallAlarmResolution, lambda: OncallAlarmResolution(alarm_id=1)),
    (OncallAlarmCreation, lambda: OncallAlarmCreation(
        oncall_user_name="x", offline_duration_minutes=15.0, assigned_user_id=2,
    )),
    (DirectionTechniqueEmail, lambda: DirectionTechniqueEmail(
        oncall_user_name="x", offline_duration_minutes=15.0,
    )),
    (OncallActions, lambda: OncallActions(
        resolutions=(), creations=(), emails=(),
    )),
    (AlarmCreationPlan, lambda: AlarmCreationPlan(
        assigned_user_id=1, needs_direction_technique_email=False, email_reason=None,
    )),
    (AckAuthorization, lambda: AckAuthorization(allowed=True, reason=None)),
]


@pytest.mark.parametrize(
    "cls,factory", FROZEN_DATACLASSES, ids=[cls.__name__ for cls, _ in FROZEN_DATACLASSES]
)
def test_dataclass_is_frozen(cls, factory):
    """Toutes les dataclasses de logic/models.py doivent rester immutables.

    Sans cette garantie, un appelant pourrait muter un snapshot après l'avoir
    passé à une fonction pure, cassant l'isolement de la logique. Mutmut le
    detecte : sans test, frozen=True peut etre mis à frozen=False sans rien casser.
    """
    instance = factory()
    fields = dataclasses.fields(cls)
    assert fields, f"{cls.__name__} doit avoir au moins un champ"

    first_field_name = fields[0].name
    current_value = getattr(instance, first_field_name)

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, first_field_name, current_value)


def test_frozen_dataclasses_list_is_exhaustive():
    """Garde-fou : si une nouvelle dataclass est ajoutee dans models.py,
    elle doit etre ajoutee a FROZEN_DATACLASSES (sinon test_dataclass_is_frozen
    ne la couvrira pas). On compte les @dataclass declarés dans le module.
    """
    import backend.app.logic.models as models_module
    declared = {
        name for name, obj in vars(models_module).items()
        if dataclasses.is_dataclass(obj) and not name.startswith("_")
    }
    covered = {cls.__name__ for cls, _ in FROZEN_DATACLASSES}
    missing = declared - covered
    assert not missing, (
        f"Dataclasses declarees dans models.py mais absentes de "
        f"FROZEN_DATACLASSES : {sorted(missing)}. Ajoute-les a la liste."
    )
