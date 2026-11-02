"""INV-175 : panneau resilience (onglet Tests, admin) — etat reel des noeuds et des passerelles
+ scenarios de panne calcules par logic/resilience.py.

- GET /api/resilience : `live` (sonde /health de chaque membre Patroni en parallele, 2 s) + `whatif`.
- GET /api/resilience/whatif?nodes=node4,node5&gateways=alarm-1 : verdict d'un scenario libre
  (dialogue de confirmation de la panne controlee, PR 4).
"""
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import get_current_admin
from ..clock import now as clock_now
from ..database import get_db
from ..logic.resilience import Gateway, Node, Scenario, Topology, default_scenarios, evaluate
from ..models import GatewayState, SystemConfig, User
from ..quorum_monitor import _patroni_cluster
from ..sites import _split, default_site, etcd_voters, gateway_hosts, node_locations, relay_nodes

router = APIRouter(prefix="/api/resilience", tags=["resilience"])
HEALTH_PORT = 8000  # backend de chaque noeud, sur l'IP wg0 que Patroni donne en `host`


def _topology(db: Session) -> Topology:
    locations = node_locations(db)
    return Topology(
        nodes=tuple(Node(name, site) for name, site in locations.items()),
        voters=frozenset(etcd_voters(db)), relay=frozenset(relay_nodes(db)),
        gateways=tuple(Gateway(g, locations.get(h, "?"), h) for g, h in gateway_hosts(db).items()),
        default_site=default_site(db))


def _health(host):
    """/health du backend de `host` ; None s'il ne repond pas (503 « degraded » = il repond)."""
    try:
        try:
            body = urllib.request.urlopen(f"http://{host}:{HEALTH_PORT}/health", timeout=2).read()
        except urllib.error.HTTPError as e:
            body = e.read()
        return json.loads(body)
    except Exception:
        return None


def _gateways_live(db: Session, topo: Topology, now: datetime) -> list:
    """Derniere trace de chaque passerelle : report-state (gateway_states) ou modem-status."""
    ids = [g.gateway_id for g in topo.gateways]
    seen = {g.gateway_id: [g.last_seen] for g in db.query(GatewayState).filter(GatewayState.gateway_id.in_(ids))}
    for row in db.query(SystemConfig).filter(SystemConfig.key.in_([f"modem_status:{i}" for i in ids])):
        seen.setdefault(row.key.split(":", 1)[1], []).append(datetime.fromisoformat(json.loads(row.value)["reported_at"]))
    out = []
    for g in topo.gateways:
        last = max(seen.get(g.gateway_id, []), default=None)
        out.append({"gateway_id": g.gateway_id, "host": g.host, "site": g.site, "registered": last is not None,
                    "last_seen_s": (now - last).total_seconds() if last else None})
    return out


@router.get("")
def resilience(_admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    try:
        members = _patroni_cluster().get("members", [])
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Patroni unreachable: {e}")
    topo = _topology(db)
    by_name = {m.get("name"): m for m in members}
    with ThreadPoolExecutor(5) as pool:
        healths = dict(zip(by_name, pool.map(lambda m: _health(m["host"]) if m.get("host") else None, by_name.values())))
    nodes = [{"name": n.name, "site": n.site, "voter": n.name in topo.voters,
              "reachable": healths.get(n.name) is not None,
              "role": by_name.get(n.name, {}).get("role"), "state": by_name.get(n.name, {}).get("state"),
              "version": (healths.get(n.name) or {}).get("version")} for n in topo.nodes]
    return {
        "live": {"nodes": nodes, "gateways": _gateways_live(db, topo, clock_now()),
                 "leader": next((m.get("name") for m in members if m.get("role") == "leader"), None),
                 "etcd_voters_up": sum(1 for n in nodes if n["voter"] and n["reachable"])},
        "whatif": [{"scenario": s.key, "label": s.label, "untestable": s.untestable, **asdict(evaluate(topo, s))}
                   for s in default_scenarios(topo)],
    }


@router.get("/whatif")
def whatif(nodes: str = "", gateways: str = "",
           _admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    return asdict(evaluate(_topology(db), Scenario("adhoc", "ad hoc", frozenset(_split(nodes)), frozenset(_split(gateways)))))
