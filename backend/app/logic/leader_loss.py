"""Logique pure INV-170 : plus aucun leader Patroni dans le cluster.

Chaque noeud (leader ou non) decide seul, d'apres GET /cluster de son Patroni local : pas de
leader vu pendant plus de 3 min (QUORUM_LOSS_THRESHOLD, INV-085) -> un email par episode,
rearme des qu'un leader est vu. Fetch Patroni, email et audit : quorum_monitor._run_leader_loss_check.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .quorum_detection import QUORUM_LOSS_THRESHOLD


def has_leader(cluster: Optional[dict]) -> bool:
    """Un membre a le role leader (ou master, anciens Patroni). None = Patroni injoignable."""
    return any(m.get("role") in ("leader", "master") for m in (cluster or {}).get("members") or [])


@dataclass(frozen=True)  # pragma: no mutate (INV-170 — frozen=False est equivalent : personne ne mute cet etat)
class LeaderLossState:
    since: Optional[datetime] = None  # debut de l'episode sans leader ; None = leader vu
    emailed: bool = False


def next_state(state: LeaderLossState, leader: bool, now: datetime) -> tuple[LeaderLossState, bool]:
    """(etat suivant, email a envoyer). Seuil strict : a 3 min 00 exactement, rien."""
    if leader:
        return LeaderLossState(), False
    since = state.since or now
    email_due = not state.emailed and now - since > QUORUM_LOSS_THRESHOLD
    return LeaderLossState(since=since, emailed=state.emailed or email_due), email_due
