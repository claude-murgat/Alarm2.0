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
        if is_leader.is_set():
            correlation_id_var.set(str(uuid.uuid4()))
            try:
                db: Session = SessionLocal()
                try:
                    _run_watchdog_check(db, clock_now())
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Watchdog error: {e}")

        await asyncio.sleep(30)  # Check every 30 seconds
