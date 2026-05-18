"""Logique pure : détection de la perte de quorum cluster (INV-085, sous-cas 1/3).

Invariant INV-085 [C] : le système doit détecter une perte de quorum quand :
- has_quorum == False (moins de N/2+1 noeuds healthy dans Patroni/etcd), OU
- Patroni injoignable depuis tous les noeuds.

Un seuil de 3 minutes (anti-flapping) s'applique aux DEUX conditions : un état
"non sain" (has_quorum=False OU patroni_reachable=False) doit persister de façon
CONTINUE pendant > 3 minutes avant que le quorum soit déclaré perdu. Cela couvre
les glitches courts (redémarrage Patroni) sans déclencher de fausse alerte.

Cette sous-issue (#79) traite UNIQUEMENT la détection. L'envoi d'email (#80) et
les reminders anti-spam (#81) sont dans des sous-issues séparées.

L'appelant (tick périodique) est responsable de :
1. Construire le ClusterSnapshot courant (lecture de /api/cluster + ping Patroni).
2. Maintenir un historique des snapshots récents (couvrant >= 3 min).
3. Appeler evaluate_quorum_loss(snapshot, history).
4. Persister QuorumState.lost_since (survie aux restarts backend).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# Seuil anti-flapping : un état non-sain doit durer STRICTEMENT plus longtemps
# que ça pour que le quorum soit déclaré perdu (INV-085, "> 3 minutes").
QUORUM_LOSS_THRESHOLD = timedelta(minutes=3)


@dataclass(frozen=True)  # pragma: no mutate (INV-085 — frozen=False est equivalent : aucun code ne mute ces dataclasses, mutmut le confirme)
class ClusterSnapshot:
    """État du cluster à un instant donné, vu par la logique pure.

    - has_quorum : valeur lue dans /api/cluster (False si < N/2+1 noeuds healthy).
    - patroni_reachable : True si Patroni répond depuis au moins un noeud.
    - timestamp : instant de la mesure.
    """
    has_quorum: bool
    patroni_reachable: bool
    timestamp: datetime


@dataclass(frozen=True)  # pragma: no mutate (INV-085 — frozen=False est equivalent : aucun code ne mute ces dataclasses, mutmut le confirme)
class QuorumState:
    """Résultat de evaluate_quorum_loss.

    - is_lost : True si l'état non-sain a persisté > 3 min sans interruption.
    - lost_since : début de la série non-saine en cours. Non-None SI ET
      SEULEMENT SI is_lost est True. Reste None pendant la fenêtre de grâce
      de 3 min, et redevient None dès que le cluster est sain à nouveau.
    """
    is_lost: bool
    lost_since: Optional[datetime]


def _is_unhealthy(snap: ClusterSnapshot) -> bool:
    """Un snapshot est non-sain si le quorum est perdu OU Patroni injoignable."""
    return (not snap.has_quorum) or (not snap.patroni_reachable)


def evaluate_quorum_loss(
    snapshot: ClusterSnapshot,
    history: list[ClusterSnapshot],
) -> QuorumState:
    """Évalue si le cluster a perdu son quorum (INV-085).

    snapshot : mesure courante.
    history : mesures précédentes (ordre quelconque, recalculé ici). Doit
              couvrir >= 3 min pour que la détection puisse se déclencher.

    L'agrégation du `snapshot` depuis l'ensemble des nœuds du cluster
    (`has_quorum`, `patroni_reachable`) est la **responsabilité de
    l'appelant** (cf issues #80 / #81). Cette fonction pure suppose que
    `snapshot` reflète déjà l'état consolidé : par exemple, pour
    `patroni_reachable`, l'appelant doit avoir vérifié que Patroni est
    injoignable depuis TOUS les nœuds (pas juste un), conformément à la
    spec INV-085 "Patroni injoignable depuis tous les noeuds pendant > 3 min".

    Retour : QuorumState. Voir QuorumState pour la sémantique de lost_since.
    """
    # Cluster sain maintenant → aucune perte en cours, reset complet.
    if not _is_unhealthy(snapshot):
        return QuorumState(is_lost=False, lost_since=None)

    # Remonter la série CONTINUE d'observations non-saines qui se termine au
    # snapshot courant. lost_since = timestamp de la plus ancienne observation
    # de cette série ininterrompue. Une observation saine intercalée coupe la
    # série (anti-flapping : le compteur repart de zéro).
    earlier = sorted(
        (s for s in history if s.timestamp < snapshot.timestamp),
        key=lambda s: s.timestamp,
        reverse=True,
    )
    lost_since = snapshot.timestamp
    for snap in earlier:
        if _is_unhealthy(snap):
            lost_since = snap.timestamp
        else:
            break

    is_lost = (snapshot.timestamp - lost_since) > QUORUM_LOSS_THRESHOLD
    return QuorumState(
        is_lost=is_lost,
        lost_since=lost_since if is_lost else None,
    )
