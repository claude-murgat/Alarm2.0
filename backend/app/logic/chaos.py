"""Logique pure : garde-fous de la panne controlee (INV-176, INV-177).

`check_request` rend la raison du refus (francais, affichee en snackbar) ou None, dans l'ordre du
catalogue. L'API la joue a la demande (POST /api/chaos) et la rejoue au moment d'agir
(GET /internal/chaos/next, avec pending=0).
"""
VOTERS = ("node1", "node2", "node3")  # voteurs etcd : jamais deux a la fois (INV-177)


def expand_target(target: str, locations: dict) -> list:
    """`"node4, site:beaufort"` -> noeuds sans doublon, ordre garde. Un site inconnu reste tel quel
    (refuse ensuite comme noeud inconnu)."""
    out = []
    for item in filter(None, (t.strip() for t in target.split(","))):
        nodes = [n for n, site in locations.items() if item == f"site:{site}"] or [item]
        out += [n for n in nodes if n not in out]
    return out


def check_request(nodes, mode, minutes, cluster, locations, active_alarms, pending):
    """`cluster` = JSON de GET {PATRONI_URL}/cluster (None si injoignable) ; `locations` = node_locations."""
    if not 1 <= minutes <= 10:
        return "durée : entre 1 et 10 minutes"
    if not nodes:
        return "aucune cible"
    for n in nodes:
        if n not in locations:
            return f"nœud inconnu : {n}"
    voters = [n for n in nodes if n in VOTERS]
    if len(voters) >= 2:
        return f"{', '.join(voters)} : quorum etcd perdu — refusé"
    if mode == "netcut" and "node3" in nodes:
        return "coupure réseau de node3 refusée : le runner et l'interface web y vivent"
    if active_alarms > 0:
        return "une alarme est en cours (active, escaladée ou acquittée)"
    if pending > 0:
        return "un test est déjà en cours"
    if cluster is None:
        return "Patroni injoignable"
    members = {m.get("name"): m for m in cluster.get("members", [])}
    for n in locations:
        if n not in members:
            return f"membre absent du cluster Patroni : {n}"
    if len(members) != len(locations):
        return f"{len(members)} membres Patroni au lieu de {len(locations)}"
    leader = next((m for m in members.values() if m.get("role") == "leader"), None)
    if leader is None:
        return "aucun leader Patroni"
    if leader.get("state") != "running":
        return f"leader {leader['name']} pas running ({leader.get('state')})"
    for m in members.values():
        if m is not leader and m.get("state") != "streaming":
            return f"{m['name']} pas streaming ({m.get('state')})"
    return None
