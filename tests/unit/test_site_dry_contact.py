"""Unit tests (tier 1) — backend/app/logic/site_dry_contact.py (INV-195).

Un site qui a deja eu un contact sec lisible (une passerelle vivante `open` ou `closed`) et n'en a plus
depuis plus de 10 min est signale une fois par episode, puis « retabli » au retour d'un contact lisible.
`no_sensor` et passerelle muette ne comptent pas (INV-194). Un site qui n'a jamais eu de contact lisible
n'est jamais signale. Demande du proprietaire du 2026-11-13.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.site_dry_contact import SITE_DRY_CONTACT_LOST_AFTER, decide_site_dry_contact

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 12, 0, 0)
MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)
LIVENESS = timedelta(seconds=15)
READABLE = {"since": None, "emailed": False}


def _run(gateways, state=None, now=NOW, sites=("izeron", "beaufort")):
    return decide_site_dry_contact(list(sites), gateways, state or {}, now, LIVENESS)


def _lost_since(since, emailed=False):
    return {"since": since, "emailed": emailed}


def test_threshold_is_10_min():
    assert SITE_DRY_CONTACT_LOST_AFTER == timedelta(minutes=10)


@pytest.mark.parametrize("state", ["open", "closed"])
def test_alive_gateway_with_a_sensor_makes_the_site_readable_and_tracked(state):
    assert _run([("beaufort", state, NOW - 14 * SEC)], sites=["beaufort"]) == ((), (), {"beaufort": READABLE})


def test_readable_site_stays_readable_without_email():
    state = {"beaufort": READABLE}
    assert _run([("beaufort", "closed", NOW)], state, sites=["beaufort"]) == ((), (), state)


def test_one_gateway_with_a_sensor_is_enough_whatever_the_others_report():
    gateways = [("beaufort", "no_sensor", NOW), ("beaufort", "closed", NOW - 30 * MIN),
                ("beaufort", "open", NOW - 2 * SEC)]
    assert _run(gateways, {"beaufort": _lost_since(NOW - 30 * MIN, True)}, sites=["beaufort"]) == (
        (), (("beaufort", NOW - 30 * MIN),), {"beaufort": READABLE})


@pytest.mark.parametrize("gateways", [
    [("beaufort", "no_sensor", NOW)],  # Arduino debranche, fige ou non programme
    [("beaufort", "closed", NOW - LIVENESS)],  # passerelle muette : exactement 15 s, plus vivante
    [("beaufort", "open", NOW - 3 * MIN), ("beaufort", "no_sensor", NOW - SEC)],
    [],  # plus aucune passerelle connue sur le site
], ids=["no_sensor", "silent_15s", "silent_and_no_sensor", "none"])
def test_no_sensor_and_silent_gateways_leave_a_tracked_site_without_readable_contact(gateways):
    assert _run(gateways, {"beaufort": READABLE}, sites=["beaufort"]) == ((), (), {"beaufort": _lost_since(NOW)})


def test_9_59_and_10_00_nothing_10_01_email_then_no_duplicate():
    state = {"beaufort": _lost_since(NOW)}
    gateways = [("beaufort", "no_sensor", NOW)]
    for elapsed in (9 * MIN + 59 * SEC, 10 * MIN):
        assert _run(gateways, state, now=NOW + elapsed, sites=["beaufort"]) == ((), (), state)
    alerts = _run(gateways, state, now=NOW + 10 * MIN + SEC, sites=["beaufort"])
    emailed = {"beaufort": _lost_since(NOW, True)}
    assert alerts == ((("beaufort", NOW),), (), emailed)
    assert _run(gateways, emailed, now=NOW + 3 * 60 * MIN, sites=["beaufort"]) == ((), (), emailed)


def test_recovery_sends_retabli_rearms_and_a_new_loss_is_emailed_again():
    emailed = {"beaufort": _lost_since(NOW - 40 * MIN, True)}
    assert _run([("beaufort", "closed", NOW)], emailed, sites=["beaufort"]) == (
        (), (("beaufort", NOW - 40 * MIN),), {"beaufort": READABLE})
    later = NOW + MIN
    assert _run([("beaufort", "no_sensor", later)], {"beaufort": READABLE}, now=later, sites=["beaufort"]) == (
        (), (), {"beaufort": _lost_since(later)})
    assert _run([("beaufort", "no_sensor", later)], {"beaufort": _lost_since(later)},
                now=later + 11 * MIN, sites=["beaufort"]) == (
        (("beaufort", later),), (), {"beaufort": _lost_since(later, True)})


def test_short_episode_ends_without_any_email():
    assert _run([("beaufort", "open", NOW)], {"beaufort": _lost_since(NOW - 9 * MIN)}, sites=["beaufort"]) == (
        (), (), {"beaufort": READABLE})


def test_site_that_never_had_a_readable_contact_never_alerts():
    for now in (NOW, NOW + 11 * MIN, NOW + 30 * 24 * 60 * MIN):
        assert _run([("izeron", "no_sensor", now)], now=now, sites=["izeron"]) == ((), (), {})
    assert _run([], sites=["izeron"]) == ((), (), {})


def test_stale_closed_row_tracks_the_site_and_alerts_10_min_after_the_first_check():
    gateways = [("beaufort", "closed", NOW - 2 * 60 * MIN)]  # passerelle avec Arduino muette avant le deploiement
    first = _run(gateways, sites=["beaufort"])
    assert first == ((), (), {"beaufort": _lost_since(NOW)})
    assert _run(gateways, first[2], now=NOW + 10 * MIN + SEC, sites=["beaufort"]) == (
        (("beaufort", NOW),), (), {"beaufort": _lost_since(NOW, True)})


def test_two_sites_are_independent():
    gateways = [("izeron", "closed", NOW), ("beaufort", "no_sensor", NOW)]
    state = {"izeron": READABLE, "beaufort": _lost_since(NOW - 11 * MIN)}
    assert _run(gateways, state) == (
        (("beaufort", NOW - 11 * MIN),), (), {"izeron": READABLE, "beaufort": _lost_since(NOW - 11 * MIN, True)})
    state = {"izeron": _lost_since(NOW - 20 * MIN, True), "beaufort": _lost_since(NOW - 11 * MIN, True)}
    gateways = [("izeron", "open", NOW), ("beaufort", "closed", NOW - MIN)]
    assert _run(gateways, state) == (
        (), (("izeron", NOW - 20 * MIN),), {"izeron": READABLE, "beaufort": _lost_since(NOW - 11 * MIN, True)})


def test_untracked_site_does_not_hide_the_next_one():
    state = {"beaufort": _lost_since(NOW - 11 * MIN)}
    assert _run([("izeron", "no_sensor", NOW)], state) == (
        (("beaufort", NOW - 11 * MIN),), (), {"beaufort": _lost_since(NOW - 11 * MIN, True)})


def test_sensor_of_another_site_does_not_count():
    assert _run([("izeron", "closed", NOW)], {"beaufort": READABLE}, sites=["beaufort"]) == (
        (), (), {"beaufort": _lost_since(NOW)})


def test_site_removed_from_known_sites_is_forgotten_without_email():
    assert _run([("test", "no_sensor", NOW)], {"test": _lost_since(NOW - 11 * MIN)}, sites=["beaufort"]) == (
        (), (), {})
