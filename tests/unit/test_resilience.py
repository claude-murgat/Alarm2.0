"""Unit tests (tier 1) — backend/app/logic/resilience.py (INV-175).

Table de verite du plan « pannes de noeuds » (30/10) : ce que le systeme fait encore quand un
ou plusieurs noeuds tombent. Topologie de prod : node1/node2 Izeron (voteurs), node3 OVH
(voteur, relais entre sites, facade publique de l'app), node4/node5 Beaufort ; passerelles
onsite-2 (sur node2) et alarm-1 (sur node4).

Aujourd'hui (2026-11-05) : 7 noeuds, voteurs node1, node2, node3, onsite-1/2 (node1/node2) « Legacy » :
TODAY ci-dessous, avec le scenario « Legacy debranche » (sans leader : ce que corrige le jour du demenagement).

Rien n'appelle la logique a l'import du module : un mutant qui plante ferait echouer la
collecte (pytest sort en 2) et mutmut le compterait comme survivant.
"""
import dataclasses

import pytest

from backend.app.logic.resilience import (
    Gateway, Node, Scenario, Topology, connected, default_scenarios, evaluate, group_of)

pytestmark = pytest.mark.unit

NODES = (Node("node1", "izeron"), Node("node2", "izeron"), Node("node3", "ovh"),
         Node("node4", "beaufort"), Node("node5", "beaufort"))
GATEWAYS = (Gateway("onsite-2", "izeron", "node2"), Gateway("alarm-1", "beaufort", "node4"))
PROD = Topology(nodes=NODES, voters=frozenset({"node1", "node2", "node3"}),
                relay=frozenset({"node3"}), gateways=GATEWAYS)

# INV-175 « Prévenu » (2026-11-10) : chaque email qui part, qui l'envoie et après combien de temps.
def inv170(nodes):
    return f"email INV-170 « aucun leader » par {nodes} après 3 min"


def inv172(gateways):
    return f"email INV-172 « passerelle muette » {gateways} par le leader après 10 min"


def inv185(sites):
    return f"email INV-185 « site sans modem » {sites} par le leader après 10 min"


def inv189(nodes):
    return f"email INV-189 « nœud injoignable » {nodes} par le leader après 10 min"


INV053 = "email INV-053 « personne en ligne » par le leader après 15 min"
INV144 = "email INV-144 « escalade sans appel » par le leader pendant une alarme"
NO_LEADER = " Pas d'email du leader (INV-053, INV-144, INV-172, INV-185, INV-189) : aucun leader."


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


@pytest.mark.parametrize("key, node", [("izeron_one_node", "node1"), ("beaufort_one_node", "node5")])
def test_one_node_down_everything_ok_and_inv189(key, node):
    o = evaluate(PROD, scen(key))
    assert o.leader_possible and o.escalation_running
    assert o.writable_from == {"izeron": True, "ovh": True, "beaufort": True}
    assert o.alarms_created == o.sms_calls == {"izeron": True, "beaufort": True}
    assert o.app_4g_reachable and o.app_izeron_lan
    assert o.alerted_by == (inv189(node),)  # INV-189 : le leader signale le noeud arrete


def test_one_izeron_node_sentence():
    o = evaluate(PROD, scen("izeron_one_node"))
    assert o.why == "2/3 voteurs etcd joignables"
    assert o.sentence == ("Leader : oui (2/3 voteurs etcd joignables). Izeron : alarmes et SMS/appels OK ; "
                          "OVH : écriture OK ; Beaufort : alarmes et SMS/appels OK. App 4G : OK. "
                          "App LAN Izeron : OK. Prévenu : email INV-189 « nœud injoignable » node1 par le leader après 10 min.")


def test_ovh_down_beaufort_blind_and_isolated_nodes_alert():
    """Beaufort isole du leader (Izeron) : ses noeuds envoient INV-170, le leader signale node3 + node4 + node5
    (absents de /cluster), sa passerelle muette et son site sans modem."""
    o = evaluate(PROD, scen("ovh_down"))
    assert o.leader_possible and o.escalation_running
    assert o.why == "2/3 voteurs etcd joignables"
    assert o.writable_from == {"izeron": True, "ovh": False, "beaufort": False}
    assert o.alarms_created == o.sms_calls == {"izeron": True, "beaufort": False}
    assert o.app_4g_reachable is False
    assert o.app_izeron_lan is True
    assert o.alerted_by == (inv170("node4 + node5"), inv172("alarm-1"), inv185("Beaufort"),
                            inv189("node3 + node4 + node5"), INV144)
    assert o.sentence == ("Leader : oui (2/3 voteurs etcd joignables). Izeron : alarmes et SMS/appels OK ; "
                          "OVH : aucune écriture ; Beaufort : aveugle (aucune écriture, ni alarme, ni SMS). "
                          "App 4G : aucun backend. App LAN Izeron : OK. Prévenu : "
                          "email INV-170 « aucun leader » par node4 + node5 après 3 min, "
                          "email INV-172 « passerelle muette » alarm-1 par le leader après 10 min, "
                          "email INV-185 « site sans modem » Beaufort par le leader après 10 min, "
                          "email INV-189 « nœud injoignable » node3 + node4 + node5 par le leader après 10 min, "
                          "email INV-144 « escalade sans appel » par le leader pendant une alarme.")


def test_izeron_both_total_outage():
    o = evaluate(PROD, scen("izeron_both"))
    assert o.leader_possible is False and o.escalation_running is False
    assert o.why == "1/3 voteurs etcd joignables"
    assert o.writable_from == {"izeron": False, "ovh": False, "beaufort": False}
    assert o.alarms_created == o.sms_calls == {"izeron": False, "beaufort": False}
    assert o.app_4g_reachable is False and o.app_izeron_lan is False
    # Sans leader, INV-189 ne peut pas partir : chaque noeud debout envoie INV-170, et la phrase le dit.
    assert o.alerted_by == (inv170("node3 + node4 + node5"),)
    assert o.sentence == ("Leader : non (1/3 voteurs etcd joignables). Izeron : aveugle (aucune écriture, ni alarme, "
                          "ni SMS) ; OVH : aucune écriture ; Beaufort : aveugle (aucune écriture, ni alarme, ni SMS). "
                          "App 4G : aucun backend. App LAN Izeron : KO. "
                          "Prévenu : email INV-170 « aucun leader » par node3 + node4 + node5 après 3 min."
                          " Pas d'email du leader (INV-053, INV-144, INV-172, INV-185, INV-189) : aucun leader.")


def test_everything_down():
    o = evaluate(PROD, _down(*(n.name for n in NODES)))
    assert o.leader_possible is False and o.why == "0/3 voteurs etcd joignables"
    assert o.alerted_by == ()  # aucun noeud debout pour envoyer INV-170
    assert o.sentence.endswith("Prévenu : personne." + NO_LEADER)


def test_beaufort_both_only_beaufort_sms_and_alarms_lost():
    o = evaluate(PROD, scen("beaufort_both"))
    assert o.leader_possible
    assert o.writable_from == {"izeron": True, "ovh": True, "beaufort": False}
    assert o.alarms_created == o.sms_calls == {"izeron": True, "beaufort": False}
    assert o.app_4g_reachable and o.app_izeron_lan
    assert o.alerted_by == (inv172("alarm-1"), inv185("Beaufort"), inv189("node4 + node5"), INV144)


def test_gateway_izeron_down():
    o = evaluate(PROD, scen("gateway_izeron"))
    assert o.leader_possible
    assert o.why == "3/3 voteurs etcd joignables"
    assert o.writable_from == {"izeron": True, "ovh": True, "beaufort": True}
    assert o.alarms_created == o.sms_calls == {"izeron": False, "beaufort": True}
    assert o.app_4g_reachable and o.app_izeron_lan
    assert o.alerted_by == (inv172("onsite-2"), inv185("Izeron"), INV144)
    assert o.sentence == ("Leader : oui (3/3 voteurs etcd joignables). Izeron : SMS/appels bloqués (passerelle muette) ; "
                          "OVH : écriture OK ; Beaufort : alarmes et SMS/appels OK. App 4G : OK. "
                          "App LAN Izeron : OK. Prévenu : "
                          "email INV-172 « passerelle muette » onsite-2 par le leader après 10 min, "
                          "email INV-185 « site sans modem » Izeron par le leader après 10 min, "
                          "email INV-144 « escalade sans appel » par le leader pendant une alarme.")


def test_gateway_beaufort_down():
    o = evaluate(PROD, scen("gateway_beaufort"))
    assert o.writable_from == {"izeron": True, "ovh": True, "beaufort": True}
    assert o.alarms_created == o.sms_calls == {"izeron": True, "beaufort": False}
    assert o.alerted_by == (inv172("alarm-1"), inv185("Beaufort"), INV144)


def test_ovh_plus_one_izeron_node_no_leader():
    """Un noeud d'Izeron debout mais seul voteur visible : il ne peut pas ecrire."""
    o = evaluate(PROD, _down("node2", "node3"))
    assert o.leader_possible is False
    assert o.writable_from == {"izeron": False, "ovh": False, "beaufort": False}
    assert o.app_izeron_lan is False and o.app_4g_reachable is False
    assert o.alerted_by == (inv170("node1 + node4 + node5"),)


def test_only_izeron_up():
    """OVH et Beaufort a terre : Izeron ecrit encore (2 voteurs), l'app ne passe qu'en LAN."""
    o = evaluate(PROD, _down("node3", "node4", "node5"))
    assert o.leader_possible
    assert o.writable_from == {"izeron": True, "ovh": False, "beaufort": False}
    assert o.app_izeron_lan is True and o.app_4g_reachable is False
    assert o.alerted_by == (inv172("alarm-1"), inv185("Beaufort"), inv189("node3 + node4 + node5"), INV144)


def test_no_inv085_when_half_the_nodes_are_down():
    """INV-085 compte les membres listes par /cluster : un noeud arrete en disparait, le ratio reste sain.
    node2 + node3 debout (2 voteurs, relais) : leader oui, 2/5 noeuds debout -> INV-189 pour les 3 autres."""
    o = evaluate(PROD, _down("node1", "node4", "node5"))
    assert o.leader_possible
    assert o.alerted_by == (inv172("alarm-1"), inv185("Beaufort"), inv189("node1 + node4 + node5"), INV144)
    assert not any("INV-085" in a for a in o.alerted_by)


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
    assert o.alerted_by == (inv172("onsite-2"), inv185("Izeron"), inv189("node1 + node2"), INV144)
    assert evaluate(cloud, _down("node1", "node4", "node5")).alerted_by == (
        inv172("alarm-1"), inv185("Beaufort"), inv189("node1 + node4 + node5"), INV144)
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
    assert o.alerted_by == (inv172("onsite-2"), inv185("Izeron"), inv189("node1 + node2 + node3"), INV053, INV144)
    assert o.sentence.endswith(
        "Prévenu : email INV-172 « passerelle muette » onsite-2 par le leader après 10 min, "
        "email INV-185 « site sans modem » Izeron par le leader après 10 min, "
        "email INV-189 « nœud injoignable » node1 + node2 + node3 par le leader après 10 min, "
        "email INV-053 « personne en ligne » par le leader après 15 min, "
        "email INV-144 « escalade sans appel » par le leader pendant une alarme.")


def test_connected():
    assert connected(PROD, "node1", "node2", set()) is True          # meme site
    assert connected(PROD, "node1", "node4", {"node3"}) is True      # relais debout
    assert connected(PROD, "node1", "node4", {"node1", "node4"}) is False
    assert connected(PROD, "node3", "node4", {"node3"}) is True


def test_outcome_is_plain_data():
    o = evaluate(PROD, scen("ovh_down"))
    d = dataclasses.asdict(o)
    assert d["writable_from"]["beaufort"] is False and d["alerted_by"][0] == inv170("node4 + node5")


# Aujourd'hui : node6/node7 (alarm-3/4) etiquetes Izeron, non votants ; onsite-1/2 « Legacy ».
TODAY = dataclasses.replace(PROD, nodes=NODES + (Node("node6", "izeron"), Node("node7", "izeron")),
                            retiring=frozenset({"node2", "node1"}))
LEGACY = "Legacy débranché (node1 + node2) : ce que corrige le jour du déménagement"


def test_default_scenarios_add_legacy_unplugged_when_nodes_are_retiring():
    scenarios = scen(topo=TODAY)
    assert list(scenarios) == ["izeron_one_node", "beaufort_one_node", "izeron_both", "ovh_down",
                               "beaufort_both", "legacy_unplugged", "gateway_izeron", "gateway_beaufort"]
    assert scenarios["legacy_unplugged"].down_nodes == {"node1", "node2"}
    assert scenarios["legacy_unplugged"].label == LEGACY  # noeuds dans l'ordre de la topologie
    assert scenarios["legacy_unplugged"].down_gateways == frozenset()
    assert [k for k, s in scenarios.items() if s.untestable] == ["izeron_both", "legacy_unplugged"]


def test_legacy_unplugged_today_no_leader():
    """onsite-1/2 debranches avec les 3 voteurs d'aujourd'hui : node3 seul voteur -> aucun leader. C'est ce
    que corrige le jour du demenagement (node6/node7 voteurs, node1/node2 retires d'etcd)."""
    o = evaluate(TODAY, scen("legacy_unplugged", TODAY))
    assert o.leader_possible is False and o.escalation_running is False
    assert o.why == "1/3 voteurs etcd joignables"
    assert o.writable_from == {"izeron": False, "ovh": False, "beaufort": False}
    assert o.alerted_by == (inv170("node3 + node4 + node5 + node6 + node7"),)
    assert o.sentence.endswith(NO_LEADER)


@pytest.mark.parametrize("site, retiring, group", [
    ("izeron", True, "Legacy (Izeron)"), ("izeron", False, "Izeron"), ("beaufort", False, "Beaufort"),
    ("ovh", False, "OVH"), ("beaufort", True, "Legacy (Izeron)"), (None, True, "Legacy (Izeron)"),
    (None, False, "Lieu inconnu"), ("", False, "Lieu inconnu")])
def test_group_of(site, retiring, group):
    """INV-161 (2026-11-05) : libelles EXACTS partages avec l'app Android (PR #297) : « Legacy (Izeron) »
    pour tout noeud retiring (regle du proprietaire, sans tenir compte du lieu), sinon le lieu."""
    assert group_of(site, retiring) == group


# INV-175 « Prévenu » (2026-11-10, demande du proprietaire) : chaque scenario par defaut de la topologie
# d'aujourd'hui (7 noeuds) -> les emails qui partent vraiment, qui les envoie, apres combien de temps.
PREVENU_TODAY = {
    "izeron_one_node": (inv189("node1"),),
    "beaufort_one_node": (inv189("node5"),),
    "izeron_both": (inv170("node3 + node4 + node5"),),
    "ovh_down": (inv170("node4 + node5"), inv172("alarm-1"), inv185("Beaufort"),
                 inv189("node3 + node4 + node5"), INV144),
    "beaufort_both": (inv172("alarm-1"), inv185("Beaufort"), inv189("node4 + node5"), INV144),
    "legacy_unplugged": (inv170("node3 + node4 + node5 + node6 + node7"),),
    "gateway_izeron": (inv172("onsite-2"), inv185("Izeron"), INV144),
    "gateway_beaufort": (inv172("alarm-1"), inv185("Beaufort"), INV144),
}


@pytest.mark.parametrize("key", list(PREVENU_TODAY))
def test_prevenu_of_each_default_scenario_today(key):
    o = evaluate(TODAY, scen(key, TODAY))
    alerted = PREVENU_TODAY[key]
    assert o.alerted_by == alerted
    assert o.sentence.endswith("Prévenu : " + ", ".join(alerted) + "." + ("" if o.leader_possible else NO_LEADER))


def test_prevenu_covers_every_default_scenario_today():
    assert list(scen(topo=TODAY)) == list(PREVENU_TODAY)


def test_one_of_two_gateways_of_a_site_down():
    """INV-172 pour la passerelle coupee ; le site garde son autre passerelle : ni INV-185, ni INV-144."""
    two = dataclasses.replace(PROD, gateways=GATEWAYS + (Gateway("alarm-2", "beaufort", "node5"),))
    o = evaluate(two, _down(gateways=["alarm-1"]))
    assert o.alarms_created == {"izeron": True, "beaufort": True}
    assert o.alerted_by == (inv172("alarm-1"),)
    both = evaluate(two, _down(gateways=["alarm-1", "alarm-2"]))
    assert both.alerted_by == (inv172("alarm-1 + alarm-2"), inv185("Beaufort"), INV144)


def test_nothing_down_nobody_alerted():
    o = evaluate(PROD, _down())
    assert o.alerted_by == ()
    assert o.sentence.endswith("Prévenu : personne.")
