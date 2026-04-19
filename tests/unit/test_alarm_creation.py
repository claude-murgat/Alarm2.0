"""Unit tests (tier 1) pour backend/app/logic/alarm_creation.py.

Invariants couverts :
- INV-080 : chaîne d'escalade vide + alarme → email direction technique + fallback assignation
- Logique de fallback : si pas assigned_user_id explicite, prendre le 1er de la chaîne

Tests purs : aucune DB, <100ms total.
"""
import pytest

from backend.app.logic.alarm_creation import evaluate_alarm_creation_plan
from backend.app.logic.models import (
    AlarmCreationPlan,
    EscalationChainEntry,
    UserSnapshot,
)


def _user(user_id: int, name: str = "u") -> UserSnapshot:
    # is_online et last_heartbeat ne sont pas utilises par evaluate_alarm_creation_plan
    return UserSnapshot(id=user_id, name=name, is_online=True, last_heartbeat=None)


def _chain(*user_ids: int) -> list[EscalationChainEntry]:
    return [EscalationChainEntry(position=i + 1, user_id=uid) for i, uid in enumerate(user_ids)]


pytestmark = pytest.mark.unit


class TestChainNotEmpty:
    """Chaîne non vide : assigned au premier de la chaîne (sauf override explicite)."""

    def test_assigns_to_first_of_chain_by_default(self):
        """Sans assigned_user_id explicite → 1er de la chaine."""
        plan = evaluate_alarm_creation_plan(
            requested_assigned_user_id=None,
            chain=_chain(1, 2, 3),
            users=[_user(1), _user(2), _user(3)],
        )
        assert plan.assigned_user_id == 1
        assert plan.needs_direction_technique_email is False
        assert plan.email_reason is None

    def test_explicit_assigned_user_id_overrides_chain(self):
        """assigned_user_id explicite → prend celui-ci, pas le 1er de la chaine."""
        plan = evaluate_alarm_creation_plan(
            requested_assigned_user_id=3,  # explicitement user 3
            chain=_chain(1, 2, 3),
            users=[_user(1), _user(2), _user(3)],
        )
        assert plan.assigned_user_id == 3
        assert plan.needs_direction_technique_email is False


class TestInv080ChainEmpty:
    """INV-080 : chaîne vide → email + fallback."""

    def test_empty_chain_with_users_falls_back_to_first_user(self):
        """Chaine vide, users existent → fallback au 1er user + email."""
        plan = evaluate_alarm_creation_plan(
            requested_assigned_user_id=None,
            chain=[],
            users=[_user(5, "alice"), _user(7, "bob")],
        )
        assert plan.assigned_user_id == 5
        assert plan.needs_direction_technique_email is True
        assert plan.email_reason == "chain_empty"

    def test_empty_chain_and_no_users_no_assignment(self):
        """Chaine vide ET pas d'users → assigned=None, email quand meme.
        Cas extreme (ne devrait pas arriver vu le seed admin/user1/user2)."""
        plan = evaluate_alarm_creation_plan(
            requested_assigned_user_id=None, chain=[], users=[],
        )
        assert plan.assigned_user_id is None
        assert plan.needs_direction_technique_email is True
        assert plan.email_reason == "chain_empty"

    def test_empty_chain_with_explicit_assigned_user_id_still_sends_email(self):
        """Meme si assigned_user_id est explicite, chaine vide doit alerter la direction.
        Attrape : si on optimise en sautant l'email pour les assignations explicites,
        on rate le signal business ('operateur non-autorise a envoyer')."""
        plan = evaluate_alarm_creation_plan(
            requested_assigned_user_id=7,
            chain=[],
            users=[_user(5), _user(7)],
        )
        assert plan.assigned_user_id == 7
        assert plan.needs_direction_technique_email is True


class TestPurity:

    def test_no_mutation(self):
        chain = _chain(1, 2)
        users = [_user(1), _user(2)]
        evaluate_alarm_creation_plan(None, chain, users)
        assert chain[0].user_id == 1
        assert users[0].id == 1

    def test_deterministic(self):
        chain = _chain(1, 2)
        users = [_user(1), _user(2)]
        r1 = evaluate_alarm_creation_plan(None, chain, users)
        r2 = evaluate_alarm_creation_plan(None, chain, users)
        assert r1 == r2
