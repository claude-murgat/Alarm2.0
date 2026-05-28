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


# Fenêtres reminders anti-spam INV-085 (sous-cas 3/3) — tranché 2026-04-20.
# Après l'email initial, ré-alerter à 1h, 3h, 6h. Aucun reminder au-delà : à
# 6h+, on suppose que l'opérateur est conscient (ou que l'incident est mort
# de lui-même). Liste ordonnée croissante : la logique reposera sur ça.
REMINDER_WINDOWS = (
    timedelta(hours=1),
    timedelta(hours=3),
    timedelta(hours=6),
)


def should_send_initial_email(
    state: QuorumState,
    email_sent_at: Optional[datetime],
) -> bool:
    """INV-085 sous-cas 2/3 (#80) : décide si on doit envoyer le 1er email
    d'alerte direction technique sur cet incident de perte de quorum.

    Règle : envoyer SI le quorum est déclaré perdu (`is_lost=True`, ce qui
    suppose déjà la fenêtre anti-flapping > 3 min franchie) ET aucun email
    n'a encore été envoyé pour cet incident (`email_sent_at is None`).

    state : sortie de `evaluate_quorum_loss` sur le tick courant.
    email_sent_at : timestamp 1er email pour CET incident (None = pas envoyé).
      Persisté dans `quorum_state.email_sent_at`, reset à NULL au retour à sain.
    """
    return state.is_lost and email_sent_at is None


def should_send_reminder(
    state: QuorumState,
    email_sent_at: Optional[datetime],
    reminders_sent_at: list[timedelta],
    now: datetime,
) -> Optional[timedelta]:
    """INV-085 sous-cas 3/3 (#81) : décide si un reminder anti-spam est dû.

    Règle : un reminder pour la fenêtre W ∈ {1h, 3h, 6h} est dû SI :
    - quorum toujours perdu (`state.is_lost is True`), ET
    - email initial déjà envoyé (`email_sent_at is not None`), ET
    - `now - email_sent_at >= W` (borne inclusive), ET
    - le reminder pour cette fenêtre n'a pas déjà été envoyé.

    Retourne la PLUS GRANDE fenêtre due qui n'a pas encore été envoyée. Aucun
    reminder au-delà de 6h. Si rien à envoyer → None.

    state : sortie de `evaluate_quorum_loss` sur le tick courant.
    email_sent_at : timestamp 1er email (None = pas encore envoyé → None).
    reminders_sent_at : liste des fenêtres déjà envoyées (subset de
      `REMINDER_WINDOWS`). Persisté en JSON dans `quorum_state.reminders_sent_at`.
    now : timestamp du tick courant.
    """
    if not state.is_lost:
        return None
    if email_sent_at is None:
        return None
    elapsed = now - email_sent_at
    # Parcours du plus grand au plus petit : on retourne la fenêtre la plus
    # avancée qui est due ET pas encore envoyée. Garantit qu'à T0+3h05 avec
    # 1h envoyé, on retourne 3h (et pas 1h à tort).
    for window in reversed(REMINDER_WINDOWS):
        if elapsed >= window and window not in reminders_sent_at:
            return window
    return None
