"""Unit tests (tier 1) pour backend/app/logic/oncall.py.

Invariants couverts :
- INV-050 : oncall offline >15min → alarme auto créée
- INV-051 : oncall revient online → alarme auto-résolue
- INV-052 : alarme oncall assignée au SUIVANT, pas au #1
- INV-053 : personne en ligne → email direction technique
- INV-054 : pas de doublon d'alarme oncall
- INV-055 : seul position 1 déclenche la surveillance
- INV-001 : pas de création si une autre alarme est active (unicité)

Tests purs : aucune DB, <100ms total.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.oncall import evaluate_oncall_heartbeat
from backend.app.logic.models import (
    AlarmSnapshot,
    DirectionTechniqueEmail,
    EscalationChainEntry,
    OncallActions,
    OncallAlarmCreation,
    OncallAlarmResolution,
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
        assert result == OncallActions(resolutions=(), creations=(), emails=())

    def test_oncall_user_not_in_users_no_action(self):
        """Chaîne pointe vers un user qui n'existe pas dans `users` → aucune action.
        Cas : user #1 supprimé mais chaîne pas encore mise à jour."""
        chain = _chain(99)  # user 99 pas dans la liste
        result = evaluate_oncall_heartbeat(chain, [_user(2)], [], DELAY, NOW)
        assert result == OncallActions(resolutions=(), creations=(), emails=())

    def test_oncall_never_had_heartbeat_no_action(self):
        """Cas degenere : oncall offline + jamais de heartbeat → aucune action
        (on ne peut pas savoir depuis combien de temps)."""
        user1 = UserSnapshot(id=1, name="user1", is_online=False, last_heartbeat=None)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, _user(2)], [], DELAY, NOW)
        assert result == OncallActions(resolutions=(), creations=(), emails=())


class TestInv051AutoResolveOnReconnect:
    """INV-051 : oncall revient online → alarme oncall auto-résolue."""

    def test_oncall_online_with_oncall_alarm_resolves(self):
        """Oncall (pos 1) online + alarme oncall active → resolution."""
        user1 = _user(1, "user1", is_online=True)
        user2 = _user(2, "user2")
        oncall_alarm = _alarm(alarm_id=42, is_oncall_alarm=True, assigned_user_id=2)
        result = evaluate_oncall_heartbeat(
            _chain(1, 2), [user1, user2], [oncall_alarm], DELAY, NOW
        )
        assert result.resolutions == (OncallAlarmResolution(alarm_id=42),)
        assert result.creations == ()
        assert result.emails == ()

    def test_oncall_online_no_oncall_alarm_no_action(self):
        """Oncall online + pas d'alarme oncall → aucune action.
        Attrape : regression si on creerait une alarme quand oncall est online."""
        user1 = _user(1, is_online=True)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, _user(2)], [], DELAY, NOW)
        assert result == OncallActions(resolutions=(), creations=(), emails=())

    def test_oncall_online_ignores_non_oncall_alarms(self):
        """Oncall online + alarme classique (pas oncall) → pas de resolution de l'alarme classique."""
        user1 = _user(1, is_online=True)
        regular_alarm = _alarm(alarm_id=10, is_oncall_alarm=False)
        result = evaluate_oncall_heartbeat(
            _chain(1, 2), [user1, _user(2)], [regular_alarm], DELAY, NOW
        )
        assert result.resolutions == ()


class TestInv050CreateAlarmAfterDelay:
    """INV-050 : oncall offline > delay → alarme auto créée."""

    def test_oncall_offline_above_delay_creates_alarm(self):
        """Offline 16 min + user2 online → creation alarme oncall assignee a user2."""
        user1 = _user(1, "user1", is_online=False, last_heartbeat=NOW - timedelta(minutes=16))
        user2 = _user(2, "user2", is_online=True)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert len(result.creations) == 1
        creation = result.creations[0]
        assert creation.oncall_user_name == "user1"
        assert 15.9 <= creation.offline_duration_minutes <= 16.1
        assert creation.assigned_user_id == 2  # INV-052

    def test_oncall_offline_exactly_at_delay_creates_alarm(self):
        """Boundary : offline = delay pile → creation (>=).
        Attrape : regression si >= devient >."""
        user1 = _user(1, "user1", is_online=False, last_heartbeat=NOW - timedelta(minutes=15))
        user2 = _user(2, "user2", is_online=True)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert len(result.creations) == 1

    def test_oncall_offline_below_delay_no_action(self):
        """Offline 14 min (< 15) → pas de creation."""
        user1 = _user(1, is_online=False, last_heartbeat=NOW - timedelta(minutes=14))
        user2 = _user(2, is_online=True)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert result == OncallActions(resolutions=(), creations=(), emails=())


class TestInv052AssignedToNextOnline:
    """INV-052 : alarme oncall assignée au SUIVANT, pas au #1.
    Si le next n'est pas online, prendre le premier online."""

    def test_assigned_to_pos2_if_online(self):
        """Next dans la chaine (pos 2) online → assigned_user_id = user2."""
        user1 = _user(1, "u1", is_online=False, last_heartbeat=NOW - timedelta(minutes=16))
        user2 = _user(2, "u2", is_online=True)
        user3 = _user(3, "u3", is_online=True)
        result = evaluate_oncall_heartbeat(
            _chain(1, 2, 3), [user1, user2, user3], [], DELAY, NOW
        )
        assert result.creations[0].assigned_user_id == 2

    def test_skips_offline_users_in_chain(self):
        """Si user2 offline, user3 online → assigned au user3 (skip offline)."""
        user1 = _user(1, "u1", is_online=False, last_heartbeat=NOW - timedelta(minutes=16))
        user2 = _user(2, "u2", is_online=False)
        user3 = _user(3, "u3", is_online=True)
        result = evaluate_oncall_heartbeat(
            _chain(1, 2, 3), [user1, user2, user3], [], DELAY, NOW
        )
        assert result.creations[0].assigned_user_id == 3


class TestInv053EmailIfNobodyOnline:
    """INV-053 : personne en ligne + oncall offline > delay → email direction technique."""

    def test_nobody_online_sends_email(self):
        """Oncall offline + tous les autres offline → email, pas de creation."""
        user1 = _user(1, "user1", is_online=False, last_heartbeat=NOW - timedelta(minutes=16))
        user2 = _user(2, "user2", is_online=False)
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert result.creations == ()
        assert len(result.emails) == 1
        assert result.emails[0].oncall_user_name == "user1"
        assert 15.9 <= result.emails[0].offline_duration_minutes <= 16.1


class TestInv054NoDuplicateOncallAlarm:
    """INV-054 : pas de doublon d'alarme oncall."""

    def test_existing_oncall_alarm_prevents_creation(self):
        """Alarme oncall deja active → pas de nouvelle creation."""
        user1 = _user(1, is_online=False, last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, is_online=True)
        existing = _alarm(alarm_id=1, is_oncall_alarm=True, status="active")
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [existing], DELAY, NOW)
        assert result.creations == ()

    def test_existing_oncall_alarm_escalated_prevents_creation(self):
        """Alarme oncall en etat 'escalated' → pas de nouvelle creation non plus."""
        user1 = _user(1, is_online=False, last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, is_online=True)
        existing = _alarm(alarm_id=1, is_oncall_alarm=True, status="escalated")
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [existing], DELAY, NOW)
        assert result.creations == ()

    def test_resolved_oncall_alarm_does_not_block_creation(self):
        """Alarme oncall resolved (historique) → on peut creer une nouvelle.
        Attrape : regression si on bloque aussi sur resolved."""
        user1 = _user(1, "user1", is_online=False, last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, is_online=True)
        resolved = _alarm(alarm_id=1, is_oncall_alarm=True, status="resolved")
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [resolved], DELAY, NOW)
        assert len(result.creations) == 1


class TestInv001AlarmUnicity:
    """INV-001 : pas de creation si une autre alarme (non-oncall) est active."""

    def test_regular_active_alarm_blocks_oncall_creation(self):
        """Une alarme classique active → pas de creation d'alarme oncall (contrainte unicité).
        Attrape : regression si on creait l'alarme oncall meme avec une alarme active existante."""
        user1 = _user(1, is_online=False, last_heartbeat=NOW - timedelta(minutes=20))
        user2 = _user(2, is_online=True)
        regular = _alarm(alarm_id=1, is_oncall_alarm=False, status="active")
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [regular], DELAY, NOW)
        assert result.creations == ()


class TestInv055OnlyPos1IsMonitored:
    """INV-055 : seul le #1 (position 1) est surveillé, pas les positions 2+."""

    def test_pos2_offline_does_not_trigger_creation(self):
        """User en pos 2 offline mais pos 1 online → aucune action.
        (Contre-test : seul pos 1 declenche le check.)"""
        user1 = _user(1, is_online=True)
        user2 = _user(2, is_online=False, last_heartbeat=NOW - timedelta(minutes=20))
        result = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert result == OncallActions(resolutions=(), creations=(), emails=())


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
        user2 = _user(2, is_online=True)
        r1 = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        r2 = evaluate_oncall_heartbeat(_chain(1, 2), [user1, user2], [], DELAY, NOW)
        assert r1 == r2


class TestEscalatedStatusInExistingAlarms:
    """Tue les mutations sur les strings 'escalated' (mutmut 9 et 24).
    Sans ces tests, `"escalated"` peut etre remplace par n'importe quoi (ex:
    `"XXescalatedXX"`) sans qu'aucun test ne le detecte — alors qu'une alarme
    en status='escalated' est un cas REAL."""

    def test_existing_escalated_oncall_alarm_is_resolved_when_user_returns(self):
        """INV-051 + mutmut 9 : alarme oncall en status='escalated' doit etre
        detectee comme 'active' au sens fonctionnel (= a resoudre quand l'oncall
        revient online)."""
        chain = _chain(1, 2)
        users = [_user(1, "u1", is_online=True), _user(2, "u2")]
        existing = _alarm(
            alarm_id=99, status="escalated", is_oncall_alarm=True, assigned_user_id=2,
        )
        result = evaluate_oncall_heartbeat(chain, users, [existing], DELAY, NOW)
        assert result.resolutions == (OncallAlarmResolution(alarm_id=99),)

    def test_existing_escalated_non_oncall_alarm_blocks_creation(self):
        """INV-001 + mutmut 24 : une alarme NON-oncall en status='escalated'
        doit aussi bloquer la creation d'une alarme oncall (contrainte unicite,
        pas seulement les status='active')."""
        chain = _chain(1, 2)
        users = [
            _user(1, "u1", is_online=False, last_heartbeat=NOW - timedelta(minutes=20)),
            _user(2, "u2", is_online=True),
        ]
        existing = _alarm(
            alarm_id=99, status="escalated", is_oncall_alarm=False, assigned_user_id=2,
        )
        result = evaluate_oncall_heartbeat(chain, users, [existing], DELAY, NOW)
        assert result.creations == ()


class TestAssignmentDistinguishesChainFromFallback:
    """Tue les mutations 26, 27, 28, 29, 31, 32, 33, 34 — toutes liees au choix
    entre _find_next_online_in_chain (priorite) et le fallback online_users[0].

    Les tests existants (TestInv052) ne distinguent pas les deux car online_users[0]
    coincidait souvent avec le user de la chaine — il faut un setup ou les deux
    branches donnent des resultats DIFFERENTS pour les attraper."""

    def test_chain_online_user_preferred_over_non_chain_online(self):
        """Mutmut 26, 27, 31, 32, 33, 34 : si un user de la CHAINE est online,
        on l'assigne — meme si un user HORS chaine est aussi online (et listed
        avant lui dans `users`).

        Setup critique : online_users[0] doit etre un user HORS chaine pour que
        le fallback donne un resultat different de _find_next_online_in_chain.
        """
        chain = _chain(1, 2, 3)
        users = [
            # oncall offline
            _user(1, "oncall", is_online=False,
                  last_heartbeat=NOW - timedelta(minutes=20)),
            # online HORS chaine — listed FIRST, donc serait online_users[0] si fallback
            _user(99, "outsider", is_online=True),
            # in chain mais offline
            _user(2, "u2", is_online=False),
            # in chain ET online — c'est CE user qui doit etre choisi par INV-052
            _user(3, "u3", is_online=True),
        ]
        result = evaluate_oncall_heartbeat(chain, users, [], DELAY, NOW)
        assert len(result.creations) == 1
        # Avec n'importe laquelle des mutations 26/27/31-34, _find_next_online_in_chain
        # retourne None ou la fonction tombe dans le fallback → assigned = user 99.
        assert result.creations[0].assigned_user_id == 3, (
            "INV-052 doit choisir un user de la chaine en priorite, jamais "
            "online_users[0] tant qu'un user de la chaine est online."
        )

    def test_falls_back_to_online_users_first_when_no_chain_online(self):
        """Mutmut 28, 29 : si aucun user de la chaine (sauf l'oncall) n'est online,
        fallback a online_users[0]. Test calibre avec UN SEUL user online pour
        attraper [0]→[1] (IndexError) ou [0]→None (no creation)."""
        chain = _chain(1)  # uniquement l'oncall, pas de "next" dans la chaine
        users = [
            _user(1, "oncall", is_online=False,
                  last_heartbeat=NOW - timedelta(minutes=20)),
            _user(99, "outsider", is_online=True),  # exactement 1 online, hors chaine
        ]
        result = evaluate_oncall_heartbeat(chain, users, [], DELAY, NOW)
        assert len(result.creations) == 1
        assert result.creations[0].assigned_user_id == 99
