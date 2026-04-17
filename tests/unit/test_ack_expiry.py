"""Unit tests (tier 1) pour backend/app/logic/ack_expiry.py.

Invariant couvert : INV-016 (expiration d'ACK réactive l'alarme).

Tests purs : aucune DB, aucun HTTP, aucun sleep. <100ms total.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.ack_expiry import evaluate_ack_expiry
from backend.app.logic.models import AckReactivation, AlarmSnapshot


NOW = datetime(2026, 4, 17, 12, 0, 0)


def _make_alarm(
    alarm_id: int = 1,
    status: str = "acknowledged",
    suspended_until: datetime | None = NOW - timedelta(minutes=1),
    created_at: datetime = NOW - timedelta(hours=1),
    assigned_user_id: int | None = 1,
    escalation_count: int = 0,
    is_oncall_alarm: bool = False,
) -> AlarmSnapshot:
    """Factory avec defauts 'alarme ack expiree' pour raccourcir les tests."""
    return AlarmSnapshot(
        id=alarm_id,
        status=status,
        created_at=created_at,
        suspended_until=suspended_until,
        assigned_user_id=assigned_user_id,
        escalation_count=escalation_count,
        is_oncall_alarm=is_oncall_alarm,
    )


pytestmark = pytest.mark.unit


class TestAckExpiryBasic:
    """Cas de base — un seul snapshot dans divers etats."""

    def test_empty_list_returns_empty(self):
        """Attrape : bug de crash sur liste vide."""
        assert evaluate_ack_expiry([], NOW) == []

    def test_acknowledged_with_expired_suspension_is_reactivated(self):
        """INV-016 : ack + suspended_until < now -> reactivation.
        C'est le cas canonique."""
        alarm = _make_alarm(alarm_id=42, suspended_until=NOW - timedelta(seconds=1))
        result = evaluate_ack_expiry([alarm], NOW)
        assert result == [AckReactivation(alarm_id=42)]

    def test_acknowledged_with_future_suspension_is_not_reactivated(self):
        """INV-002 : pendant la suspension, l'alarme reste acknowledged."""
        alarm = _make_alarm(suspended_until=NOW + timedelta(minutes=5))
        assert evaluate_ack_expiry([alarm], NOW) == []

    def test_acknowledged_suspension_exactly_now_is_not_reactivated(self):
        """Boundary : suspended_until == now doit NE PAS reactiver (strict <).
        Attrape une regression si quelqu'un change '<' en '<='."""
        alarm = _make_alarm(suspended_until=NOW)
        assert evaluate_ack_expiry([alarm], NOW) == []

    def test_acknowledged_without_suspension_is_ignored(self):
        """Cas degenere : ack sans suspended_until -> ne doit rien faire
        (pas de crash, pas de reactivation)."""
        alarm = _make_alarm(suspended_until=None)
        assert evaluate_ack_expiry([alarm], NOW) == []

    @pytest.mark.parametrize("status", ["active", "escalated", "resolved"])
    def test_non_acknowledged_status_is_ignored(self, status: str):
        """Seules les alarmes status='acknowledged' peuvent etre reactivees.
        Attrape : bug si un refactor change le filtre de status."""
        alarm = _make_alarm(status=status, suspended_until=NOW - timedelta(hours=1))
        assert evaluate_ack_expiry([alarm], NOW) == []


class TestAckExpiryMultiple:
    """Combinaisons de plusieurs alarmes."""

    def test_only_eligible_alarms_are_returned(self):
        """Dans un mix, seules les ack+expired ressortent.
        Attrape : bug de filtre ou d'ordre qui incluerait des non-eligibles."""
        alarms = [
            _make_alarm(alarm_id=1, status="active"),                              # skip
            _make_alarm(alarm_id=2, suspended_until=NOW - timedelta(minutes=1)),   # ok
            _make_alarm(alarm_id=3, suspended_until=NOW + timedelta(minutes=5)),   # skip (future)
            _make_alarm(alarm_id=4, status="resolved",
                        suspended_until=NOW - timedelta(hours=1)),                 # skip
            _make_alarm(alarm_id=5, suspended_until=NOW - timedelta(seconds=1)),   # ok
        ]
        result = evaluate_ack_expiry(alarms, NOW)
        assert result == [AckReactivation(alarm_id=2), AckReactivation(alarm_id=5)]

    def test_preserves_input_order(self):
        """La fonction ne doit pas trier ou reordonner.
        Attrape : si l'appelant se basait sur l'ordre d'entree pour le traitement."""
        alarms = [
            _make_alarm(alarm_id=10, suspended_until=NOW - timedelta(hours=1)),
            _make_alarm(alarm_id=5, suspended_until=NOW - timedelta(hours=2)),
            _make_alarm(alarm_id=7, suspended_until=NOW - timedelta(hours=3)),
        ]
        result = evaluate_ack_expiry(alarms, NOW)
        assert [r.alarm_id for r in result] == [10, 5, 7]


class TestAckExpiryPurity:
    """Garantie : fonction pure, pas de side effect."""

    def test_no_mutation_of_input(self):
        """Attrape : bug si la fonction mute les snapshots (dataclass frozen
        empeche normalement, mais test explicite)."""
        alarm = _make_alarm(suspended_until=NOW - timedelta(minutes=1))
        evaluate_ack_expiry([alarm], NOW)
        # Si un test future enlevait frozen=True, ce test lance une AttributeError
        # a l'execution plutot qu'en silence.
        assert alarm.status == "acknowledged"
        assert alarm.suspended_until == NOW - timedelta(minutes=1)

    def test_deterministic_same_input_same_output(self):
        """Attrape : non-determinisme cache (random, datetime.utcnow interne, etc.)."""
        alarms = [
            _make_alarm(alarm_id=1, suspended_until=NOW - timedelta(minutes=1)),
            _make_alarm(alarm_id=2, suspended_until=NOW + timedelta(minutes=1)),
        ]
        r1 = evaluate_ack_expiry(alarms, NOW)
        r2 = evaluate_ack_expiry(alarms, NOW)
        r3 = evaluate_ack_expiry(alarms, NOW)
        assert r1 == r2 == r3
