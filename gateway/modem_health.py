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


# INV-128 : keepalive toutes les 60 s -> 5 controles ≈ 5 min sans reseau avant
# de couper/rallumer la radio (ce qui a debloque onsite-2 le 2026-10-13).
UNREGISTERED_CHECKS_BEFORE_RECOVERY = 5


def parse_network(creg: str, cpsi: str, csq: str) -> dict:
    """Etat reseau a partir des reponses brutes AT+CREG? / AT+CPSI? / AT+CSQ.
    CREG stat 1 = inscrit, 5 = itinerance ; tout le reste = pas de reseau."""
    stat = next((l.split(",")[-1].strip() for l in creg.splitlines() if l.startswith("+CREG:")), "")
    if stat not in ("1", "5"):
        return {"registered": False, "tech": None, "rsrp": None, "csq": 0}
    fields = next((l.split(":", 1)[1].split(",") for l in cpsi.splitlines() if l.startswith("+CPSI:")), [""])
    tech = fields[0].strip() or None
    # CPSI LTE : ...,rsrq,rsrp,rssi,rssnr ; rsrp en 1/10 dBm
    rsrp = round(int(fields[11]) / 10, 1) if tech == "LTE" and len(fields) > 11 else None
    q = next((l.split(":")[1].split(",")[0].strip() for l in csq.splitlines() if l.startswith("+CSQ:")), "99")
    return {"registered": True, "tech": tech, "rsrp": rsrp, "csq": 0 if q == "99" else int(q)}


def reregister_due(unregistered_checks: int, tried: bool) -> bool:
    """Une seule tentative de reinscription par episode sans reseau."""
    return not tried and unregistered_checks >= UNREGISTERED_CHECKS_BEFORE_RECOVERY


def sends_allowed(at_failures: int, registered: bool) -> bool:
    """INV-145 : un SMS/appel n'est tente que modem joignable ET inscrit ; sinon il
    echouerait a coup sur et consommerait un des 3 essais (INV-064)."""
    return at_failures == 0 and registered
