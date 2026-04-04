"""
Leader election via Patroni REST API.

Interroge Patroni toutes les 5 secondes pour savoir si ce noeud est le primary.
Patroni gere PostgreSQL (replication, promotion, failover) via etcd.

L'interface publique (is_leader, leader_election_loop) est identique a
l'ancienne version basee sur advisory lock — les modules qui importent
is_leader n'ont pas besoin de changer.
"""
import asyncio
import logging
import os
import urllib.request

from .events import log_event

logger = logging.getLogger("leader_election")

PATRONI_URL = os.getenv("PATRONI_URL", "http://patroni:8008")

# Event asyncio partage entre toutes les coroutines du meme process.
#   is_leader.is_set()     -> ce noeud est PRIMAIRE
#   not is_leader.is_set() -> ce noeud est SECONDAIRE (replica)
is_leader = asyncio.Event()


async def leader_election_loop(database_url: str):
    """Interroge Patroni en boucle pour determiner le role de ce noeud.

    - GET {PATRONI_URL}/primary : 200 = ce noeud est primary
    - 503 ou erreur = ce noeud est replica/indisponible
    - Si SQLite (dev local) : toujours primary, pas de Patroni
    """
    if not database_url.startswith("postgresql"):
        logger.info("SQLite detecte - pas de Patroni, noeud toujours PRIMAIRE")
        is_leader.set()
        return

    while True:
        try:
            req = urllib.request.Request(f"{PATRONI_URL}/primary", method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                if not is_leader.is_set():
                    logger.info("Patroni: ce noeud est maintenant PRIMAIRE")
                    log_event("leader_elected", role="primary")
                is_leader.set()
            else:
                if is_leader.is_set():
                    logger.warning("Patroni: ce noeud passe en SECONDAIRE")
                    log_event("leader_lost", role="secondary")
                is_leader.clear()
        except Exception:
            if is_leader.is_set():
                logger.warning("Patroni injoignable - ce noeud passe en SECONDAIRE")
                log_event("leader_lost", role="secondary")
            is_leader.clear()

        await asyncio.sleep(5)
