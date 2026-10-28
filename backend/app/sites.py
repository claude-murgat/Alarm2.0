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


# INV-161 : lieu de chaque noeud du cluster (onglet Cluster). nom:lieu separes par des virgules.
DEFAULT_NODE_LOCATIONS = "node1:izeron,node2:izeron,node3:ovh,node4:beaufort,node5:beaufort"


def node_locations(db: Session) -> dict[str, str]:
    pairs = (p.split(":", 1) for p in _split(_config(db, "node_locations", DEFAULT_NODE_LOCATIONS)))
    return {name.strip(): loc.strip() for name, loc in (p for p in pairs if len(p) == 2)}
