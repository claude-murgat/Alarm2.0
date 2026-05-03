"""Unit tests (tier 1) pour backend/app/logic/escalation.py.

Invariants couverts (voir tests/INVARIANTS.md) :
- INV-010 : escalade seulement si delay dépassé
- INV-011 : délai UNIFORME 15 min pour chaque user (plus de split astreinte/veille)
- INV-012 : cumulative (ancien user reste notifié, mais géré par l'appelant)
- INV-013 : wrap-around (après le dernier, repart au premier)
- INV-014 : ignore is_online pour choisir le next_user (FCM réveille)
- INV-015 : pas d'escalade si status=acknowledged
- INV-015b : FCM wake-up envoyé au user courant SI offline, avant l'escalade

Tests purs : aucune DB, aucun HTTP, aucun sleep. <100ms total.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.escalation import evaluate_escalation
from backend.app.logic.models import (
    AlarmSnapshot,
    EscalationActions,
    EscalationChainEntry,
    EscalationDecision,
    FCMWakeUp,
)


NOW = datetime(2026, 4, 17, 12, 0, 0)
DELAY = 15.0  # Delay uniforme INV-011


def _chain(*user_ids: int) -> list[EscalationChainEntry]:
    """Helper : construit une chaine [pos1=user_ids[0], pos2=user_ids[1], ...]."""
    return [EscalationChainEntry(position=i + 1, user_id=uid) for i, uid in enumerate(user_ids)]


def _alarm(
    alarm_id: int = 1,
    status: str = "active",
    assigned_user_id: int = 1,
    created_at: datetime = NOW - timedelta(minutes=16),  # Default : eligible pour escalade
    escalation_count: int = 0,
    is_oncall_alarm: bool = False,
) -> AlarmSnapshot:
    return AlarmSnapshot(
        id=alarm_id,
        status=status,
        created_at=created_at,
        suspended_until=None,
        assigned_user_id=assigned_user_id,
        escalation_count=escalation_count,
        is_oncall_alarm=is_oncall_alarm,
    )


pytestmark = pytest.mark.unit


class TestNoEligibleAlarms:
    """Cas ou aucune escalade ne doit se produire."""

    def test_empty_alarms_returns_empty_actions(self):
        """Attrape : crash sur liste vide."""
        result = evaluate_escalation([], _chain(1, 2, 3), {1: True, 2: True, 3: True}, DELAY, NOW)
        assert result == EscalationActions(escalations=(), wake_ups=())

    def test_empty_chain_returns_empty_actions(self):
        """Si la chaine est vide, impossible de trouver un next_user.
        Ne doit ni crasher ni escalader."""
        alarms = [_alarm()]
        result = evaluate_escalation(alarms, [], {1: True}, DELAY, NOW)
        assert result == EscalationActions(escalations=(), wake_ups=())

    def test_elapsed_below_delay_no_escalation(self):
        """INV-010 : à delay-1 min, pas d'escalade."""
        alarm = _alarm(created_at=NOW - timedelta(minutes=14))  # elapsed=14 < 15
        result = evaluate_escalation([alarm], _chain(1, 2, 3), {1: True}, DELAY, NOW)
        assert result.escalations == ()

    def test_elapsed_exactly_delay_escalates(self):
        """INV-010 boundary : à exactement delay min, escalade (>= strict).
        Attrape : regression si quelqu'un change >= en >."""
        alarm = _alarm(created_at=NOW - timedelta(minutes=15))  # elapsed=15 == 15
        result = evaluate_escalation([alarm], _chain(1, 2, 3), {1: True}, DELAY, NOW)
        assert result.escalations == (EscalationDecision(alarm_id=1, from_user_id=1, to_user_id=2),)

    def test_acknowledged_alarm_not_escalated(self):
        """INV-015 : une alarme acknowledged ne doit jamais etre escaladee,
        meme si elapsed > delay."""
        alarm = _alarm(status="acknowledged", created_at=NOW - timedelta(hours=1))
        result = evaluate_escalation([alarm], _chain(1, 2, 3), {1: True}, DELAY, NOW)
        assert result == EscalationActions(escalations=(), wake_ups=())

    def test_resolved_alarm_not_escalated(self):
        """Une alarme resolved ne doit pas etre escaladee."""
        alarm = _alarm(status="resolved", created_at=NOW - timedelta(hours=1))
        result = evaluate_escalation([alarm], _chain(1, 2, 3), {1: True}, DELAY, NOW)
        assert result.escalations == ()


class TestUniformDelay:
    """INV-011 : delai UNIFORME pour chaque position (plus de split astreinte/veille)."""

    @pytest.mark.parametrize("position_of_current", [1, 2, 3])
    def test_same_delay_regardless_of_position(self, position_of_current: int):
        """Le delai est identique que l'alarme soit sur pos 1, 2 ou 3.
        Attrape : regression si on reintroduit un split astreinte/veille."""
        chain = _chain(1, 2, 3)
        current_user = chain[position_of_current - 1].user_id
        # A 16 min : doit escalader depuis n'importe quelle position
        alarm = _alarm(assigned_user_id=current_user, created_at=NOW - timedelta(minutes=16))
        result = evaluate_escalation([alarm], chain, {1: True, 2: True, 3: True}, DELAY, NOW)
        assert len(result.escalations) == 1

    @pytest.mark.parametrize("position_of_current", [1, 2, 3])
    def test_same_delay_no_escalation_below_threshold(self, position_of_current: int):
        """A 14 min, pas d'escalade QUEL QUE SOIT la position.
        Attrape : si le split 15/2 n'est pas completement retire, veille escaladerait a 14 min."""
        chain = _chain(1, 2, 3)
        current_user = chain[position_of_current - 1].user_id
        alarm = _alarm(assigned_user_id=current_user, created_at=NOW - timedelta(minutes=14))
        result = evaluate_escalation([alarm], chain, {1: True, 2: True, 3: True}, DELAY, NOW)
        assert result.escalations == ()


class TestChainNavigation:
    """INV-013 : wrap-around. INV-014 : ignore is_online pour choisir le next."""

    def test_escalates_from_pos1_to_pos2(self):
        alarm = _alarm(assigned_user_id=1)
        result = evaluate_escalation([alarm], _chain(1, 2, 3), {1: True, 2: True, 3: True}, DELAY, NOW)
        assert result.escalations == (EscalationDecision(alarm_id=1, from_user_id=1, to_user_id=2),)

    def test_escalates_from_pos2_to_pos3(self):
        alarm = _alarm(assigned_user_id=2)
        result = evaluate_escalation([alarm], _chain(1, 2, 3), {1: True, 2: True, 3: True}, DELAY, NOW)
        assert result.escalations == (EscalationDecision(alarm_id=1, from_user_id=2, to_user_id=3),)

    def test_wrap_around_from_last_to_first(self):
        """INV-013 : apres le dernier, repart au premier."""
        alarm = _alarm(assigned_user_id=3)
        result = evaluate_escalation([alarm], _chain(1, 2, 3), {1: True, 2: True, 3: True}, DELAY, NOW)
        assert result.escalations == (EscalationDecision(alarm_id=1, from_user_id=3, to_user_id=1),)

    def test_does_not_escalate_to_same_user(self):
        """Si l'user courant est aussi le next (chaine a 1 seul user), pas d'escalade."""
        alarm = _alarm(assigned_user_id=1)
        result = evaluate_escalation([alarm], _chain(1), {1: True}, DELAY, NOW)
        assert result.escalations == ()

    def test_ignores_online_status_for_next_user_selection(self):
        """INV-014 : l'escalade choisit le suivant dans l'ORDRE, meme s'il est offline.
        Attrape : regression si quelqu'un reintroduit un filtre is_online."""
        alarm = _alarm(assigned_user_id=1)
        # user 2 offline, user 3 online : on doit quand meme aller sur user 2
        result = evaluate_escalation(
            [alarm], _chain(1, 2, 3), {1: True, 2: False, 3: True}, DELAY, NOW
        )
        assert result.escalations == (EscalationDecision(alarm_id=1, from_user_id=1, to_user_id=2),)

    def test_current_user_not_in_chain_goes_to_first(self):
        """Cas degenere : alarme assignee a un user qui n'est pas dans la chaine.
        Doit aller au premier de la chaine (fallback)."""
        alarm = _alarm(assigned_user_id=99)  # 99 pas dans la chaine
        result = evaluate_escalation(
            [alarm], _chain(1, 2, 3), {1: True, 99: True}, DELAY, NOW
        )
        # to_user = 1 (premier de la chaine)
        assert result.escalations == (EscalationDecision(alarm_id=1, from_user_id=99, to_user_id=1),)


class TestFcmWakeUpInv015b:
    """INV-015b : FCM wake-up envoye au user courant SI offline, avant l'escalade.
    Si user online : pas de FCM dedie (sa sonnerie est deja active)."""

    def test_offline_current_user_gets_wakeup_fcm(self):
        """User courant offline au moment de l'escalade -> FCM wake-up pour lui."""
        alarm = _alarm(assigned_user_id=1)
        result = evaluate_escalation(
            [alarm], _chain(1, 2, 3), {1: False, 2: True, 3: True}, DELAY, NOW
        )
        assert result.wake_ups == (FCMWakeUp(alarm_id=1, user_id=1),)
        assert result.escalations == (EscalationDecision(alarm_id=1, from_user_id=1, to_user_id=2),)

    def test_online_current_user_no_wakeup_fcm(self):
        """User courant online -> pas de FCM dedie (sa sonnerie marche).
        Attrape : regression si on envoie FCM a tout le monde systematiquement."""
        alarm = _alarm(assigned_user_id=1)
        result = evaluate_escalation(
            [alarm], _chain(1, 2, 3), {1: True, 2: True, 3: True}, DELAY, NOW
        )
        assert result.wake_ups == ()

    def test_no_wakeup_if_no_escalation(self):
        """Pas d'escalade = pas de wake-up (le wake-up est lie a l'escalade imminente).
        A 14 min (avant delay), meme si user offline, pas de wake-up."""
        alarm = _alarm(
            assigned_user_id=1,
            created_at=NOW - timedelta(minutes=14),  # Pas encore eligible
        )
        result = evaluate_escalation(
            [alarm], _chain(1, 2, 3), {1: False}, DELAY, NOW
        )
        assert result.wake_ups == ()
        assert result.escalations == ()

    def test_no_wakeup_if_acknowledged(self):
        """Pas d'escalade (acknowledged) = pas de wake-up."""
        alarm = _alarm(
            assigned_user_id=1,
            status="acknowledged",
            created_at=NOW - timedelta(hours=1),
        )
        result = evaluate_escalation(
            [alarm], _chain(1, 2, 3), {1: False}, DELAY, NOW
        )
        assert result == EscalationActions(escalations=(), wake_ups=())


class TestMultipleAlarms:
    """Plusieurs alarmes actives en meme temps (ne devrait pas arriver vu INV-001,
    mais la fonction doit etre robuste)."""

    def test_each_alarm_evaluated_independently(self):
        """Alarmes en divers etats, seules celles eligibles sont escaladees."""
        alarms = [
            _alarm(alarm_id=1, assigned_user_id=1, created_at=NOW - timedelta(minutes=16)),
            _alarm(alarm_id=2, assigned_user_id=2, status="acknowledged",
                   created_at=NOW - timedelta(hours=1)),
            _alarm(alarm_id=3, assigned_user_id=3, created_at=NOW - timedelta(minutes=14)),
        ]
        result = evaluate_escalation(
            alarms, _chain(1, 2, 3), {1: True, 2: True, 3: True}, DELAY, NOW
        )
        # Seule alarm 1 a elapsed >= delay ET n'est pas acknowledged
        assert result.escalations == (EscalationDecision(alarm_id=1, from_user_id=1, to_user_id=2),)


class TestPurity:
    """Garantie : fonction pure, determinisme."""

    def test_no_mutation_of_inputs(self):
        """Attrape : mutation cachee des dataclasses passees en parametre."""
        alarm = _alarm()
        chain = _chain(1, 2, 3)
        users = {1: True, 2: True, 3: True}
        evaluate_escalation([alarm], chain, users, DELAY, NOW)
        assert alarm.assigned_user_id == 1
        assert alarm.escalation_count == 0
        assert chain[0].user_id == 1

    def test_deterministic_same_input_same_output(self):
        """Attrape : non-determinisme cache (random, datetime.utcnow interne)."""
        alarm = _alarm()
        chain = _chain(1, 2, 3)
        users = {1: True, 2: True, 3: True}
        r1 = evaluate_escalation([alarm], chain, users, DELAY, NOW)
        r2 = evaluate_escalation([alarm], chain, users, DELAY, NOW)
        r3 = evaluate_escalation([alarm], chain, users, DELAY, NOW)
        assert r1 == r2 == r3


class TestContinueSemantics:
    """Tue les mutations `continue` -> `break` dans la boucle principale (mutmut
    105, 111, 116) ainsi que le default de `users_online.get()` (mutmut 119).

    Pourquoi : une seule alarme skippee ne doit jamais empecher l'evaluation des
    autres. Le `break` mute interromprait la boucle, donc une alarme acknowledged
    en debut de liste empecherait toute escalade subsequente — silence total
    alors que d'autres alarmes critiques sont en attente.
    """

    def test_skip_acked_alarm_continues_with_next(self):
        """Mutmut 105 : if status not in (active, escalated): continue (vs break)."""
        alarms = [
            _alarm(alarm_id=1, status="acknowledged",
                   created_at=NOW - timedelta(hours=1)),  # acked → skip
            _alarm(alarm_id=2, assigned_user_id=1),       # eligible
        ]
        result = evaluate_escalation(alarms, _chain(1, 2),
                                     {1: True, 2: True}, DELAY, NOW)
        assert len(result.escalations) == 1
        assert result.escalations[0].alarm_id == 2

    def test_skip_too_fresh_alarm_continues_with_next(self):
        """Mutmut 111 : if elapsed < delay: continue (vs break)."""
        alarms = [
            _alarm(alarm_id=1, created_at=NOW - timedelta(minutes=5)),  # 5 < 15 → skip
            _alarm(alarm_id=2, assigned_user_id=1),                     # eligible (default 16 min)
        ]
        result = evaluate_escalation(alarms, _chain(1, 2),
                                     {1: True, 2: True}, DELAY, NOW)
        assert len(result.escalations) == 1
        assert result.escalations[0].alarm_id == 2

    def test_skip_no_next_user_continues_with_next(self):
        """Mutmut 116 : if next_user_id is None or == self: continue (vs break).
        Cas : chaine a 1 seul user → pas de next pour cet user, mais doit traiter
        les autres alarmes assignees a un user pas dans la chaine."""
        chain = _chain(1)  # un seul user
        alarms = [
            _alarm(alarm_id=1, assigned_user_id=1),  # next = self → no escalation
            _alarm(alarm_id=2, assigned_user_id=2),  # next = user 1 (premier de la chaine)
        ]
        result = evaluate_escalation(alarms, chain,
                                     {1: True, 2: True}, DELAY, NOW)
        assert len(result.escalations) == 1
        assert result.escalations[0].alarm_id == 2

    def test_user_absent_from_online_dict_treated_as_online(self):
        """Mutmut 119 : users_online.get(uid, True) — un user absent du dict
        est traite comme ONLINE par defaut (pas de wake-up FCM).

        Ce defaut est important : il rend la fonction robuste a un users_online
        partiel (ex: dict construit a partir de heartbeats recents seulement).
        """
        chain = _chain(1, 2)
        alarm = _alarm(assigned_user_id=1)
        # User 1 absent du dict → default True → traite comme online
        result = evaluate_escalation([alarm], chain, {}, DELAY, NOW)
        assert len(result.escalations) == 1
        assert len(result.wake_ups) == 0  # pas de wake-up car traite comme online
