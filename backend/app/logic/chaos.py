"""Logique pure : garde-fous de la panne controlee (INV-176, INV-177).

`check_request` rend la raison du refus (francais, affichee en snackbar) ou None, dans l'ordre du
catalogue. L'API la joue a la demande (POST /api/chaos) et la rejoue au moment d'agir
(GET /internal/chaos/next, avec pending=0). `is_stale` : expiration d'une demande non terminee.
"""
from datetime import timedelta


def is_stale(status, minutes, n_nodes, created_at, claimed_at, started_at, now) -> bool:
    """INV-176 : pending jamais reclamee -> 30 min apres created_at ; claimed/running -> `minutes` + 5 min par noeud
    (le runner attend jusqu'a 300 s chaque noeud, l'un apres l'autre) + 2 min apres started_at (sinon claimed_at) :
    un test reclame tard ou un site lent a revenir n'expire pas avant la fin de sa restauration."""
    if status == "pending":
        return now - created_at > timedelta(minutes=30)
    return now - (started_at or claimed_at or created_at) > timedelta(minutes=minutes + 5 * n_nodes + 2)


def expand_target(target: str, locations: dict) -> list:
    """`"node4, site:beaufort"` -> noeuds sans doublon, ordre garde. Un site inconnu reste tel quel
    (refuse ensuite comme noeud inconnu)."""
    out = []
    for item in filter(None, (t.strip() for t in target.split(","))):
        nodes = [n for n, site in locations.items() if item == f"site:{site}"] or [item]
        out += [n for n in nodes if n not in out]
    return out


def check_request(nodes, mode, minutes, cluster, locations, voters, active_alarms, pending):
    """`cluster` = JSON de GET {PATRONI_URL}/cluster (None si injoignable) ; `locations` = node_locations ;
    `voters` = etcd_voters (INV-177 : jamais deux a la fois)."""
    if not 1 <= minutes <= 10:
        return "durée : entre 1 et 10 minutes"
    if not nodes:
        return "aucune cible"
    for n in nodes:
        if n not in locations:
            return f"nœud inconnu : {n}"
    hit = [n for n in nodes if n in voters]
    if len(hit) >= 2:
        return f"{', '.join(hit)} : quorum etcd perdu — refusé"
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
