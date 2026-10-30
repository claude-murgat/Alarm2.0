"""Unit tests (tier 1) — INV-170 : plus aucun leader Patroni (backend/app/logic/leader_loss.py).

Logique pure : entrees = reponse GET /cluster du Patroni local (ou None), etat, horloge ;
sorties = etat suivant, email du. Chaque noeud decide seul : sans leader, pas de coordination.
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.logic.leader_loss import LeaderLossState, has_leader, next_state

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _cluster(*roles):
    return {"members": [{"name": f"node{i}", "role": r, "state": "running"} for i, r in enumerate(roles, 1)]}


class TestHasLeader:
    """Le cluster a un leader si un membre a le role leader (ou master, anciens Patroni)."""

    def test_leader(self):
        assert has_leader(_cluster("leader", "replica", "replica")) is True

    def test_master_old_patroni(self):
        assert has_leader(_cluster("master", "replica")) is True

    def test_replicas_only(self):
        assert has_leader(_cluster("replica", "replica", "replica")) is False

    def test_no_members(self):
        assert has_leader({"members": []}) is False
        assert has_leader({}) is False

    def test_patroni_unreachable(self):
        assert has_leader(None) is False


def _run(steps):
    """steps : (secondes depuis T0, leader vu). Renvoie l'etat final et les instants (s) d'email."""
    state, emails = LeaderLossState(), []
    for seconds, leader in steps:
        state, due = next_state(state, leader, T0 + timedelta(seconds=seconds))
        if due:
            emails.append(seconds)
    return state, emails


class TestNextState:
    """Sans leader pendant plus de 3 min (strict) -> un email par episode, rearme au retour."""

    def test_leader_seen_never_emails(self):
        state, emails = _run([(s, True) for s in range(0, 3600, 60)])
        assert emails == []
        assert state.since is None and state.emailed is False

    def test_since_is_the_first_tick_without_leader(self):
        state, emails = _run([(0, False), (60, False)])
        assert state.since == T0 and state.emailed is False
        assert emails == []

    def test_2_min_59_no_email(self):
        assert _run([(0, False), (60, False), (179, False)])[1] == []

    def test_exactly_3_min_no_email(self):
        assert _run([(0, False), (180, False)])[1] == []

    def test_3_min_01_emails(self):
        state, emails = _run([(0, False), (181, False)])
        assert emails == [181]
        assert state.since == T0 and state.emailed is True

    def test_one_email_per_episode(self):
        assert _run([(0, False), (181, False), (240, False), (3600, False), (86400, False)])[1] == [181]

    def test_leader_back_resets_the_state(self):
        state, emails = _run([(0, False), (181, False), (200, True)])
        assert emails == [181]
        assert state.since is None and state.emailed is False

    def test_leader_back_then_lost_again_emails_again(self):
        state, emails = _run([(0, False), (181, False), (200, True), (300, False), (480, False), (481, False)])
        assert emails == [181, 481]
        assert state.since == T0 + timedelta(seconds=300)

    def test_short_glitch_restarts_the_count(self):
        # Leader vu un tick au milieu : les 3 min repartent de la nouvelle perte.
        assert _run([(0, False), (100, True), (150, False), (300, False), (330, False)])[1] == []
