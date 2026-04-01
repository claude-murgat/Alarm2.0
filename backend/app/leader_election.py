"""
Leader election via PostgreSQL advisory lock.

Un seul nœud peut détenir le lock — c'est le nœud primaire.
Il exécute la boucle d'escalade et le watchdog.
Les nœuds secondaires restent en standby et prennent le relais
automatiquement si le primaire tombe (PostgreSQL libère le lock
dès que la connexion TCP se ferme, sans intervention manuelle).

Failover en <20s (prochain cycle d'élection = 10s).
"""
import asyncio
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

logger = logging.getLogger("leader_election")

# Clé int64 unique pour cette application — évite les collisions avec d'autres
# processus PostgreSQL qui utiliseraient aussi des advisory locks.
ADVISORY_LOCK_KEY = 7_481_293_847

# Event asyncio partagé entre toutes les coroutines du même process.
#   is_leader.is_set()  → ce nœud est PRIMAIRE (lock acquis, escalade active)
#   not is_leader.is_set() → ce nœud est SECONDAIRE (standby)
is_leader = asyncio.Event()


async def leader_election_loop(database_url: str):
    """Tente d'acquérir le lock advisory PostgreSQL en boucle.

    Comportement :
    - Si lock acquis       : set(is_leader), keepalive SELECT 1 toutes les 10s
    - Si lock non acquis   : clear(is_leader), retente dans 10s
    - Si connexion perdue  : clear(is_leader), reconnecte au prochain cycle
    - Si SQLite (dev local): is_leader toujours set, fonction se termine
    """
    # SQLite ne supporte pas les advisory locks → toujours primaire (dev sans Docker)
    if not database_url.startswith("postgresql"):
        logger.info("SQLite détecté — advisory lock désactivé, nœud toujours PRIMAIRE")
        is_leader.set()
        return

    lock_conn = None

    while True:
        try:
            # Créer une connexion dédiée (NullPool = vraie connexion PostgreSQL,
            # pas recyclée par un pool — le lock est attaché à cette session)
            if lock_conn is None:
                engine = create_engine(database_url, poolclass=NullPool)
                lock_conn = engine.connect()

            # pg_try_advisory_lock : non bloquant.
            # Retourne True si ce nœud obtient le lock, False si un autre le détient.
            result = lock_conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": ADVISORY_LOCK_KEY},
            ).scalar()
            lock_conn.commit()

            if result:
                if not is_leader.is_set():
                    logger.info(
                        "Advisory lock acquis — ce nœud est maintenant PRIMAIRE "
                        "(escalade + watchdog actifs)"
                    )
                is_leader.set()
                # Keepalive : maintient la connexion ouverte.
                # Si cette connexion se ferme, PostgreSQL libère automatiquement
                # le lock et un autre nœud peut l'acquérir.
                lock_conn.execute(text("SELECT 1")).scalar()
                lock_conn.commit()
            else:
                if is_leader.is_set():
                    logger.warning(
                        "Advisory lock non disponible — ce nœud passe en SECONDAIRE (standby)"
                    )
                is_leader.clear()

        except Exception as e:
            logger.error(f"Leader election error: {e}")
            is_leader.clear()
            if lock_conn is not None:
                try:
                    lock_conn.close()
                except Exception:
                    pass
                lock_conn = None

        await asyncio.sleep(10)
