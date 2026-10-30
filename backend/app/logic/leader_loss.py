"""Logique pure INV-170 : plus aucun leader vivant vu de ce noeud.

Chaque noeud non primary decide seul : un leader vivant est vu si un backend pair (ceux du relai
d'ecriture INV-044) se dit `primary` sur /health, donc si une ecriture relayee d'ici peut aboutir. Le role
de /cluster seul ne suffit pas : Patroni le lit dans etcd en serialisable, l'ancienne cle /leader survit
a la perte du quorum ; /cluster (Patroni local) ne sert qu'a nommer la cause.
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


def no_leader_cause(cluster: Optional[dict], primary_seen: bool) -> Optional[str]:
    """None = leader vivant : un backend pair repond 200 avec role `primary` (`primary_seen`).
    Sinon la cause, d'apres `cluster` : GET /cluster du Patroni local (None si injoignable)."""
    if primary_seen:
        return None
    if cluster is None:
        return "patroni_injoignable"
    if leader_host(cluster) is None:
        return "aucun_leader"
    return "leader_hors_service"


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
