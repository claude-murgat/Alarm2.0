"""INV-155/156 : sites connus (SystemConfig `sites`, `default_site`).

Les defauts s'appliquent aussi a une base deja en service (le seed ne tourne que sur
une base vide)."""
from sqlalchemy.orm import Session

from .models import SystemConfig

DEFAULT_SITES = "izeron,beaufort"
DEFAULT_SITE = "izeron"


def _split(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


def _config(db: Session, key: str, default: str) -> str:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    return row.value if row and row.value else default


def known_sites(db: Session) -> list[str]:
    return _split(_config(db, "sites", DEFAULT_SITES))


def default_site(db: Session) -> str:
    return _config(db, "default_site", DEFAULT_SITE)


def requested_sites(db: Session, site: str | None) -> list[str]:
    """Parametre `site` de /internal/{sms,calls}/pending : absent -> default_site
    (passerelle d'avant INV-155), liste separee par des virgules admise."""
    return _split(site or "") or [default_site(db)]


# INV-161 : lieu, machine et noeuds « bientot obsoletes » du cluster (onglet Cluster). nom:valeur separes
# par des virgules. Jour du demenagement (INV-090) : onsite-1/2 (node1/node2) retires, alarm-3/4 a Izeron.
DEFAULT_NODE_LOCATIONS = "node3:ovh,node4:beaufort,node5:beaufort,node6:izeron,node7:izeron"
DEFAULT_NODE_MACHINES = "node3:OVH,node4:alarm-1,node5:alarm-2,node6:alarm-3,node7:alarm-4"
DEFAULT_RETIRING_NODES = ""


def _pairs(db: Session, key: str, default: str) -> dict[str, str]:
    pairs = (p.split(":", 1) for p in _split(_config(db, key, default)))
    return {a.strip(): b.strip() for a, b in (p for p in pairs if len(p) == 2)}


def node_locations(db: Session) -> dict[str, str]:
    return _pairs(db, "node_locations", DEFAULT_NODE_LOCATIONS)


def node_machines(db: Session) -> dict[str, str]:
    return _pairs(db, "node_machines", DEFAULT_NODE_MACHINES)


def retiring_nodes(db: Session) -> list[str]:
    return _split(_config(db, "retiring_nodes", DEFAULT_RETIRING_NODES))


# INV-175 : topologie du panneau resilience. Memes defauts sur une base deja en service.
DEFAULT_ETCD_VOTERS = "node3,node6,node7"  # INV-090, jour du demenagement
DEFAULT_RELAY_NODES = "node3"  # seul noeud qui relie les sites entre eux
# passerelle:noeud qui l'heberge ; Izeron sur alarm-3 (node6) le jour J, alarm-4 -> « alarm-4:node7 »
DEFAULT_GATEWAY_HOSTS = "alarm-3:node6,alarm-1:node4"


def etcd_voters(db: Session) -> list[str]:
    return _split(_config(db, "etcd_voters", DEFAULT_ETCD_VOTERS))


def relay_nodes(db: Session) -> list[str]:
    return _split(_config(db, "relay_nodes", DEFAULT_RELAY_NODES))


def gateway_hosts(db: Session) -> dict[str, str]:
    return _pairs(db, "gateway_hosts", DEFAULT_GATEWAY_HOSTS)
