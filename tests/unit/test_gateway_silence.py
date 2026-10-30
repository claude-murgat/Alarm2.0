"""Unit tests (tier 1) — backend/app/logic/gateway_silence.py (INV-172).

Une passerelle qui n'a plus envoye de report-state depuis 10 min est signalee une fois par
episode de silence ; elle est rearmee des qu'elle rapporte de nouveau.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.gateway_silence import GATEWAY_SILENCE_THRESHOLD, decide_silence

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 30, 12, 0, 0)
NONE = frozenset()


def _gw(gateway_id, silent_for, site="izeron"):
    return {"gateway_id": gateway_id, "site": site, "last_seen": NOW - silent_for}


def test_threshold_is_10_min():
    assert GATEWAY_SILENCE_THRESHOLD == timedelta(minutes=10)


def test_9_min_59_is_not_silent():
    assert decide_silence([_gw("onsite-2", timedelta(minutes=9, seconds=59))], NONE, NOW) == ((), NONE)


@pytest.mark.parametrize("silent_for", [timedelta(minutes=10), timedelta(minutes=10, seconds=1)])
def test_10_min_is_silent_and_alerted(silent_for):
    gw = _gw("onsite-2", silent_for)
    assert decide_silence([gw], NONE, NOW) == ((gw,), frozenset({"onsite-2"}))


def test_already_emailed_is_not_alerted_again():
    gw = _gw("onsite-2", timedelta(minutes=30))
    assert decide_silence([gw], frozenset({"onsite-2"}), NOW) == ((), frozenset({"onsite-2"}))


def test_back_then_silent_again_is_alerted_again():
    emailed = frozenset({"onsite-2"})
    # La passerelle reparle : elle sort de l'ensemble (rearmement)...
    alerts, emailed = decide_silence([_gw("onsite-2", timedelta(seconds=5))], emailed, NOW)
    assert (alerts, emailed) == ((), NONE)
    # ... et un nouveau silence est signale.
    later = NOW + timedelta(minutes=10)
    gw = _gw("onsite-2", timedelta(seconds=5))
    assert decide_silence([gw], emailed, later) == ((gw,), frozenset({"onsite-2"}))


def test_two_gateways_only_the_silent_one_is_alerted():
    silent = _gw("onsite-2", timedelta(minutes=11))
    alive = _gw("alarm-1", timedelta(seconds=4), site="beaufort")
    assert decide_silence([alive, silent], NONE, NOW) == ((silent,), frozenset({"onsite-2"}))


def test_no_gateway_means_nothing():
    assert decide_silence([], frozenset({"disparue"}), NOW) == ((), NONE)
