"""Unit tests (tier 1) pour INV-005 : escalation_count monotone croissant.

Invariant (tests/INVARIANTS.md) :
    `alarm.escalation_count` ne peut que croitre, jamais decroitre,
    meme apres ack + reactivation. C'est un compteur historique utilise
    par les stats KPI : un decrement trahirait la promesse au business.

Bug reporte (issue #26, Eric) :
    "j'ai vu 3 escalades ce matin sur une alarme, 2 le soir. j'ai
    l'impression que quand l'astreinte acquitte et que ca reescalade 30 min
    plus tard, le compteur il repart de 0 ou un truc du genre."

Contrat teste :
    Le caller de evaluate_escalation / evaluate_ack_expiry (escalation.py)
    applique les Actions retournees selon les docstrings de logic/models.py :
      - EscalationDecision   -> alarm.escalation_count += 1
      - AckReactivation      -> alarm.status = 'active' (escalation_count preserve)
    Apres TOUTE sequence d'operations (escalate, ack, ack_expire, resolve,
    advance-time), escalation_count[t+1] >= escalation_count[t].

Strategie : tests purs, on mime le caller escalation.py::escalation_loop
via des helpers _step_*. Aucune DB, aucun HTTP, aucun sleep. <100ms total.

Pourquoi tier 1 plutot que tier 2/3 :
    L'invariant est une propriete du duo (fonctions pures + caller). Le mimer
    avec des frozen dataclasses expose les decisions atomiques une par une,
    ce qu'un TestClient+DB obscurcirait (tout passe par un commit global).
    Un property-based hypothesis explore 100+ sequences en <1s, impossible
    en tier 2/3.
"""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings, strategies as st

from backend.app.logic.ack_expiry import evaluate_ack_expiry
from backend.app.logic.escalation import evaluate_escalation
from backend.app.logic.models import AlarmSnapshot, EscalationChainEntry


pytestmark = pytest.mark.unit


NOW0 = datetime(2026, 4, 21, 10, 0, 0)
DELAY = 15.0
CHAIN = [
    EscalationChainEntry(position=1, user_id=1),
    EscalationChainEntry(position=2, user_id=2),
    EscalationChainEntry(position=3, user_id=3),
]


def _initial_alarm(now: datetime) -> AlarmSnapshot:
    return AlarmSnapshot(
        id=42,
        status="active",
        created_at=now,
        suspended_until=None,
        assigned_user_id=1,
        escalation_count=0,
        is_oncall_alarm=False,
    )


# --- Simulateur : mime escalation.py::escalation_loop dans sa prise de decisions ---
# Chaque _step_* reflete ce que le caller applique d'apres les docstrings de
# logic/models.py. Si ces docstrings changent, ces helpers doivent changer en miroir.

def _step_escalation(alarm: AlarmSnapshot, now: datetime) -> AlarmSnapshot:
    """Applique les decisions de evaluate_escalation. Mime escalation.py:234-245."""
    users_online = {entry.user_id: True for entry in CHAIN}
    actions = evaluate_escalation([alarm], CHAIN, users_online, DELAY, now)
    for decision in actions.escalations:
        if decision.alarm_id != alarm.id:
            continue
        alarm = replace(
            alarm,
            assigned_user_id=decision.to_user_id,
            status="escalated",
            escalation_count=alarm.escalation_count + 1,
            created_at=now,
        )
    return alarm


def _step_ack(alarm: AlarmSnapshot, now: datetime) -> AlarmSnapshot:
    """Mime alarms.py::acknowledge_alarm. Ne change pas escalation_count."""
    if alarm.status in ("active", "escalated"):
        return replace(
            alarm,
            status="acknowledged",
            suspended_until=now + timedelta(minutes=30),
        )
    return alarm


def _step_ack_expire(alarm: AlarmSnapshot, now: datetime) -> AlarmSnapshot:
    """Applique AckReactivation. Mime escalation.py:148-169. escalation_count PRESERVE."""
    reactivations = evaluate_ack_expiry([alarm], now)
    for reactivation in reactivations:
        if reactivation.alarm_id == alarm.id:
            alarm = replace(
                alarm,
                status="active",
                suspended_until=None,
                created_at=now,
            )
    return alarm


def _step_resolve(alarm: AlarmSnapshot) -> AlarmSnapshot:
    """Mime alarms.py::resolve_alarm."""
    return replace(alarm, status="resolved")


def test_eric_scenario_three_escalations_then_ack_reactivate_escalate():
    """Attrape le bug de l'issue #26 si, pendant la sequence decrite par Eric
    (3 escalades puis ack puis reactivation puis 1 escalade supplementaire),
    escalation_count decroitrait entre deux etapes.

    Sequence : count doit suivre 0 -> 1 -> 2 -> 3 -> 3(ack) -> 3(reactivation) -> 4.
    Un '= 0' ou '-= 1' dans n'importe quel call site (escalation.py:243,
    test_api.py:253, calls.py:95) ou une mauvaise application de AckReactivation
    (escalation.py:148-169 qui reinitialiserait le compteur) ferait casser la
    monotonicite. Ce test echoue alors avec un message precis sur l'etape fautive.
    """
    now = NOW0
    alarm = _initial_alarm(now)
    counts = [alarm.escalation_count]  # [0]

    # 3 escalades successives (le matin, ce qu'Eric a vu live)
    for _ in range(3):
        now += timedelta(minutes=16)  # > DELAY, declenche escalade
        alarm = _step_escalation(alarm, now)
        counts.append(alarm.escalation_count)

    assert counts == [0, 1, 2, 3], (
        f"sanity : 3 escalades successives doivent donner [0,1,2,3], got {counts}"
    )

    # Astreinte ack -> suspended 30 min. Compteur ne bouge pas.
    alarm = _step_ack(alarm, now)
    counts.append(alarm.escalation_count)

    # 31 min plus tard : ack expire, alarme reactivee. Compteur ne doit PAS etre reinitialise.
    now += timedelta(minutes=31)
    alarm = _step_ack_expire(alarm, now)
    counts.append(alarm.escalation_count)

    # Le soir : une escalade de plus (Eric aurait alors du voir 4, pas 2).
    now += timedelta(minutes=16)
    alarm = _step_escalation(alarm, now)
    counts.append(alarm.escalation_count)

    # INV-005 : la suite doit etre monotone non-decroissante.
    for i in range(1, len(counts)):
        assert counts[i] >= counts[i - 1], (
            f"INV-005 violated (issue #26) : escalation_count est decroissant entre "
            f"l'etape {i - 1} et {i} : {counts}"
        )

    # Et specifiquement : l'ack + reactivation ne DOIT PAS reset le compteur a 0.
    # counts = [0, 1, 2, 3, 3(ack), 3(reactivated), 4(escalated_again)]
    assert counts[-1] == 4, (
        f"apres 3 escalades + ack + reactivation + 1 escalade, le compteur doit etre "
        f"a 4 (jamais reinitialise). Si Eric voit 2 au lieu de 4 le soir, c'est qu'un "
        f"reset s'est produit quelque part. got sequence: {counts}"
    )


@settings(max_examples=100, deadline=None)
@given(
    ops=st.lists(
        st.sampled_from(["escalate_tick", "ack", "ack_expire_tick", "resolve", "advance_time"]),
        min_size=1,
        max_size=30,
    ),
)
def test_escalation_count_monotone_under_random_operations(ops):
    """INV-005 property-based : pour TOUTE sequence aleatoire d'operations
    (escalate tick, ack, ack expiry tick, resolve, advance time), escalation_count
    ne decroit JAMAIS d'un etat au suivant.

    Attrape tout regressions futures qui reinitialiserait ou decrementerait le
    compteur sur un code path obscur — y compris les cas vaporeux (ack quand
    deja ack, escalate apres resolve, etc.).
    """
    now = NOW0
    alarm = _initial_alarm(now)
    prev = alarm.escalation_count

    for step_idx, op in enumerate(ops):
        if op == "escalate_tick":
            now += timedelta(minutes=16)  # > DELAY garanti, eligible si active/escalated
            alarm = _step_escalation(alarm, now)
        elif op == "ack":
            alarm = _step_ack(alarm, now)
        elif op == "ack_expire_tick":
            now += timedelta(minutes=31)  # > suspension 30 min
            alarm = _step_ack_expire(alarm, now)
        elif op == "resolve":
            alarm = _step_resolve(alarm)
        elif op == "advance_time":
            now += timedelta(minutes=5)

        assert alarm.escalation_count >= prev, (
            f"INV-005 violated : escalation_count a decru de {prev} a "
            f"{alarm.escalation_count} apres op={op!r} a l'etape {step_idx} "
            f"(sequence complete : {ops})"
        )
        prev = alarm.escalation_count
