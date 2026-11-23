"""INV-129 : barres de signal du modem affichees par l'app et le web ; INV-158 : resume par site (en-tete web).

Logique pure : `status` est le dernier rapport de la gateway (dict JSON stocke
dans system_config 'modem_status'), ou None si jamais recu.
"""
from datetime import datetime

# Gateway : un rapport par keepalive (60 s). 3 rapports manques = etat inconnu.
STALE_AFTER_SECONDS = 180
# Plus de 24 h sans rapport : modem debranche (retire ou deplace), plus compte pour son site.
UNPLUGGED_AFTER_SECONDS = 24 * 3600

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


def site_signals(gateways, now: datetime) -> list:
    """Un resume par site, a partir des entrees `gateways` de /api/system/modem-status (triees par site
    puis gateway_id) : barres du meilleur modem (a egalite, le premier), compteur ok/total."""
    sites = {}
    for g in gateways:
        if (now - datetime.fromisoformat(g["updated_at"])).total_seconds() <= UNPLUGGED_AFTER_SECONDS:
            sites.setdefault(g["site"], []).append(g)
    result = []
    for site, gws in sites.items():
        best = max(gws, key=lambda g: g["bars"])
        result.append({"site": site, "state": best["state"], "bars": best["bars"], "tech": best["tech"],
                       "rsrp": best["rsrp"], "ok": sum(g["state"] == "ok" for g in gws), "total": len(gws),
                       "gateways": gws})
    return result
