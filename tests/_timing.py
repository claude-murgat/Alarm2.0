"""Attente de la boucle d'escalade backend, calee sur son tick reel (INV-084).

La periode de la boucle est `escalation_tick_seconds` (SystemConfig, defaut
10 s). En CI le tier 3 la regle a 1 s : attendre un tick prend ~2,5 s au lieu
de 12 s. Lue une fois par session ; le reset de test ne la modifie pas.
"""
import os
import time

import requests

_tick = None


def escalation_tick_seconds() -> float:
    global _tick
    if _tick is None:
        url = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
        try:
            value = requests.get(f"{url}/api/config/system", timeout=5).json().get("escalation_tick_seconds")
            _tick = float(value) if value else 10.0
        except Exception:
            _tick = 10.0  # backend du defaut : jamais plus court que l'ancien sleep(12)
    return _tick


def wait_escalation_ticks(n: int = 1) -> None:
    """Bloque le temps de `n` ticks complets apres l'appel.

    Marge : un tick deja commence au moment de l'appel peut avoir lu l'etat
    avant l'action du test -> on laisse le temps d'un tick de plus (borne 1,5 s
    a tick=1 s, 2 s a tick=10 s comme les anciens sleep(12)).
    """
    tick = escalation_tick_seconds()
    time.sleep(n * tick + max(1.5, 0.2 * tick))
