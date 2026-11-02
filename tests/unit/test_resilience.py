"""Unit tests (tier 1) — backend/app/logic/resilience.py (INV-175).

Table de verite du plan « pannes de noeuds » (30/10) : ce que le systeme fait encore quand un
ou plusieurs noeuds tombent. Topologie de prod : node1/node2 Izeron (voteurs), node3 OVH
(voteur, relais entre sites, facade publique de l'app), node4/node5 Beaufort ; passerelles
onsite-2 (sur node2) et alarm-1 (sur node4).

Rien n'appelle la logique a l'import du module : un mutant qui plante ferait echouer la
collecte (pytest sort en 2) et mutmut le compterait comme survivant.
"""
import dataclasses

import pytest

from backend.app.logic.resilience import (
    Gateway, Node, Scenario, Topology, connected, default_scenarios, evaluate)

pytestmark = pytest.mark.unit

NODES = (Node("node1", "izeron"), Node("node2", "izeron"), Node("node3", "ovh"),
         Node("node4", "beaufort"), Node("node5", "beaufort"))
GATEWAYS = (Gateway("onsite-2", "izeron", "node2"), Gateway("alarm-1", "beaufort", "node4"))
PROD = Topology(nodes=NODES, voters=frozenset({"node1", "node2", "node3"}),
                relay=frozenset({"node3"}), gateways=GATEWAYS)

INV170 = "email INV-170 (après PR 1)"


def scen(key=None, topo=PROD):
    d = {s.key: s for s in default_scenarios(topo)}
    return d[key] if key else d


def _down(*nodes, gateways=()):
    return Scenario("adhoc", "ad hoc", frozenset(nodes), frozenset(gateways))


def test_default_scenarios_of_prod():
    scenarios = scen()
    assert list(scenarios) == ["izeron_one_node", "beaufort_one_node", "izeron_both", "ovh_down",
                               "beaufort_both", "gateway_izeron", "gateway_beaufort"]
    assert scenarios["izeron_one_node"].down_nodes == {"node1"}
    assert scenarios["beaufort_one_node"].down_nodes == {"node5"}  # node4 porte la passerelle
    assert scenarios["ovh_down"].down_nodes == {"node3"}
    assert scenarios["izeron_both"].down_nodes == {"node1", "node2"}
    assert scenarios["beaufort_both"].down_nodes == {"node4", "node5"}
    assert scenarios["gateway_izeron"].down_gateways == {"onsite-2"}
    assert scenarios["gateway_beaufort"].down_gateways == {"alarm-1"}
    assert all(not s.down_nodes for k, s in scenarios.items() if k.startswith("gateway_"))
    assert all(not s.down_gateways for k, s in scenarios.items() if not k.startswith("gateway_"))
    assert [k for k, s in scenarios.items() if s.untestable] == ["izeron_both"]
    assert [s.label for s in scenarios.values()] == [
        "Un nœud à Izeron (node1)", "Un nœud à Beaufort (node5)", "Izeron entier (node1 + node2)",
        "OVH (node3)", "Beaufort entier (node4 + node5)", "Passerelle Izeron (onsite-2)",
        "Passerelle Beaufort (alarm-1)"]
    assert _down("node1").untestable is False and _down("node1").down_gateways == frozenset()


@pytest.mark.parametrize("key", ["izeron_one_node", "beaufort_one_node"])
def test_one_node_down_everything_ok(key):
    o = evaluate(PROD, scen(key))
    assert o.leader_possible and o.escalation_running
    assert o.writable_from == {"izeron": True, "ovh": True, "beaufort": True}
    assert o.alarms_created == o.sms_calls == {"izeron": True, "beaufort": True}
    assert o.app_4g_reachable and o.app_izeron_lan
    assert o.alerted_by == ()


def test_one_izeron_node_sentence():
    o = evaluate(PROD, scen("izeron_one_node"))
    assert o.why == "2/3 voteurs etcd joignables"
    assert o.sentence == ("Leader : oui (2/3 voteurs etcd joignables). Izeron : alarmes et SMS/appels OK ; "
                          "OVH : écriture OK ; Beaufort : alarmes et SMS/appels OK. App 4G : OK. "
                          "App LAN Izeron : OK. Prévenu : personne.")


def test_ovh_down_beaufort_blind_and_nobody_alerted():
    o = evaluate(PROD, scen("ovh_down"))
    assert o.leader_possible and o.escalation_running
    assert o.why == "2/3 voteurs etcd joignables"
    assert o.writable_from == {"izeron": True, "ovh": False, "beaufort": False}
    assert o.alarms_created == o.sms_calls == {"izeron": True, "beaufort": False}
    assert o.app_4g_reachable is False
    assert o.app_izeron_lan is True
    assert o.alerted_by == ()
    assert o.sentence == ("Leader : oui (2/3 voteurs etcd joignables). Izeron : alarmes et SMS/appels OK ; "
                          "OVH : aucune écriture ; Beaufort : aveugle (aucune écriture, ni alarme, ni SMS). "
                          "App 4G : aucun backend. App LAN Izeron : OK. Prévenu : personne.")


def test_izeron_both_total_outage():
    o = evaluate(PROD, scen("izeron_both"))
    assert o.leader_possible is False and o.escalation_running is False
    assert o.why == "1/3 voteurs etcd joignables"
    assert o.writable_from == {"izeron": False, "ovh": False, "beaufort": False}
    assert o.alarms_created == o.sms_calls == {"izeron": False, "beaufort": False}
    assert o.app_4g_reachable is False and o.app_izeron_lan is False
    assert o.alerted_by == (INV170,)
    assert o.sentence == ("Leader : non (1/3 voteurs etcd joignables). Izeron : aveugle (aucune écriture, ni alarme, "
                          "ni SMS) ; OVH : aucune écriture ; Beaufort : aveugle (aucune écriture, ni alarme, ni SMS). "
                          "App 4G : aucun backend. App LAN Izeron : KO. Prévenu : email INV-170 (après PR 1).")


def test_everything_down():
    o = evaluate(PROD, _down(*(n.name for n in NODES)))
    assert o.leader_possible is False and o.why == "0/3 voteurs etcd joignables"
    assert o.alerted_by == (INV170,)


def test_beaufort_both_only_beaufort_sms_and_alarms_lost():
    o = evaluate(PROD, scen("beaufort_both"))
    assert o.leader_possible
    assert o.writable_from == {"izeron": True, "ovh": True, "beaufort": False}
    assert o.alarms_created == o.sms_calls == {"izeron": True, "beaufort": False}
    assert o.app_4g_reachable and o.app_izeron_lan
    assert o.alerted_by == ("email INV-144",)


def test_gateway_izeron_down():
    o = evaluate(PROD, scen("gateway_izeron"))
    assert o.leader_possible
    assert o.why == "3/3 voteurs etcd joignables"
    assert o.writable_from == {"izeron": True, "ovh": True, "beaufort": True}
    assert o.alarms_created == o.sms_calls == {"izeron": False, "beaufort": True}
    assert o.app_4g_reachable and o.app_izeron_lan
    assert o.alerted_by == ("email INV-144",)
    assert o.sentence == ("Leader : oui (3/3 voteurs etcd joignables). Izeron : SMS/appels bloqués (passerelle muette) ; "
                          "OVH : écriture OK ; Beaufort : alarmes et SMS/appels OK. App 4G : OK. "
                          "App LAN Izeron : OK. Prévenu : email INV-144.")


def test_gateway_beaufort_down():
    o = evaluate(PROD, scen("gateway_beaufort"))
    assert o.writable_from == {"izeron": True, "ovh": True, "beaufort": True}
    assert o.alarms_created == o.sms_calls == {"izeron": True, "beaufort": False}
    assert o.alerted_by == ("email INV-144",)


def test_ovh_plus_one_izeron_node_no_leader():
    """Un noeud d'Izeron debout mais seul voteur visible : il ne peut pas ecrire."""
    o = evaluate(PROD, _down("node2", "node3"))
    assert o.leader_possible is False
    assert o.writable_from == {"izeron": False, "ovh": False, "beaufort": False}
    assert o.app_izeron_lan is False and o.app_4g_reachable is False
    assert o.alerted_by == (INV170,)


def test_only_izeron_up():
    """OVH et Beaufort a terre : Izeron ecrit encore (2 voteurs), l'app ne passe qu'en LAN."""
    o = evaluate(PROD, _down("node3", "node4", "node5"))
    assert o.leader_possible
    assert o.writable_from == {"izeron": True, "ovh": False, "beaufort": False}
    assert o.app_izeron_lan is True and o.app_4g_reachable is False
    assert o.alerted_by == ("email INV-144", "email INV-085")


def test_inv085_email_only_with_a_leader_and_half_members_down():
    # node2 + node3 debout (2 voteurs, relais) : leader oui, 2/5 membres sains -> INV-085 ;
    # alarm-1 (node4) a terre -> INV-144 aussi.
    o = evaluate(PROD, _down("node1", "node4", "node5"))
    assert o.leader_possible
    assert o.alerted_by == ("email INV-144", "email INV-085")
    assert o.sentence.endswith("Prévenu : email INV-144, email INV-085.")
    # 3/5 sains : plus de la moitie -> pas d'INV-085 (cf. beaufort_both)
    assert "email INV-085" not in evaluate(PROD, scen("beaufort_both")).alerted_by


def test_second_cloud_voter_keeps_a_leader_when_izeron_is_down():
    """Le modele suit la topologie : un 2e voteur cloud rend « Izeron entier » testable."""
    cloud = dataclasses.replace(PROD, nodes=NODES + (Node("node6", "cloud"),),
                                voters=frozenset({"node1", "node3", "node6"}))
    assert scen("izeron_both", cloud).untestable is False
    o = evaluate(cloud, scen("izeron_both", cloud))
    assert o.leader_possible and o.escalation_running
    assert o.why == "2/3 voteurs etcd joignables"
    assert o.writable_from == {"izeron": False, "ovh": True, "beaufort": True, "cloud": True}
    assert o.alarms_created == {"izeron": False, "beaufort": True}
    assert o.app_4g_reachable is True and o.app_izeron_lan is False
    assert o.alerted_by == ("email INV-144",)
    # 3 membres sains sur 6 : la moitie exactement -> INV-085
    assert evaluate(cloud, _down("node1", "node4", "node5")).alerted_by == ("email INV-144", "email INV-085")
    # 4 voteurs (node6 en plus, node2 garde) : la majorite passe a 3, Izeron entier tue le quorum.
    four = dataclasses.replace(cloud, voters=frozenset({"node1", "node2", "node3", "node6"}))
    assert evaluate(four, scen("izeron_both", four)).leader_possible is False
    assert scen("izeron_both", four).untestable is True


def test_inv053_when_leader_but_no_path_for_the_app():
    two_clouds = dataclasses.replace(
        PROD, nodes=NODES + (Node("node6", "cloud"), Node("node7", "cloud")),
        voters=frozenset({"node1", "node6", "node7"}), relay=frozenset({"node3", "node6"}))
    o = evaluate(two_clouds, _down("node1", "node2", "node3"))
    assert o.leader_possible
    assert o.app_4g_reachable is False and o.app_izeron_lan is False
    assert o.alerted_by == ("email INV-144", "email INV-053")


def test_connected():
    assert connected(PROD, "node1", "node2", set()) is True          # meme site
    assert connected(PROD, "node1", "node4", {"node3"}) is True      # relais debout
    assert connected(PROD, "node1", "node4", {"node1", "node4"}) is False
    assert connected(PROD, "node3", "node4", {"node3"}) is True


def test_outcome_is_plain_data():
    o = evaluate(PROD, scen("ovh_down"))
    d = dataclasses.asdict(o)
    assert d["writable_from"]["beaufort"] is False and d["alerted_by"] == ()
