"""
Horloge injectable pour les tests.
En prod : datetime.utcnow() classique.
En test : on peut avancer le temps via advance().
"""

from datetime import datetime, timedelta

_offset = timedelta(0)


def now() -> datetime:
    """Retourne l'heure UTC courante + offset de test."""
    return datetime.utcnow() + _offset


def advance(seconds: float):
    """Avance l'horloge de N secondes (cumulatif)."""
    global _offset
    _offset += timedelta(seconds=seconds)


def reset():
    """Remet l'horloge à l'heure réelle."""
    global _offset
    _offset = timedelta(0)


def get_offset_seconds() -> float:
    """Retourne le décalage actuel en secondes."""
    return _offset.total_seconds()
