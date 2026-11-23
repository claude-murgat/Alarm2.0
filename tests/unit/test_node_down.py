"""Unit tests (tier 1) — backend/app/logic/node_down.py (INV-189).

Un noeud du cluster absent de /cluster, arrete, ou qui ne suit plus le leader (pas `streaming` sur la
timeline du leader) depuis plus de 10 min est signale une fois par episode ; un tick sain clot
l'episode, avec « retabli » s'il avait ete signale. Incident du 2026-11-09 : alarm-2 (node5, Beaufort)
sans cable reseau ~45 min, personne prevenu.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.node_down import NODE_DOWN_AFTER, decide_node_down

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 9, 8, 0, 0)
MIN = timedelta(minutes=1)
SEC = timedelta(seconds=1)
NODES = [f"node{i}" for i in range(1, 8)]  # node_locations par defaut (INV-161)
ABSENT = "absent de /cluster"


def _m(n, state="streaming", timeline=290, role="replica"):
    return {"name": f"node{n}", "role": role, "state": state, "timeline": timeline, "lag": 0}


def _cluster(*special, absent=()):
    """node2 leader `running` timeline 290, les autres `streaming` sur 290 ; `special` remplace un membre."""
    given = {m["name"]: m for m in special}
    members = [given.get(f"node{i}", _m(i, "running", role="leader") if i == 2 else _m(i))
               for i in range(1, 8) if f"node{i}" not in absent]
    return {"members": members}


def _run(cluster, state=None, now=T0, expected=NODES):
    return decide_node_down(cluster, list(expected), {} if state is None else state, now)


def _open(since=T0, emailed=False, name="node5"):
    return {name: {"since": since, "emailed": emailed}}


def test_threshold_is_10_min():
    assert NODE_DOWN_AFTER == timedelta(minutes=10)


def test_healthy_cluster_has_no_episode():
    # Leader `running`, replicas `streaming` sur sa timeline (lag ignore).
    assert _run(_cluster(_m(4, timeline=290) | {"lag": 64 * 2**20})) == ((), (), {})


@pytest.mark.parametrize("cluster", [
    _cluster(_m(5, timeline=None)),
    {"members": [_m(2, "running", timeline=None, role="leader"), _m(5, timeline=288)]},
])
def test_missing_timeline_only_the_state_counts(cluster):
    # Comme INV-168 : `timeline` est optionnelle dans /cluster.
    assert _run(cluster, expected=["node2", "node5"]) == ((), (), {})


def test_first_unhealthy_tick_opens_the_episode_without_email():
    assert _run(_cluster(absent={"node5"})) == ((), (), _open())


@pytest.mark.parametrize("elapsed", [9 * MIN + 59 * SEC, 10 * MIN])
def test_not_more_than_10_min_sends_nothing(elapsed):
    assert _run(_cluster(absent={"node5"}), _open(), now=T0 + elapsed) == ((), (), _open())


@pytest.mark.parametrize("elapsed", [10 * MIN + SEC, 10 * MIN + MIN, timedelta(hours=3)])
def test_more_than_10_min_sends_one_email(elapsed):
    assert _run(_cluster(absent={"node5"}), _open(), now=T0 + elapsed) == (
        (("node5", T0, ABSENT),), (), _open(emailed=True))


def test_one_email_per_episode():
    state = _open(emailed=True)
    assert _run(_cluster(absent={"node5"}), state, now=T0 + 45 * MIN) == ((), (), state)


def test_recovery_is_reported_once_and_rearms():
    assert _run(_cluster(), _open(emailed=True), now=T0 + 45 * MIN) == ((), (("node5", T0),), {})
    # Nouvelle coupure : nouvel episode, nouveau delai.
    later = T0 + 50 * MIN
    assert _run(_cluster(absent={"node5"}), {}, now=later) == ((), (), _open(since=later))


def test_episode_under_10_min_closes_silently():
    assert _run(_cluster(), _open(), now=T0 + 9 * MIN) == ((), (), {})


@pytest.mark.parametrize("member, seen", [
    (None, ABSENT),
    (_m(5, "stopped", timeline=None), "état stopped"),  # timeline absente : seul l'etat compte
    (_m(5, "stopped"), "état stopped"),
    (_m(5, "starting"), "état starting"),
    (_m(5, "unknown"), "état unknown"),
    (_m(5, "running"), "état running"),  # replica `running` : ne suit pas le leader
    (_m(5, "running", timeline=289), "running sur la timeline 289 (leader : 290)"),  # ancienne timeline
    (_m(5, "streaming", timeline=288), "streaming sur la timeline 288 (leader : 290)"),
])
def test_what_the_leader_saw_is_reported(member, seen):
    cluster = _cluster(absent={"node5"}) if member is None else _cluster(member)
    assert _run(cluster, _open(), now=T0 + 11 * MIN) == ((("node5", T0, seen),), (), _open(emailed=True))


def test_two_nodes_each_with_their_own_timing():
    cluster = _cluster(_m(4, "running", timeline=289), absent={"node5"})
    _, _, state = _run(cluster, _open(), now=T0 + 5 * MIN)  # node4 decroche 5 min apres node5
    assert state == {"node4": {"since": T0 + 5 * MIN, "emailed": False}, "node5": {"since": T0, "emailed": False}}
    down, recovered, state = _run(cluster, state, now=T0 + 11 * MIN)
    assert down == (("node5", T0, ABSENT),) and recovered == ()
    assert state == {"node4": {"since": T0 + 5 * MIN, "emailed": False}, "node5": {"since": T0, "emailed": True}}
    down, recovered, state = _run(cluster, state, now=T0 + 15 * MIN + SEC)
    assert down == (("node4", T0 + 5 * MIN, "running sur la timeline 289 (leader : 290)"),) and recovered == ()
    assert state == {"node4": {"since": T0 + 5 * MIN, "emailed": True}, "node5": {"since": T0, "emailed": True}}


def test_two_nodes_due_at_the_same_tick_get_one_email_each():
    cluster = _cluster(_m(7, "stopped"), absent={"node5"})
    state = {"node7": {"since": T0, "emailed": False}, "node5": {"since": T0, "emailed": False}}
    down, recovered, _ = _run(cluster, state, now=T0 + 11 * MIN)
    assert down == (("node5", T0, ABSENT), ("node7", T0, "état stopped")) and recovered == ()


def test_flapping_node_under_10_min_is_never_alerted():
    state, sent = {}, []
    for minute in range(30):
        down_now = minute % 10 != 9  # sain une minute sur dix : jamais 10 min d'affilee
        cluster = _cluster(absent={"node5"}) if down_now else _cluster()
        down, recovered, state = _run(cluster, state, now=T0 + minute * MIN)
        sent += down + recovered
    assert sent == []


@pytest.mark.parametrize("cluster", [
    {},
    {"members": []},
    {"members": [_m(1), _m(3)]},  # aucun leader liste
    _cluster(_m(2, "starting", role="leader"), absent={"node5"}),  # leader pas `running`
])
def test_no_running_leader_leaves_the_state_untouched(cluster):
    state = _open()
    assert _run(cluster, state, now=T0 + 30 * MIN) == ((), (), state)


def test_retiring_node_is_checked_like_the_others():
    # onsite-1 (node1, « Legacy ») debranche avant le demenagement : signale.
    assert _run(_cluster(absent={"node1"}), _open(name="node1"), now=T0 + 11 * MIN) == (
        (("node1", T0, ABSENT),), (), _open(name="node1", emailed=True))


def test_node_removed_from_node_locations_and_absent_is_forgotten_silently():
    expected = [n for n in NODES if n != "node1"]  # ote de node_locations le jour du demenagement
    assert _run(_cluster(absent={"node1"}), _open(name="node1", emailed=True),
                now=T0 + 11 * MIN, expected=expected) == ((), (), {})


def test_member_listed_by_patroni_but_not_in_node_locations_is_checked():
    cluster = _cluster()
    cluster["members"].append({"name": "node9", "role": "replica", "state": "stopped", "timeline": 290})
    assert _run(cluster, _open(name="node9"), now=T0 + 11 * MIN) == (
        (("node9", T0, "état stopped"),), (), _open(name="node9", emailed=True))
