"""Logique pure de sante du lien serie gateway <-> modem SIM7600.

Aucun import : testable en tier 1 sans pyserial (cf tests/unit/test_gateway_modem_health.py).
`send_at_command` ne leve jamais d'exception et renvoie "" quand le port serie
est mort (modem re-enumere sur l'USB) : ces fonctions ne doivent donc jamais
traiter une reponse vide comme un succes.
"""

# Keepalive AT toutes les 60s : 2 echecs consecutifs ≈ 2 min sans modem avant
# de sortir (un echec isole peut venir d'un modem momentanement occupe).
MAX_AT_FAILURES = 2


def call_was_dialed(atd_response: str) -> bool:
    """Le SIM7600 repond OK a `ATD<numero>;`. Toute autre reponse, y compris
    vide, signifie que l'appel n'est jamais parti."""
    return "OK" in atd_response.splitlines()


def at_failures_after(at_response: str, failures: int) -> int:
    """Compteur d'echecs consecutifs du keepalive apres une reponse a `AT`."""
    return 0 if "OK" in at_response.splitlines() else failures + 1
