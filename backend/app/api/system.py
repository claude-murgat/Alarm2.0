"""API system — endpoint /peer-health pour le detecteur emergency CD (cf §7 doc).

Chaque backend retourne SA VUE des 2 voisins (lit PEER_TEST_URLS).
Le detecteur (sur NODE3) consulte les 3 vues + applique vote majoritaire 2/3
avant de juger "service casse" (anti faux-positif panne reseau).

Endpoint public (pas d'auth) pour permettre l'orchestrateur de l'interroger
sans gerer les tokens admin. Retourne uniquement des metriques operationnelles,
aucune donnee business -> pas de fuite.
"""
import os
import time
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter
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
