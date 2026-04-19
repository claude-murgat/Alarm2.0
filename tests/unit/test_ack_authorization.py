"""Unit tests (tier 1) pour backend/app/logic/ack_authorization.py.

Invariants couverts :
- INV-031 : seuls les users notifies peuvent ACK. Sinon 403.
  Attention : c'est un BUG catalog (code actuel ne le verifie pas) - ce test
  documente le comportement VOULU et guide le fix.

Tests purs : aucune DB, <100ms total.
"""
import pytest

from backend.app.logic.ack_authorization import evaluate_ack_authorization
from backend.app.logic.models import AckAuthorization


pytestmark = pytest.mark.unit


class TestInv031NotifiedOnlyCanAck:

    def test_user_in_notified_list_allowed(self):
        """INV-031 canonique : user est notifie → allowed=True."""
        result = evaluate_ack_authorization(
            notified_user_ids=[1, 2, 3],
            current_user_id=2,
        )
        assert result == AckAuthorization(allowed=True, reason=None)

    def test_user_not_in_notified_list_denied(self):
        """🐛 Bug a corriger : user pas notifie → allowed=False.
        Attrape : le code actuel renvoie 200 ici."""
        result = evaluate_ack_authorization(
            notified_user_ids=[1, 2],
            current_user_id=99,
        )
        assert result.allowed is False
        assert result.reason == "not_notified"

    def test_empty_notified_list_denies(self):
        """Cas degenere : notified_user_ids vide (ne devrait pas arriver vu INV-004)
        → denie par defaut (safe fallback)."""
        result = evaluate_ack_authorization(
            notified_user_ids=[],
            current_user_id=1,
        )
        assert result.allowed is False
        assert result.reason == "not_notified"

    def test_first_user_in_chain_allowed(self):
        """User #1 notifie (cas standard) → allowed."""
        result = evaluate_ack_authorization(
            notified_user_ids=[1],
            current_user_id=1,
        )
        assert result.allowed is True

    def test_escalation_cumulative_all_notified_can_ack(self):
        """INV-012 cumulative : apres escalade, tous les users cumulatifs peuvent ACK."""
        notified = [1, 2, 3]  # user1 escale vers user2 escale vers admin
        for user_id in notified:
            result = evaluate_ack_authorization(notified_user_ids=notified, current_user_id=user_id)
            assert result.allowed is True, f"user {user_id} devrait pouvoir ACK"


class TestPurity:

    def test_no_mutation(self):
        notified = [1, 2, 3]
        evaluate_ack_authorization(notified, 1)
        assert notified == [1, 2, 3]

    def test_deterministic(self):
        r1 = evaluate_ack_authorization([1, 2], 1)
        r2 = evaluate_ack_authorization([1, 2], 1)
        assert r1 == r2
