"""Logique pure : passerelle muette (INV-172).

Une passerelle poste son etat toutes les 5 s (report-state, INV-120). Sans rapport depuis 10 min,
elle (ou son noeud) est a l'arret : son site ne peut plus declencher par contact sec ni emettre de
SMS/appels. L'appelant (`quorum_monitor`, leader seul) garde `emailed` (passerelles muettes deja
signalees) dans SystemConfig et envoie un email par passerelle renvoyee dans `alerts`.
"""
from datetime import datetime, timedelta

GATEWAY_SILENCE_THRESHOLD = timedelta(minutes=10)
# Au-dela, la ligne est celle d'une passerelle renommee ou retiree (ou d'un test) : ignoree.
GATEWAY_RETIRED_AFTER = timedelta(days=7)


def decide_silence(gateways: list[dict], emailed: frozenset, now: datetime) -> tuple[tuple, frozenset]:
    """`gateways` : lignes gateway_states ({gateway_id, site, last_seen}). Renvoie (passerelles a
    signaler ce tick, nouvel ensemble `emailed`) : une passerelle n'est signalee qu'une fois par
    episode de silence et sort de `emailed` des qu'elle rapporte de nouveau (rearmement) ou que
    son silence depasse 7 jours (retiree)."""
    silent = [g for g in gateways
              if GATEWAY_SILENCE_THRESHOLD <= now - g["last_seen"] <= GATEWAY_RETIRED_AFTER]
    alerts = tuple(g for g in silent if g["gateway_id"] not in emailed)
    return alerts, frozenset(g["gateway_id"] for g in silent)
