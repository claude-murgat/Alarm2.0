"""Unit tests (tier 1) pour backend/app/logic/quorum_detection.py.

Invariant couvert : INV-085 (perte de quorum cluster), sous-cas 1/3 — détection.

Tests purs : aucune DB, aucun cluster réel, aucun sleep. Entrées = snapshots,
sortie = QuorumState. <100ms total.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.quorum_detection import (
    ClusterSnapshot,
    QuorumState,
    evaluate_quorum_loss,
)

NOW = datetime(2026, 5, 14, 12, 0, 0)

pytestmark = pytest.mark.unit


def _healthy(ts: datetime) -> ClusterSnapshot:
    """Cluster sain : quorum présent ET Patroni joignable."""
    return ClusterSnapshot(has_quorum=True, patroni_reachable=True, timestamp=ts)


def _no_quorum(ts: datetime) -> ClusterSnapshot:
    """Quorum perdu (has_quorum=False), Patroni encore joignable."""
    return ClusterSnapshot(has_quorum=False, patroni_reachable=True, timestamp=ts)


def _patroni_down(ts: datetime) -> ClusterSnapshot:
    """Patroni injoignable, quorum nominalement présent."""
    return ClusterSnapshot(has_quorum=True, patroni_reachable=False, timestamp=ts)


def _every_minute(oldest_minutes_ago: int, factory) -> list[ClusterSnapshot]:
    """Une observation par minute, de NOW-oldest jusqu'à NOW-1 inclus."""
    return [factory(NOW - timedelta(minutes=m)) for m in range(oldest_minutes_ago, 0, -1)]


def test_quorum_lost_for_1_minute_is_not_yet_declared_lost():
    """Attrape le bug 'alerte immédiate' : has_quorum=False depuis seulement 1 min
    ne doit PAS franchir le seuil anti-flapping de 3 min → is_lost False, lost_since None."""
    history = _every_minute(1, _no_quorum)  # observation non-saine à NOW-1min
    snapshot = _no_quorum(NOW)
    state = evaluate_quorum_loss(snapshot, history)
    assert state == QuorumState(is_lost=False, lost_since=None)


def test_quorum_lost_for_4_minutes_is_declared_lost():
    """Attrape le bug 'jamais déclenché' : has_quorum=False en continu depuis 4 min
    franchit le seuil 3 min → is_lost True, lost_since = début de la série non-saine."""
    history = _every_minute(4, _no_quorum)  # NOW-4 .. NOW-1, tous non-sains
    snapshot = _no_quorum(NOW)
    state = evaluate_quorum_loss(snapshot, history)
    assert state.is_lost is True
    assert state.lost_since == NOW - timedelta(minutes=4)


def test_quorum_recovered_after_loss_resets_state():
    """Attrape le bug 'alerte collée' : après 4 min non-saines, un snapshot sain
    à NOW doit remettre l'état à zéro → is_lost False ET lost_since None."""
    history = _every_minute(4, _no_quorum)
    snapshot = _healthy(NOW)
    state = evaluate_quorum_loss(snapshot, history)
    assert state == QuorumState(is_lost=False, lost_since=None)


def test_patroni_unreachable_for_4_minutes_is_declared_lost():
    """Attrape le bug 'has_quorum seulement' : la 2e condition INV-085 (Patroni
    injoignable depuis tous les noeuds) doit aussi déclencher la perte après 4 min."""
    history = _every_minute(4, _patroni_down)
    snapshot = _patroni_down(NOW)
    state = evaluate_quorum_loss(snapshot, history)
    assert state.is_lost is True
    assert state.lost_since == NOW - timedelta(minutes=4)
