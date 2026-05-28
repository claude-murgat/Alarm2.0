"""Unit tests (tier 1) pour backend/app/logic/oncall.py.

Invariants couverts (après dépréciation INV-050/051/052/054 le 2026-05-26) :
- INV-053 : pos 1 offline >= delay + tous offline → email direction technique
  (au plus une fois par épisode, marker effacé dès qu'un user repasse online).

Les anciens tests TestInv050/051/052/054/055/AlarmUnicity/EscalatedStatus/
AssignmentDistinguishes... ont été supprimés en même temps que le code de
création d'alarme oncall (cf tests/INVARIANTS.md §5 encadré "Changement de
stratégie 2026-05-26" + INV-056 pour le tracking statistique qui remplace).

Tests purs : aucune DB, <100ms total.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.oncall import evaluate_oncall_heartbeat
from backend.app.logic.models import (
    AlarmSnapshot,
    EscalationChainEntry,
    OncallActions,
    UserSnapshot,
)


NOW = datetime(2026, 4, 17, 12, 0, 0)
DELAY = 15.0  # ONCALL_OFFLINE_DELAY_MINUTES


def _user(
    user_id: int,
    name: str = "user",
    is_online: bool = True,
    last_heartbeat: datetime | None = None,
) -> UserSnapshot:
    return UserSnapshot(
        id=user_id,
        name=name,
        is_online=is_online,
        last_heartbeat=last_heartbeat if last_heartbeat is not None else NOW,
    )


def _chain(*user_ids: int) -> list[EscalationChainEntry]:
    return [EscalationChainEntry(position=i + 1, user_id=uid) for i, uid in enumerate(user_ids)]


def _alarm(
    alarm_id: int = 1,
    status: str = "active",
    is_oncall_alarm: bool = False,
    assigned_user_id: int = 2,
) -> AlarmSnapshot:
    return AlarmSnapshot(
        id=alarm_id,
        status=status,
        created_at=NOW - timedelta(minutes=5),
        suspended_until=None,
        assigned_user_id=assigned_user_id,
        escalation_count=0,
        is_oncall_alarm=is_oncall_alarm,
    )


pytestmark = pytest.mark.unit


class TestEdgeCases:

    def test_empty_chain_no_action(self):
        """Pas de chaine → aucune action (pas de crash)."""
        result = evaluate_oncall_heartbeat([], [_user(1)], [], DELAY, NOW)
        assert result == OncallActions()

    def test_oncall_user_not_in_users_no_action(self):
        """Chaîne pointe vers un user qui n'existe pas dans `users` → aucune action.
        Cas : user #1 supprimé mais chaîne pas encore mise à jour."""
        chain = _chain(99)  # user 99 pas dans la liste
        result = evaluate_oncall_heartbeat(chain, [_user(2)], [], DELAY, NOW)
        assert result == OncallActions()

    def test_oncall_never_had_heartbeat_no_action(self):
        """Cas degenere : oncall offline + jamais de heartbeat → aucune action
        (on ne peut pas savoir depuis combien de temps)."""
        user1 = UserSnapshot(id=1, name="user1", is_online=False, last_heartbeat=None)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, _user(2)], [], DELAY, NOW)
        # Pas d'email, pas de marker à clear (marker pas posé)
        assert result.emails == ()
        assert result.email_marker_set is False


class TestInv053EmailIfNobodyOnline:
    """INV-053 : personne en ligne + oncall offline > delay → email direction technique."""

    def test_nobody_online_sends_email(self):
        """Oncall offline + tous les autres offline → email."""
        user1 = _user(1, "user1", is_online=False, last_heartbeat=NOW - timedelta(minutes=16))
        user2 = _user(2, "user2", is_online=False)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert len(result.emails) == 1
        assert result.emails[0].oncall_user_name == "user1"
        assert 15.9 <= result.emails[0].offline_duration_minutes <= 16.1
        assert result.email_marker_set is True

    def test_below_delay_no_email(self):
        """Oncall offline depuis 14 min (< 15) → pas d'email."""
        user1 = _user(1, is_online=False, last_heartbeat=NOW - timedelta(minutes=14))
        user2 = _user(2, is_online=False)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert result.emails == ()
        assert result.email_marker_set is False

    def test_at_exact_delay_sends_email(self):
        """Boundary : offline = delay pile → email (>=).
        Attrape : regression si >= devient >."""
        user1 = _user(1, "user1", is_online=False, last_heartbeat=NOW - timedelta(minutes=15))
        user2 = _user(2, "user2", is_online=False)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert len(result.emails) == 1


class TestInv053EmailMarkerOneShotPerEpisode:
    """INV-053 (issue #116) : email "personne online" envoye AU PLUS UNE FOIS
    par episode. Sans cette regle, escalation_loop (tick ~10s) spam la direction
    technique tant que personne ne revient online (80 mails en 13 min observes
    en prod 2026-05-18).

    Un episode = condition INV-053 vraie sans interruption. Il se termine des
    qu'au moins un user redevient online (marker clear), puis se rouvre quand
    tous retombent offline (marker set re-autorise).
    """

    def test_inv053_first_episode_sends_email_and_sets_marker(self):
        """Premier tick d'un nouvel episode : email envoye + marker_set demande.

        Attrape : regression si l'email cesse d'etre envoye sur le tout premier
        tick (regression vs INV-053) OU si le marker_set n'est pas demande
        (alors le 2eme tick re-enverrait → bug initial)."""
        user1 = _user(1, "user1", is_online=False,
                      last_heartbeat=NOW - timedelta(minutes=16))
        user2 = _user(2, "user2", is_online=False)
        result = evaluate_oncall_heartbeat(
            _chain(1, 2), [user1, user2], [], DELAY, NOW,
            email_already_sent=False,
        )
        assert len(result.emails) == 1, (
            "premier tick d'un episode INV-053 : 1 email exactement"
        )
        assert result.email_marker_set is True, (
            "le marker doit etre pose pour bloquer les ticks suivants"
        )
        assert result.email_marker_clear is False, (
            "rien a clear sur un premier tick (marker etait deja vide)"
        )

    def test_inv053_second_tick_same_episode_does_not_resend(self):
        """RED PROOF du bug #116 : 2eme tick du meme episode → ZERO email.

        Avant le fix, evaluate_oncall_heartbeat renvoyait toujours un email
        des que `not online_users`, sans regarder le marker. Resultat : 80 mails
        en 13 min en prod (1 par tick de 10s).

        Apres le fix : email_already_sent=True coupe l'emission tant que le
        marker n'est pas clear (au moins un user qui revient online)."""
        user1 = _user(1, "user1", is_online=False,
                      last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, "user2", is_online=False)
        result = evaluate_oncall_heartbeat(
            _chain(1, 2), [user1, user2], [], DELAY, NOW,
            email_already_sent=True,
        )
        assert len(result.emails) == 0, (
            "marker deja pose → AUCUN email tant que personne n'est revenu "
            "online (sinon spam comme issue #116)"
        )
        assert result.email_marker_set is False, (
            "ne pas re-set un marker deja pose (no-op)"
        )

    def test_inv053_episode_ends_when_user_returns_online_clears_marker(self):
        """Fin d'episode : au moins un user revient online → marker_clear demande.

        Setup : oncall (pos 1) reste offline > delay, user2 revient online,
        marker etait pose (email_already_sent=True). On veut le clear pour
        re-autoriser un email lors d'un FUTUR episode (si tout le monde
        retombe offline). Note 2026-05-26 : avec la dépréciation INV-050,
        on ne crée plus d'alarme — le marker_clear se manifeste seul."""
        user1 = _user(1, "user1", is_online=False,
                      last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, "user2", is_online=True)
        result = evaluate_oncall_heartbeat(
            _chain(1, 2), [user1, user2], [], DELAY, NOW,
            email_already_sent=True,
        )
        assert result.email_marker_clear is True, (
            "au moins un user online + marker pose → demander le clear pour "
            "fermer l'episode et reautoriser un futur email"
        )
        assert result.email_marker_set is False, (
            "flags mutuellement exclusifs : on ne set pas et clear en meme temps"
        )

    def test_inv053_no_marker_change_when_already_clear_and_online(self):
        """No-op : marker deja vide + user online → ni set ni clear.

        Cas nominal entre deux episodes. Evite des ecritures DB inutiles a
        chaque tick (le bug initial venait justement d'ecrire/envoyer un email
        a chaque tick — meme principe applique au marker)."""
        user1 = _user(1, "user1", is_online=False,
                      last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, "user2", is_online=True)
        result = evaluate_oncall_heartbeat(
            _chain(1, 2), [user1, user2], [], DELAY, NOW,
            email_already_sent=False,
        )
        assert result.email_marker_set is False
        assert result.email_marker_clear is False, (
            "marker deja vide → rien a clear (no-op pour eviter ecritures "
            "DB a chaque tick)"
        )


class TestNoAlarmCreatedAfterDeprecation20260526:
    """Lock contre une régression de la dépréciation INV-050/051/052/054 (2026-05-26).

    Ces tests verrouillent le fait que evaluate_oncall_heartbeat ne produit JAMAIS
    de creation ni de resolution d'alarme, peu importe le scénario qui auparavant
    aurait déclenché ces actions. Si quelqu'un réintroduit ces champs dans
    OncallActions sans mise à jour du catalogue (tests/INVARIANTS.md §5), ces
    tests cassent immédiatement."""

    def test_oncall_offline_with_other_user_online_no_creation(self):
        """Scénario INV-050 d'avant-dépréciation : oncall offline > delay + user2 online.
        Plus aucune création d'alarme ne doit se produire."""
        user1 = _user(1, "user1", is_online=False,
                      last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, "user2", is_online=True)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert not hasattr(result, "creations") or result.creations == (), (
            "INV-050 déprécié : plus de création d'alarme oncall_offline"
        )
        assert not hasattr(result, "resolutions") or result.resolutions == (), (
            "INV-051 déprécié : plus de résolution non plus"
        )
        assert result.emails == (), (
            "INV-053 : un user online → pas d'email non plus"
        )

    def test_oncall_back_online_with_existing_oncall_alarm_no_resolution(self):
        """Scénario INV-051 d'avant-dépréciation : oncall revient online + alarme oncall existe.
        L'auto-résolution est supprimée — l'alarme legacy reste, à traiter manuellement."""
        user1 = _user(1, "user1", is_online=True)
        user2 = _user(2, "user2")
        oncall_alarm = _alarm(alarm_id=42, is_oncall_alarm=True, assigned_user_id=2)
        result = evaluate_oncall_heartbeat(
            _chain(1, 2), [user1, user2], [oncall_alarm], DELAY, NOW
        )
        assert not hasattr(result, "resolutions") or result.resolutions == ()
        assert result.emails == ()


class TestPurity:

    def test_no_mutation_of_inputs(self):
        user1 = _user(1, is_online=False, last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, is_online=True)
        chain = _chain(1, 2)
        evaluate_oncall_heartbeat(chain, [user1, user2], [], DELAY, NOW)
        assert user1.is_online is False
        assert chain[0].position == 1

    def test_deterministic(self):
        user1 = _user(1, is_online=False, last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, is_online=False)
        r1 = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        r2 = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert r1 == r2
