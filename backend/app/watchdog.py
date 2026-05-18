import asyncio
import logging
import uuid
from datetime import timedelta
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import SystemConfig, User
from .clock import now as clock_now
from .events import log_event
from .logging_config import correlation_id_var

logger = logging.getLogger("watchdog")

# INV-084 : fallback si la cle SystemConfig 'watchdog_timeout_seconds' est absente.
# La valeur effective est lue en DB a chaque tick (cf _run_watchdog_check).
WATCHDOG_TIMEOUT_SECONDS_DEFAULT = 60

# INV-084 (issue #75) : fallback pour la cadence de la boucle watchdog.
# Cle SystemConfig 'watchdog_tick_seconds' (decision 2026-05-12 : 2 cles
# separees de escalation_tick_seconds).
WATCHDOG_TICK_SECONDS_DEFAULT = 30.0


def _get_watchdog_tick_seconds(db: Session) -> float:
    """INV-084 : lit la période de la boucle watchdog depuis SystemConfig.

    Indépendante de `escalation_tick_seconds` (décision 2026-05-12 issue #75) :
    deux leviers séparés pour accélérer les tests et adapter la cadence en prod
    sans redéploiement. Fallback sur le default si la clé est absente.
    """
    cfg = db.query(SystemConfig).filter(
        SystemConfig.key == "watchdog_tick_seconds"
    ).first()
    if cfg is None:
        return WATCHDOG_TICK_SECONDS_DEFAULT
    try:
        return float(cfg.value)
    except (TypeError, ValueError):
        return WATCHDOG_TICK_SECONDS_DEFAULT


def _run_watchdog_check(db: Session, now) -> None:
    """Un tick de watchdog : marque offline les users dont le heartbeat
    est plus ancien que `watchdog_timeout_seconds` (lu depuis SystemConfig).

    INV-084 : la valeur est lue en DB a chaque appel (pas de cache) pour
    qu'un changement admin (POST /api/config/system) prenne effet
    immediatement, sans redemarrage.
    """
    cfg = db.query(SystemConfig).filter(
        SystemConfig.key == "watchdog_timeout_seconds"
    ).first()
    timeout_seconds = float(cfg.value) if cfg else float(WATCHDOG_TIMEOUT_SECONDS_DEFAULT)

    threshold = now - timedelta(seconds=timeout_seconds)

    users = db.query(User).filter(User.is_online == True).all()  # noqa: E712
    for user in users:
        if user.last_heartbeat and user.last_heartbeat < threshold:
            logger.warning(
                f"User {user.id} ({user.name}) missed heartbeat. "
                f"Last: {user.last_heartbeat}"
            )
            user.is_online = False
            log_event("watchdog_offline", db=db, user_id=user.id, user_name=user.name)
            db.commit()


async def watchdog_loop():
    """Background task that checks user heartbeats and marks users offline.

    N'agit que si ce nœud est primaire (advisory lock acquis).
    En secondaire, dort et retente à chaque cycle.
    """
    from .leader_election import is_leader
    while True:
        tick_seconds = WATCHDOG_TICK_SECONDS_DEFAULT
        try:
            db: Session = SessionLocal()
            try:
                # INV-084 : lecture du tick à chaque itération pour qu'un changement
                # admin (POST /api/config/system) prenne effet sans redémarrage.
                tick_seconds = _get_watchdog_tick_seconds(db)

                if is_leader.is_set():
                    correlation_id_var.set(str(uuid.uuid4()))
                    # INV-084 : _run_watchdog_check lit watchdog_timeout_seconds
                    # depuis SystemConfig a chaque appel (pas de cache).
                    _run_watchdog_check(db, clock_now())
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Watchdog error: {e}")

        await asyncio.sleep(tick_seconds)
