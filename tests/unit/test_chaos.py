"""Unit tests (tier 1) — backend/app/logic/chaos.py (INV-176, INV-177).

Garde-fous de la panne controlee, dans l'ordre du catalogue : minutes 1..10 ; cible connue ;
jamais >= 2 voteurs etcd (Izeron entier) ; netcut sur node3 ; aucune alarme en cours ; un seul
test a la fois ; 5 membres Patroni sains (leader running, replicas streaming). Voteurs = `etcd_voters`
(topologie, INV-175), passes en argument. Expiration `is_stale` (INV-176).
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.chaos import check_request, expand_target, is_stale

pytestmark = pytest.mark.unit

LOCATIONS = {"node1": "izeron", "node2": "izeron", "node3": "ovh", "node4": "beaufort", "node5": "beaufort"}


def _m(name, role="replica", state="streaming"):
    return {"name": name, "role": role, "state": state, "host": f"10.99.0.{name[-1]}"}


def _cluster(*members):
    return {"members": list(members)}


HEALTHY = _cluster(_m("node1"), _m("node2"), _m("node3", "leader", "running"), _m("node4"), _m("node5"))
IZERON = "node1, node2 : quorum etcd perdu — refusé"
VOTERS = ("node1", "node2", "node3")  # defaut d'etcd_voters


def check(nodes=("node4",), mode="stop", minutes=2, cluster=HEALTHY, locations=LOCATIONS, voters=VOTERS,
          active_alarms=0, pending=0):
    return check_request(list(nodes), mode, minutes, cluster, locations, voters, active_alarms, pending)


def test_voters_come_from_the_topology_not_a_constant():
    loc6 = {**LOCATIONS, "node6": "ovh2"}
    six = _cluster(*HEALTHY["members"], _m("node6"))
    four_voters = ("node1", "node2", "node3", "node6")  # INV-175 : 4 voteurs, majorite 3
    assert check(nodes=("node2", "node6"), locations=loc6, cluster=six, voters=four_voters) == \
        "node2, node6 : quorum etcd perdu — refusé"
    assert check(nodes=("node6",), locations=loc6, cluster=six, voters=four_voters) is None
    # 2e voteur cloud a la place de node2 : Izeron entier ne contient plus qu'un voteur
    assert check(nodes=("node1", "node2"), locations=loc6, cluster=six, voters=("node1", "node3", "node6")) is None


def test_healthy_cluster_single_node_accepted():
    assert check() is None
    assert check(nodes=("node4", "node5")) is None
    assert check(nodes=("node3",)) is None  # OVH arrete : un seul voteur
    assert check(nodes=("node1",)) is None
    assert check(minutes=1) is None
    assert check(minutes=10) is None


@pytest.mark.parametrize("minutes", [0, 11, -3])
def test_minutes_out_of_1_10_refused(minutes):
    assert check(minutes=minutes) == "durée : entre 1 et 10 minutes"


def test_empty_target_refused():
    assert check(nodes=()) == "aucune cible"


def test_unknown_node_refused_with_its_name():
    assert check(nodes=("node4", "node9")) == "nœud inconnu : node9"
    assert check(nodes=("site:mars",)) == "nœud inconnu : site:mars"


def test_two_voters_refused_whole_izeron():
    assert check(nodes=("node1", "node2")) == IZERON
    assert check(nodes=("node2", "node1")) == "node2, node1 : quorum etcd perdu — refusé"
    assert check(nodes=("node1", "node3")) == "node1, node3 : quorum etcd perdu — refusé"
    assert check(nodes=("node4", "node2", "node3")) == "node2, node3 : quorum etcd perdu — refusé"
    assert check(nodes=("node1", "node2", "node3")) == "node1, node2, node3 : quorum etcd perdu — refusé"


def test_netcut_on_node3_refused_but_netcut_elsewhere_and_stop_node3_accepted():
    assert check(nodes=("node3",), mode="netcut") == "coupure réseau de node3 refusée : le runner et l'interface web y vivent"
    assert check(nodes=("node4", "node3"), mode="netcut").startswith("coupure réseau de node3 refusée")
    assert check(nodes=("node4",), mode="netcut") is None
    assert check(nodes=("node3",), mode="stop") is None


def test_active_alarm_refused():
    assert check(active_alarms=1) == "une alarme est en cours (active, escaladée ou acquittée)"
    assert check(active_alarms=3) == "une alarme est en cours (active, escaladée ou acquittée)"


def test_pending_test_refused():
    assert check(pending=1) == "un test est déjà en cours"


def test_cluster_unreachable_refused():
    assert check(cluster=None) == "Patroni injoignable"


def test_member_missing_or_extra_refused():
    four = _cluster(_m("node1"), _m("node2"), _m("node3", "leader", "running"), _m("node4"))
    assert check(cluster=four) == "membre absent du cluster Patroni : node5"
    six = _cluster(*HEALTHY["members"], _m("node9"))
    assert check(cluster=six) == "6 membres Patroni au lieu de 5"
    swapped = _cluster(_m("node1"), _m("node2"), _m("node3", "leader", "running"), _m("node4"), _m("node9"))
    assert check(cluster=swapped) == "membre absent du cluster Patroni : node5"


def test_leader_missing_or_not_running_refused():
    no_leader = _cluster(_m("node1"), _m("node2"), _m("node3"), _m("node4"), _m("node5"))
    assert check(cluster=no_leader) == "aucun leader Patroni"
    starting = _cluster(_m("node1"), _m("node2"), _m("node3", "leader", "starting"), _m("node4"), _m("node5"))
    assert check(cluster=starting) == "leader node3 pas running (starting)"


def test_replica_not_streaming_refused_sync_standby_streaming_accepted():
    stuck = _cluster(_m("node1"), _m("node2"), _m("node3", "leader", "running"), _m("node4", state="running"), _m("node5"))
    assert check(cluster=stuck) == "node4 pas streaming (running)"
    sync = _cluster(_m("node1", "sync_standby"), _m("node2"), _m("node3", "leader", "running"), _m("node4"), _m("node5"))
    assert check(cluster=sync) is None


def test_refusals_follow_the_catalogue_order():
    # minutes avant la cible, la cible avant le quorum, le quorum avant l'alarme, l'alarme avant le test
    # en cours, le test en cours avant le cluster.
    assert check(nodes=("node1", "node2"), minutes=0) == "durée : entre 1 et 10 minutes"
    assert check(nodes=("node1", "node2", "node9"), active_alarms=1) == "nœud inconnu : node9"
    assert check(nodes=("node1", "node2"), active_alarms=1) == IZERON
    assert check(nodes=("node3",), mode="netcut", active_alarms=1).startswith("coupure réseau")
    assert check(active_alarms=1, pending=1) == "une alarme est en cours (active, escaladée ou acquittée)"
    assert check(pending=1, cluster=None) == "un test est déjà en cours"


def test_expand_target_nodes_sites_and_unknown():
    assert expand_target("node4", LOCATIONS) == ["node4"]
    assert expand_target("site:beaufort", LOCATIONS) == ["node4", "node5"]
    assert expand_target("site:izeron", LOCATIONS) == ["node1", "node2"]
    assert expand_target("node5, site:beaufort ,node4", LOCATIONS) == ["node5", "node4"]  # sans doublon, ordre garde
    assert expand_target("site:mars", LOCATIONS) == ["site:mars"]  # garde le nom : refuse comme noeud inconnu
    assert expand_target("", LOCATIONS) == []
    assert expand_target(",node4", LOCATIONS) == ["node4"]  # virgule en tete
    assert expand_target(" , ", LOCATIONS) == []


NOW = datetime(2026, 10, 30, 12, 0, 0)


def stale(status, minutes=5, created=NOW - timedelta(hours=2), claimed=None, started=None):
    return is_stale(status, minutes, created, claimed, started, NOW)


def test_pending_expires_30_minutes_after_creation():
    assert not stale("pending", created=NOW - timedelta(minutes=30))
    assert stale("pending", created=NOW - timedelta(minutes=30, seconds=1))
    assert not stale("pending", minutes=10, created=NOW - timedelta(minutes=29))


def test_claimed_or_running_expires_minutes_plus_10_after_start_not_creation():
    # demande creee il y a 2 h, reclamee tard (verrou CD) : son age ne compte plus
    assert not stale("claimed", claimed=NOW - timedelta(minutes=15))
    assert stale("claimed", claimed=NOW - timedelta(minutes=15, seconds=1))
    assert not stale("claimed", minutes=10, claimed=NOW - timedelta(minutes=20))
    assert stale("claimed", minutes=1, claimed=NOW - timedelta(minutes=11, seconds=1))
    # started_at prime sur claimed_at
    assert not stale("running", claimed=NOW - timedelta(hours=1), started=NOW - timedelta(minutes=15))
    assert stale("running", claimed=NOW, started=NOW - timedelta(minutes=15, seconds=1))
    # ni claimed_at ni started_at (ne devrait pas arriver) : created_at
    assert stale("running", created=NOW - timedelta(minutes=15, seconds=1))
    assert not stale("running", created=NOW - timedelta(minutes=15))
