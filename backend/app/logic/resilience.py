"""Logique pure : panneau resilience (INV-175) — ce que le systeme fait encore quand des noeuds
ou des passerelles tombent.

Topologie de prod : node1/node2 Izeron, node3 OVH, node4/node5 Beaufort ; voteurs etcd
node1-3 ; node3 seul relais entre les sites et facade publique de l'app ; passerelles onsite-2
(sur node2) et alarm-1 (sur node4). Tout vient de SystemConfig (sites.py), rien n'est fige ici.
`alerted_by` encode les alertes d'aujourd'hui : sans leader, aucun email ne part (INV-085 est
leader-gated) ; INV-170 (PR 1) ajoutera l'email « aucun leader » depuis chaque noeud.
"""
from dataclasses import dataclass, replace


@dataclass(frozen=True)  # pragma: no mutate (frozen=False equivalent : rien ne mute ces valeurs)
class Node:
    name: str
    site: str


@dataclass(frozen=True)  # pragma: no mutate (frozen=False equivalent : rien ne mute ces valeurs)
class Gateway:
    gateway_id: str
    site: str
    host: str


@dataclass(frozen=True)  # pragma: no mutate (frozen=False equivalent : rien ne mute ces valeurs)
class Topology:
    nodes: tuple
    voters: frozenset
    relay: frozenset
    gateways: tuple
    public_nodes: frozenset = frozenset({"node3"})
    default_site: str = "izeron"


@dataclass(frozen=True)  # pragma: no mutate (frozen=False equivalent : rien ne mute ces valeurs)
class Scenario:
    key: str
    label: str
    down_nodes: frozenset
    down_gateways: frozenset = frozenset()
    untestable: bool = False


@dataclass(frozen=True)  # pragma: no mutate (frozen=False equivalent : rien ne mute ces valeurs)
class Outcome:
    leader_possible: bool
    why: str
    writable_from: dict
    escalation_running: bool
    alarms_created: dict
    sms_calls: dict
    app_4g_reachable: bool
    app_izeron_lan: bool
    alerted_by: tuple
    sentence: str


def _place(site: str) -> str:
    return {"ovh": "OVH"}.get(site, site.capitalize())


def connected(topo: Topology, a: str, b: str, up: set) -> bool:
    """Deux noeuds se voient : meme site, ou un relais debout."""
    site = {n.name: n.site for n in topo.nodes}
    return site[a] == site[b] or bool(topo.relay & up)


def evaluate(topo: Topology, scenario: Scenario) -> Outcome:
    site = {n.name: n.site for n in topo.nodes}
    up = set(site) - scenario.down_nodes
    sites = list(dict.fromkeys(list(site.values()) + [topo.default_site]))
    # Leader possible : un noeud debout voit plus de la moitie des voteurs
    seen = {n: sum(1 for v in topo.voters & up if connected(topo, n, v, up)) for n in up}
    candidates = {n for n, k in seen.items() if 2 * k > len(topo.voters)}
    leader = bool(candidates)
    why = f"{max(seen.values(), default=0)}/{len(topo.voters)} voteurs etcd joignables"
    writable = {s: any(connected(topo, n, c, up) for n in up if site[n] == s for c in candidates) for s in sites}
    gw_up = {g: g.host in up and g.gateway_id not in scenario.down_gateways for g in topo.gateways}
    alarms = {g.site: any(gw_up[h] for h in topo.gateways if h.site == g.site) and writable[g.site]
              for g in topo.gateways}
    app_4g = any(p in up and writable[site[p]] for p in topo.public_nodes)
    lan = any(site[n] == topo.default_site for n in up) and writable[topo.default_site]
    alerted = []
    if leader:
        if not all(gw_up.values()):
            alerted.append("email INV-144")
        if not app_4g and not lan:
            alerted.append("email INV-053")
        if 2 * len(up) <= len(site):
            alerted.append("email INV-085")
    else:
        alerted.append("email INV-170 (après PR 1)")

    def status(s):
        if s not in alarms:
            return "écriture OK" if writable[s] else "aucune écriture"
        if alarms[s]:
            return "alarmes et SMS/appels OK"
        return "SMS/appels bloqués (passerelle muette)" if writable[s] else "aveugle (aucune écriture, ni alarme, ni SMS)"

    sentence = (f"Leader : {'oui' if leader else 'non'} ({why}). "
                + " ; ".join(f"{_place(s)} : {status(s)}" for s in sites) + ". "
                + f"App 4G : {'OK' if app_4g else 'aucun backend'}. "
                + f"App LAN {_place(topo.default_site)} : {'OK' if lan else 'KO'}. "
                + f"Prévenu : {', '.join(alerted) or 'personne'}.")
    return Outcome(leader_possible=leader, why=why, writable_from=writable, escalation_running=leader,
                   alarms_created=alarms, sms_calls=dict(alarms), app_4g_reachable=app_4g,
                   app_izeron_lan=lan, alerted_by=tuple(alerted), sentence=sentence)


def default_scenarios(topo: Topology) -> list:
    """Un noeud par site multi-noeuds (celui sans passerelle), chaque site entier, chaque
    passerelle. `untestable` = aucun leader possible (arret total, refuse par le proprietaire)."""
    by_site: dict = {}
    for n in topo.nodes:
        by_site.setdefault(n.site, []).append(n.name)
    hosts = {g.host for g in topo.gateways}
    out = []
    for s, names in by_site.items():
        if len(names) > 1:
            spare = [n for n in names if n not in hosts] or names
            out.append(Scenario(f"{s}_one_node", f"Un nœud à {_place(s)} ({spare[0]})", frozenset({spare[0]})))
    for s, names in by_site.items():
        if len(names) > 1:
            out.append(Scenario(f"{s}_both", f"{_place(s)} entier ({' + '.join(names)})", frozenset(names)))
        else:
            out.append(Scenario(f"{s}_down", f"{_place(s)} ({names[0]})", frozenset(names)))
    for g in topo.gateways:
        out.append(Scenario(f"gateway_{g.site}", f"Passerelle {_place(g.site)} ({g.gateway_id})",
                            frozenset(), frozenset({g.gateway_id})))
    return [replace(s, untestable=not evaluate(topo, s).leader_possible) for s in out]
