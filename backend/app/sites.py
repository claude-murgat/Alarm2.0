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
