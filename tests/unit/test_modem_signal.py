"""Unit tests (tier 1) — backend/app/logic/modem_signal.py (INV-129).

Barres de signal affichees par l'app et le web a partir du dernier etat
remonte par la gateway. Mesures reelles onsite-2 2026-10-13 : RSRP -116 dBm
(bordure de cellule) -> 1 barre.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.modem_signal import STALE_AFTER_SECONDS, modem_signal

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
