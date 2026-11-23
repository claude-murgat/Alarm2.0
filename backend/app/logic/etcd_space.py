"""Logique pure INV-196 : base etcd d'un voteur a 1 Gio ou plus, ou alarme NOSPACE.

etcd refuse toute ecriture quand son fichier atteint le quota (2 Gio par defaut) : Patroni ne tient plus
son leader, plus rien n'ecrit dans le cluster. Un email par episode et par niveau ; rearme quand tous les
voteurs repondent sains. Fetchs, email, audit et etat (SystemConfig) : quorum_monitor._run_etcd_space_check.
"""
from typing import Optional

ETCD_DB_SIZE_ALERT_BYTES = 1024 ** 3  # la moitie du quota par defaut d'etcd (--quota-backend-bytes non fixe)
_LEVELS = (None, "size", "nospace")  # du moins grave au plus grave


def etcd_level(status: dict) -> Optional[str]:
    """`status` : reponse de POST /v3/maintenance/status. dbSize (taille du fichier) : c'est elle que le
    quota compare, pas dbSizeInUse."""
    if any("NOSPACE" in e for e in status.get("errors", ())):
        return "nospace"
    if int(status["dbSize"]) >= ETCD_DB_SIZE_ALERT_BYTES:
        return "size"
    return None


def decide_etcd_space(statuses: list, alerted: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """`statuses` : un status par voteur, None si injoignable ; `alerted` : niveau deja signale.
    Renvoie (niveau a signaler ou None, nouvel etat)."""
    level = max((etcd_level(s) for s in statuses if s is not None), key=_LEVELS.index, default=None)
    if level is None and None in statuses:
        return None, alerted  # un voteur muet est peut-etre encore au-dessus : pas de rearmement
    return (level if _LEVELS.index(level) > _LEVELS.index(alerted) else None), level
