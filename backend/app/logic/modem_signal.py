"""INV-129 : barres de signal du modem affichees par l'app et le web.

Logique pure : `status` est le dernier rapport de la gateway (dict JSON stocke
dans system_config 'modem_status'), ou None si jamais recu.
"""
from datetime import datetime

# Gateway : un rapport par keepalive (60 s). 3 rapports manques = etat inconnu.
STALE_AFTER_SECONDS = 180

_RSRP_BARS = ((-90, 4), (-100, 3), (-110, 2))   # dBm, LTE
_CSQ_BARS = ((20, 4), (15, 3), (10, 2))          # 0-31, toutes technos


def _bars(value, thresholds) -> int:
    return next((b for limit, b in thresholds if value >= limit), 1)


def modem_signal(status, now: datetime) -> dict:
    if not status or (now - datetime.fromisoformat(status["reported_at"])).total_seconds() > STALE_AFTER_SECONDS:
        return {"state": "unknown", "bars": 0, "tech": None, "rsrp": None,
                "updated_at": status["reported_at"] if status else None}
    if not status["registered"]:
        bars, state = 0, "no_network"
    elif status.get("rsrp") is not None:
        bars, state = _bars(status["rsrp"], _RSRP_BARS), "ok"
    else:
        bars, state = _bars(status["csq"], _CSQ_BARS), "ok"
    return {"state": state, "bars": bars, "tech": status.get("tech"),
            "rsrp": status.get("rsrp"), "updated_at": status["reported_at"]}
