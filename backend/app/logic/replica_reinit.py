"""Logique pure : replica Patroni bloque → re-clonage automatique (INV-168).

Un replica (role `replica` / `sync_standby`) est bloque s'il n'est pas `streaming` ou s'il
est sur une timeline plus ancienne que le leader. Bloque sans interruption depuis `after`
(SystemConfig `replica_reinit_after_minutes`, defaut 10 min) → le leader demande a son
Patroni un `POST /reinitialize` (efface ses donnees et le re-clone depuis le leader).

L'appelant (`quorum_monitor`, sur le leader seul) lit GET /cluster a chaque tick et garde en
memoire `stuck_since` (debut de la serie bloquee, renvoye a jour par `decide_reinit`) et
`last_handled` (dernier reinit ou alerte garde-fou, par membre).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

REINIT_AFTER_DEFAULT = timedelta(minutes=10)
# Au plus un reinit (ou une alerte garde-fou) par membre et par heure.
REINIT_COOLDOWN = timedelta(minutes=60)
_REPLICA_ROLES = ("replica", "sync_standby")


@dataclass(frozen=True)  # pragma: no mutate (INV-168 — frozen=False est equivalent : aucun code ne mute ce verdict)
class ReinitDecision:
    """- stuck_since : a garder pour le tick suivant (membres bloques en ce moment seulement).
    - target : membre /cluster a re-cloner ce tick (un seul), sinon None ; reason : pourquoi.
    - blocked : plus de la moitie des replicas bloques → pas de reinit, alerte pour ces membres.
    """
    stuck_since: dict
    target: Optional[dict] = None
    reason: Optional[str] = None
    blocked: tuple = ()


def _is_stuck(member: dict, leader_timeline) -> bool:
    timeline = member.get("timeline")  # optionnel dans /cluster : non compare s'il manque
    return member.get("state") != "streaming" or (
        timeline is not None and leader_timeline is not None and timeline < leader_timeline)


def decide_reinit(cluster: dict, stuck_since: dict, last_handled: dict,
                  now: datetime, after: timedelta) -> ReinitDecision:
    """Un tick INV-168. `cluster` = JSON de GET {PATRONI_URL}/cluster."""
    members = cluster.get("members", [])
    leader = next((m for m in members if m.get("role") == "leader"), None)
    # Maintenance manuelle, pas de leader, ou leader pas sain : les etats des replicas ne
    # veulent rien dire, on ne touche a rien et la serie repart de zero.
    if cluster.get("pause") or leader is None or leader.get("state") != "running":
        return ReinitDecision(stuck_since={})
    replicas = [m for m in members if m.get("role") in _REPLICA_ROLES]
    stuck = {m["name"]: m for m in replicas if _is_stuck(m, leader.get("timeline"))}
    since = {name: stuck_since.get(name, now) for name in stuck}
    due = [name for name in stuck if now - since[name] >= after
           and (name not in last_handled or now - last_handled[name] >= REINIT_COOLDOWN)]
    if not due:
        return ReinitDecision(stuck_since=since)
    # Garde-fou : la majorite des replicas bloques = probleme cote leader ou reseau.
    if 2 * len(stuck) > len(replicas):
        return ReinitDecision(stuck_since=since, blocked=tuple(due))
    target = stuck[due[0]]
    reason = (f"state={target.get('state')} timeline={target.get('timeline')} "
              f"leader_timeline={leader.get('timeline')}")
    return ReinitDecision(stuck_since=since, target=target, reason=reason)


def reinit_after(raw: Optional[str]) -> timedelta:
    """Valeur SystemConfig `replica_reinit_after_minutes` ; absente, invalide ou <= 0 → 10 min."""
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        return REINIT_AFTER_DEFAULT
    return timedelta(minutes=minutes) if minutes > 0 else REINIT_AFTER_DEFAULT


def reinit_url(api_url: str) -> str:
    """`api_url` du membre (ex. http://10.99.0.4:8008/patroni) → URL de son POST /reinitialize."""
    parts = urlsplit(api_url)
    return f"{parts.scheme}://{parts.netloc}/reinitialize"
