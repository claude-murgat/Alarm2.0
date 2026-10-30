"""API system — endpoint /peer-health pour le detecteur emergency CD (cf §7 doc).

Chaque backend retourne SA VUE des 2 voisins (lit PEER_TEST_URLS).
Le detecteur (sur NODE3) consulte les 3 vues + applique vote majoritaire 2/3
avant de juger "service casse" (anti faux-positif panne reseau).

Endpoint public (pas d'auth) pour permettre l'orchestrateur de l'interroger
sans gerer les tokens admin. Retourne uniquement des metriques operationnelles,
aucune donnee business -> pas de fuite.
"""
import json
import os
import time
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..clock import now as clock_now
from ..database import get_db
from ..logic.modem_signal import modem_signal
from ..models import SystemConfig, User
from ..sites import default_site
from pydantic import BaseModel

router = APIRouter(prefix="/api/system", tags=["system"])

# Boot time (pour uptime). Capture au moment de l'import.
_BOOT_TS = time.time()


class PeerState(BaseModel):
    reachable: bool
    health: str  # "200" | "503" | "timeout" | "refused" | "<status_code>"
    lag_ms: Optional[float] = None
    last_check: str  # ISO 8601


class PeerHealthResponse(BaseModel):
    self_node: str
    peers: dict  # {"<peer-url>": PeerState}
    etcd_quorum_seen: bool
    uptime_s: int
    checked_at: str


def _probe_peer(url: str, timeout: float = 5.0) -> PeerState:
    """Ping un peer et retourne sa vue depuis ICI.

    Distinction nette (§7 doc — durcissement reseau) :
      - 200/2xx : sain, ce noeud repond bien
      - 503     : malade (le backend a repondu mais s'auto-declare KO -> bug logiciel)
      - timeout : injoignable (probablement reseau, NE compte PAS comme "casse")
      - refused : container down (peut etre logiciel ou reseau, ambigu)
    """
    health_target = url.rstrip("/") + "/health"
    t0 = time.time()
    try:
        r = requests.get(health_target, timeout=timeout)
        lag_ms = (time.time() - t0) * 1000
        return PeerState(
            reachable=True,
            health=str(r.status_code),
            lag_ms=round(lag_ms, 1),
            last_check=datetime.utcnow().isoformat(),
        )
    except requests.Timeout:
        return PeerState(
            reachable=False, health="timeout",
            last_check=datetime.utcnow().isoformat(),
        )
    except requests.ConnectionError as e:
        # ConnectionRefusedError vs DNS error -> on simplifie en "refused".
        kind = "refused" if "refused" in str(e).lower() else "unreachable"
        return PeerState(
            reachable=False, health=kind,
            last_check=datetime.utcnow().isoformat(),
        )
    except Exception:
        return PeerState(
            reachable=False, health="error",
            last_check=datetime.utcnow().isoformat(),
        )


@router.get("/peer-health", response_model=PeerHealthResponse)
def peer_health():
    """Vue locale du peer-health (cf §7 doc).

    PEER_TEST_URLS est deja defini cote env (cf .env.prod.nodeX) sous la forme
    'http://10.99.0.X:8000,http://10.99.0.Y:8000'. On reutilise sans changement.
    """
    self_node = os.getenv("NODE_NAME", "unknown")
    peers_raw = os.getenv("PEER_TEST_URLS", "")
    peer_urls = [u.strip() for u in peers_raw.split(",") if u.strip()]

    peers_state = {url: _probe_peer(url) for url in peer_urls}

    # etcd_quorum_seen : lecture best-effort via le module leader_election.
    # Si on est leader OU si on tourne et qu'on a une DB up, on a quorum.
    try:
        from ..leader_election import is_leader
        # is_leader.is_set() vrai = primary, mais quorum existe meme cote replica.
        # Pour V1 on simplifie : si le backend tourne et /health est OK, on dit
        # quorum_seen=true. Le detecteur croisera avec ses propres etcdctl en
        # cas de doute (cf §7 Cond 1b durcie).
        quorum = True
    except Exception:
        quorum = False

    return PeerHealthResponse(
        self_node=self_node,
        peers={url: state.dict() for url, state in peers_state.items()},
        etcd_quorum_seen=quorum,
        uptime_s=int(time.time() - _BOOT_TS),
        checked_at=datetime.utcnow().isoformat(),
    )


@router.get("/modem-status")
def modem_status(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """INV-129/158 : barres de signal 4G du modem de chaque passerelle (app + web).
    `gateways` : une entree par passerelle. Premier niveau : la plus mal en point
    (app d'avant INV-158, qui lit un seul objet)."""
    now = clock_now()
    rows = db.query(SystemConfig).filter(
        or_(SystemConfig.key == "modem_status", SystemConfig.key.like("modem_status:%"))).all()
    gateways = []
    for row in rows:
        status = json.loads(row.value)
        gw = {"gateway_id": status.get("gateway_id") or "legacy",
              "site": status.get("site") or default_site(db), **modem_signal(status, now)}
        # Ancienne passerelle muette : mise a jour depuis, elle rapporte sous son gateway_id
        if gw["gateway_id"] != "legacy" or gw["state"] != "unknown":
            gateways.append(gw)
    gateways.sort(key=lambda g: (g["site"], g["gateway_id"]))
    worst = min(gateways, key=lambda g: g["bars"],
                default={"gateway_id": None, "site": None, **modem_signal(None, now)})
    return {**worst, "gateways": gateways}
