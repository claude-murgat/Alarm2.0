"""Logique pure INV-170 : plus aucun leader vivant vu de ce noeud.

Chaque noeud non primary decide seul : le membre que son Patroni liste comme leader (GET /cluster) doit
avoir un backend qui se dit `primary` (GET http://<host>:8000/health). Le role de /cluster seul ne suffit
pas : Patroni le lit dans etcd en serialisable, l'ancienne cle /leader survit a la perte du quorum.
Pas de leader vivant pendant plus de 3 min (QUORUM_LOSS_THRESHOLD, INV-085) -> un email par episode,
au plus un par heure. Fetchs, email et audit : quorum_monitor._run_leader_loss_check.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .quorum_detection import QUORUM_LOSS_THRESHOLD

EMAIL_MIN_INTERVAL = timedelta(hours=1)


def leader_host(cluster: Optional[dict]) -> Optional[str]:
    """Hote du membre de role `leader` (/cluster n'emet jamais `master`). None = aucun, ou Patroni injoignable."""
    return next((m.get("host") for m in (cluster or {}).get("members") or [] if m.get("role") == "leader"), None)


def no_leader_cause(cluster: Optional[dict], leader_health: Optional[dict]) -> Optional[str]:
    """None = leader vivant. `cluster` : GET /cluster du Patroni local (None si injoignable) ;
    `leader_health` : corps de /health du backend du leader liste (None si injoignable ou en erreur)."""
    if cluster is None:
        return "patroni_injoignable"
    if leader_host(cluster) is None:
        return "aucun_leader"
    if (leader_health or {}).get("role") != "primary":
        return "leader_hors_service"
    return None


@dataclass(frozen=True)  # pragma: no mutate (INV-170 — frozen=False est equivalent : personne ne mute cet etat)
class LeaderLossState:
    since: Optional[datetime] = None  # debut de l'episode sans leader ; None = leader vu
    emailed: bool = False  # email de cet episode deja parti
    last_emailed_at: Optional[datetime] = None  # survit au rearmement : plafond d'un email par heure


def next_state(state: LeaderLossState, leader: bool, now: datetime) -> tuple[LeaderLossState, bool]:
    """(etat suivant, email a envoyer). Seuil strict : a 3 min 00 exactement, rien. Plafond : 1 h pile suffit."""
    if leader:
        return LeaderLossState(last_emailed_at=state.last_emailed_at), False
    since = state.since or now
    email_due = (not state.emailed and now - since > QUORUM_LOSS_THRESHOLD
                 and (state.last_emailed_at is None or now - state.last_emailed_at >= EMAIL_MIN_INTERVAL))
    last = now if email_due else state.last_emailed_at
    return LeaderLossState(since=since, emailed=state.emailed or email_due, last_emailed_at=last), email_due
