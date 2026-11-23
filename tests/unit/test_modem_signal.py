"""Unit tests (tier 1) — backend/app/logic/modem_signal.py (INV-129, INV-158 par site).

Barres de signal affichees par l'app et le web a partir du dernier etat
remonte par la gateway. Mesures reelles onsite-2 2026-10-13 : RSRP -116 dBm
(bordure de cellule) -> 1 barre.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.modem_signal import STALE_AFTER_SECONDS, modem_signal, site_signals

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 14, 12, 0, 0)


def _st(age_s=10, **kw):
    base = {"registered": True, "tech": "LTE", "rsrp": -95.0, "csq": 20,
            "reported_at": (NOW - timedelta(seconds=age_s)).isoformat()}
    base.update(kw)
    return base


def test_no_report_is_unknown():
    assert modem_signal(None, NOW) == {"state": "unknown", "bars": 0, "tech": None,
                                       "rsrp": None, "updated_at": None}


def test_fresh_at_exactly_180s():
    assert modem_signal(_st(age_s=180), NOW)["state"] == "ok"


def test_stale_at_181s():
    assert modem_signal(_st(age_s=181), NOW)["state"] == "unknown"


def test_stale_report_is_unknown():
    r = modem_signal(_st(age_s=STALE_AFTER_SECONDS + 1), NOW)
    assert (r["state"], r["bars"]) == ("unknown", 0)


def test_not_registered_is_no_network():
    r = modem_signal(_st(registered=False, tech=None, rsrp=None, csq=0), NOW)
    assert (r["state"], r["bars"]) == ("no_network", 0)


@pytest.mark.parametrize("rsrp,bars", [(-85, 4), (-90, 4), (-91, 3), (-100, 3), (-101, 2),
                                       (-110, 2), (-111, 1), (-116, 1), (-130, 1)])
def test_lte_bars_from_rsrp(rsrp, bars):
    r = modem_signal(_st(rsrp=float(rsrp)), NOW)
    assert (r["state"], r["bars"]) == ("ok", bars)


@pytest.mark.parametrize("csq,bars", [(25, 4), (20, 4), (19, 3), (15, 3), (14, 2),
                                      (10, 2), (9, 1), (0, 1)])
def test_csq_used_when_no_rsrp(csq, bars):
    r = modem_signal(_st(tech="WCDMA", rsrp=None, csq=csq), NOW)
    assert (r["state"], r["bars"]) == ("ok", bars)


def test_exposes_raw_values():
    r = modem_signal(_st(), NOW)
    assert r["tech"] == "LTE" and r["rsrp"] == -95.0 and r["updated_at"]


# ── INV-158 (amende 2026-11-13) : un indicateur par site, meilleur modem + compteur ok/total ──

DAY = 24 * 3600


def _gw(gateway_id, site, age_s=10, **kw):
    """Entree de `gateways` telle que /api/system/modem-status la construit."""
    return {"gateway_id": gateway_id, "site": site, **modem_signal(_st(age_s, **kw), NOW)}


def _summary(sites):
    return [(s["site"], s["state"], s["bars"], s["ok"], s["total"], [g["gateway_id"] for g in s["gateways"]])
            for s in sites]


def test_sites_best_modem_and_counter():
    weak = _gw("alarm-3", "izeron", rsrp=-110.0)        # 2 barres
    silent = _gw("onsite-2", "izeron", age_s=600)        # inconnu depuis 10 min : compte, pas ok
    sites = site_signals([_gw("alarm-1", "beaufort"), weak, silent], NOW)
    assert _summary(sites) == [("beaufort", "ok", 3, 1, 1, ["alarm-1"]),
                               ("izeron", "ok", 2, 1, 2, ["alarm-3", "onsite-2"])]
    assert sites[1]["gateways"] == [weak, silent]
    assert (sites[1]["tech"], sites[1]["rsrp"]) == ("LTE", -110.0)


def test_best_is_the_most_bars_wherever_it_is():
    sites = site_signals([_gw("alarm-1", "izeron", registered=False, tech=None, rsrp=None, csq=0),
                          _gw("alarm-2", "izeron", rsrp=-120.0, tech="LTE"),
                          _gw("alarm-3", "izeron", rsrp=-85.0, tech="LTE-B20")], NOW)
    assert _summary(sites) == [("izeron", "ok", 4, 2, 3, ["alarm-1", "alarm-2", "alarm-3"])]
    assert (sites[0]["tech"], sites[0]["rsrp"]) == ("LTE-B20", -85.0)


def test_tie_takes_the_first_by_gateway_id():
    sites = site_signals([_gw("alarm-1", "izeron", rsrp=-95.0, tech="LTE"),
                          _gw("alarm-2", "izeron", rsrp=-92.0, tech="WCDMA")], NOW)
    assert (sites[0]["bars"], sites[0]["tech"], sites[0]["rsrp"]) == (3, "LTE", -95.0)


def test_all_unknown_site_shows_unknown_zero_ok():
    sites = site_signals([_gw("alarm-1", "beaufort", age_s=181), _gw("alarm-2", "beaufort", age_s=3600)], NOW)
    assert _summary(sites) == [("beaufort", "unknown", 0, 0, 2, ["alarm-1", "alarm-2"])]
    assert (sites[0]["tech"], sites[0]["rsrp"]) == (None, None)


def test_no_network_is_counted_but_not_ok():
    sites = site_signals([_gw("alarm-1", "beaufort", registered=False, tech=None, rsrp=None, csq=0)], NOW)
    assert _summary(sites) == [("beaufort", "no_network", 0, 0, 1, ["alarm-1"])]


def test_counted_at_exactly_24h():
    assert _summary(site_signals([_gw("alarm-2", "beaufort", age_s=DAY)], NOW)) == [
        ("beaufort", "unknown", 0, 0, 1, ["alarm-2"])]


def test_unplugged_after_24h_is_not_counted():
    sites = site_signals([_gw("alarm-1", "beaufort"), _gw("alarm-2", "beaufort", age_s=DAY + 1),
                          _gw("alarm-4", "izeron", age_s=DAY + 1)], NOW)
    assert _summary(sites) == [("beaufort", "ok", 3, 1, 1, ["alarm-1"])]


def test_no_gateway_no_site():
    assert site_signals([], NOW) == []
