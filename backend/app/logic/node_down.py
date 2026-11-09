"""Logique pure : noeud injoignable ou qui ne suit plus le leader (INV-189).

Sain = le leader `running`, ou `streaming` sur la timeline du leader (regle des cartes de l'onglet
Cluster, INV-161 ; timeline absente : seul l'etat compte, comme INV-168). Pas sain sans interruption
pendant plus de 10 min -> un email par episode ; un tick sain clot l'episode, avec « retabli » s'il
avait ete signale. L'appelant (`quorum_monitor`, leader seul) garde `state` dans SystemConfig.
"""
from datetime import datetime, timedelta

NODE_DOWN_AFTER = timedelta(minutes=10)


def _problem(member, leader):
    """None si le membre est sain, sinon ce que le leader en voit (email et audit)."""
    if member is None:
        return "absent de /cluster"
    if member is leader:
        return None
    state, timeline, leader_timeline = member.get("state"), member.get("timeline"), leader.get("timeline")
    if timeline is not None and leader_timeline is not None and timeline != leader_timeline:
        return f"{state} sur la timeline {timeline} (leader : {leader_timeline})"
    if state != "streaming":
        return f"état {state}"
    return None


def decide_node_down(cluster: dict, expected: list, state: dict, now: datetime) -> tuple[tuple, tuple, dict]:
    """`cluster` : JSON de GET /cluster ; `expected` : noeuds de `node_locations` ; `state` :
    {noeud: {"since", "emailed"}} du tick precedent. Renvoie (a signaler [(noeud, since, probleme)],
    retablis [(noeud, since)], nouvel etat). Sans leader `running`, rien ne change (INV-170 alerte)."""
    members = {m.get("name"): m for m in cluster.get("members", [])}
    leader = next((m for m in members.values() if m.get("role") == "leader"), None)
    if leader is None or leader.get("state") != "running":
        return (), (), state
    down, recovered, new_state = [], [], {}
    for name in sorted(set(expected) | set(members)):
        problem = _problem(members.get(name), leader)
        prev = state.get(name)
        if problem is None:
            if prev and prev["emailed"]:
                recovered.append((name, prev["since"]))
            continue
        since, emailed = (prev["since"], prev["emailed"]) if prev else (now, False)
        if not emailed and now - since > NODE_DOWN_AFTER:
            down.append((name, since, problem))
            emailed = True
        new_state[name] = {"since": since, "emailed": emailed}
    return tuple(down), tuple(recovered), new_state
