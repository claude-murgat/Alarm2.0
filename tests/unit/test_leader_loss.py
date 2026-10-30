"""Unit tests (tier 1) — INV-170 : plus aucun leader vivant vu d'un noeud (backend/app/logic/leader_loss.py).

Logique pure : entrees = reponse GET /cluster du Patroni local (ou None), /health du backend du
leader liste (ou None), etat, horloge ; sorties = cause, etat suivant, email du.
Chaque noeud decide seul : sans leader, pas de coordination.
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.logic.leader_loss import LeaderLossState, leader_host, next_state, no_leader_cause

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _cluster(*roles):
    return {"members": [{"name": f"node{i}", "role": r, "state": "running", "host": f"10.99.0.{i}"}
                        for i, r in enumerate(roles, 1)]}


class TestLeaderHost:
    """Hote du membre que Patroni liste comme leader (role `leader` seulement : /cluster n'emet jamais `master`)."""

    def test_leader(self):
        assert leader_host(_cluster("replica", "leader", "replica")) == "10.99.0.2"

    def test_master_is_not_a_cluster_role(self):
        assert leader_host(_cluster("master", "replica")) is None

    def test_replicas_only(self):
        assert leader_host(_cluster("replica", "replica", "replica")) is None

    def test_no_members(self):
        assert leader_host({"members": []}) is None
        assert leader_host({}) is None

    def test_patroni_unreachable(self):
        assert leader_host(None) is None


class TestNoLeaderCause:
    """None = leader vivant (son backend dit primary). Sinon une des trois causes."""

    PRIMARY = {"status": "ok", "role": "primary"}

    def test_live_leader(self):
        assert no_leader_cause(_cluster("leader", "replica"), self.PRIMARY) is None

    def test_patroni_unreachable(self):
        assert no_leader_cause(None, None) == "patroni_injoignable"

    def test_no_leader_listed(self):
        assert no_leader_cause(_cluster("replica", "replica"), None) == "aucun_leader"
        assert no_leader_cause({}, self.PRIMARY) == "aucun_leader"

    def test_listed_leader_backend_unreachable(self):
        # Cas reproduit : quorum etcd perdu, /cluster sert l'ancienne cle /leader.
        assert no_leader_cause(_cluster("leader", "replica"), None) == "leader_hors_service"

    def test_listed_leader_backend_not_primary(self):
        assert no_leader_cause(_cluster("leader"), {"role": "secondary"}) == "leader_hors_service"
        assert no_leader_cause(_cluster("leader"), {}) == "leader_hors_service"


def _run(steps):
    """steps : (secondes depuis T0, leader vu). Renvoie l'etat final et les instants (s) d'email."""
    state, emails = LeaderLossState(), []
    for seconds, leader in steps:
        state, due = next_state(state, leader, T0 + timedelta(seconds=seconds))
        if due:
            emails.append(seconds)
    return state, emails


class TestNextState:
    """Sans leader pendant plus de 3 min (strict) -> un email par episode, au plus un par heure."""

    def test_leader_seen_never_emails(self):
        state, emails = _run([(s, True) for s in range(0, 3600, 60)])
        assert emails == []
        assert state.since is None and state.emailed is False and state.last_emailed_at is None

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
        assert state.last_emailed_at == T0 + timedelta(seconds=181)

    def test_one_email_per_episode(self):
        assert _run([(0, False), (181, False), (240, False), (3600, False), (86400, False)])[1] == [181]

    def test_leader_back_resets_the_episode_but_remembers_the_last_email(self):
        state, emails = _run([(0, False), (181, False), (200, True)])
        assert emails == [181]
        assert state.since is None and state.emailed is False
        assert state.last_emailed_at == T0 + timedelta(seconds=181)

    def test_flapping_leader_emails_at_most_once_per_hour(self):
        # Leader vu un tick sur cinq : chaque episode depasse 3 min, mais un seul email par heure.
        steps, t = [], 0
        while t < 2 * 3600:
            steps += [(t, False), (t + 60, False), (t + 120, False), (t + 181, False), (t + 240, True)]
            t += 300
        emails = _run(steps)[1]
        assert emails[0] == 181
        assert all(b - a >= 3600 for a, b in zip(emails, emails[1:]))
        assert len(emails) == 2

    def test_new_episode_59_min_59_after_the_last_email_waits(self):
        # Email a 181 s, leader revu, nouvelle perte : 1 h apres le 1er email = 3781 s.
        steps = [(0, False), (181, False), (200, True), (3000, False), (3780, False)]
        assert _run(steps)[1] == [181]

    def test_new_episode_emails_once_the_hour_is_over(self):
        steps = [(0, False), (181, False), (200, True), (3000, False), (3780, False), (3781, False), (3840, False)]
        assert _run(steps)[1] == [181, 3781]

    def test_leader_back_then_lost_again_hours_later_emails_again(self):
        steps = [(0, False), (181, False), (200, True), (7200, False), (7380, False), (7381, False)]
        state, emails = _run(steps)
        assert emails == [181, 7381]
        assert state.since == T0 + timedelta(seconds=7200)

    def test_short_glitch_restarts_the_count(self):
        # Leader vu un tick au milieu : les 3 min repartent de la nouvelle perte.
        assert _run([(0, False), (100, True), (150, False), (300, False), (330, False)])[1] == []
