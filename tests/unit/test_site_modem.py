"""Unit tests (tier 1) — backend/app/logic/site_modem.py (INV-185).

Un site dont aucun modem n'a envoye d'etat (modem-status) depuis 10 min, ou n'en a envoye aucun
depuis le debut de l'episode, est signale une fois par episode ; un etat plus recent que le debut
de l'episode le rearme. Revue PR #296 (F1) : un modem absent des le demarrage n'alertait personne.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.site_modem import SITE_MODEM_SILENCE, decide_site_modem

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 5, 12, 0, 0)
SITES = ["izeron", "beaufort"]
MIN = timedelta(minutes=1)


def _run(reports, state=None, now=NOW, sites=("beaufort",)):
    return decide_site_modem(list(sites), reports, state or {}, now)


def test_threshold_is_10_min():
    assert SITE_MODEM_SILENCE == timedelta(minutes=10)


def test_fresh_modem_is_fine_and_leaves_no_episode():
    assert _run([("beaufort", NOW - 9 * MIN - timedelta(seconds=59))]) == ((), {})


@pytest.mark.parametrize("age", [10 * MIN, 10 * MIN + timedelta(seconds=1), timedelta(days=3)])
def test_stale_modem_is_alerted_at_once(age):
    last = NOW - age
    assert _run([("beaufort", last)]) == (
        (("beaufort", last, last),), {"beaufort": {"since": last, "emailed": True}})


def test_one_email_per_episode():
    last = NOW - 30 * MIN
    state = {"beaufort": {"since": last, "emailed": True}}
    assert _run([("beaufort", last)], state) == ((), state)


def test_site_without_any_report_is_alerted_10_min_after_the_episode_started():
    alerts, state = _run([])
    assert alerts == () and state == {"beaufort": {"since": NOW, "emailed": False}}
    assert _run([], state, now=NOW + 9 * MIN) == ((), state)
    assert _run([], state, now=NOW + 10 * MIN) == (
        (("beaufort", NOW, None),), {"beaufort": {"since": NOW, "emailed": True}})


def test_newer_report_ends_the_episode_and_a_new_silence_is_alerted_again():
    emailed = {"beaufort": {"since": NOW - 40 * MIN, "emailed": True}}
    back = NOW - 15 * MIN  # un modem a parle apres le debut de l'episode, puis s'est tu (ticks manques)
    assert _run([("beaufort", back)], emailed) == (
        (("beaufort", back, back),), {"beaufort": {"since": back, "emailed": True}})
    assert _run([("beaufort", NOW - MIN)], emailed) == ((), {})


def test_report_older_than_the_episode_keeps_it():
    # Episode ouvert sans aucun etat il y a 5 min ; un etat plus ancien ne le clot pas.
    state = {"beaufort": {"since": NOW - 5 * MIN, "emailed": False}}
    assert _run([("beaufort", NOW - 20 * MIN)], state) == ((), state)


def test_freshest_report_of_the_site_counts():
    # Modem deplace d'alarm-1 a alarm-2 (meme site) : l'etat perime d'alarm-1 ne compte pas.
    reports = [("beaufort", NOW - timedelta(days=2)), ("beaufort", NOW - MIN), ("beaufort", NOW - timedelta(days=1))]
    assert _run(reports) == ((), {})
    reports = [("beaufort", NOW - 20 * MIN), ("beaufort", NOW - 12 * MIN), ("beaufort", NOW - 30 * MIN)]
    last = NOW - 12 * MIN
    assert _run(reports) == ((("beaufort", last, last),), {"beaufort": {"since": last, "emailed": True}})


def test_sites_are_independent_and_unknown_sites_ignored():
    alerts, state = _run([("izeron", NOW - MIN), ("lyon", NOW - MIN)], sites=SITES)
    assert alerts == () and state == {"beaufort": {"since": NOW, "emailed": False}}
