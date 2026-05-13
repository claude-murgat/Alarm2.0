import asyncio
import logging
import uuid
from datetime import timedelta
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import User, SystemConfig
from .clock import now as clock_now
from .events import log_event
from .logging_config import correlation_id_var

logger = logging.getLogger("watchdog")

WATCHDOG_TIMEOUT_SECONDS = 60
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
                    now = clock_now()
                    threshold = now - timedelta(seconds=WATCHDOG_TIMEOUT_SECONDS)

                    users = db.query(User).filter(User.is_online == True).all()
                    for user in users:
                        if user.last_heartbeat and user.last_heartbeat < threshold:
                            logger.warning(
                                f"User {user.id} ({user.name}) missed heartbeat. "
                                f"Last: {user.last_heartbeat}"
                            )
                            user.is_online = False
                            log_event("watchdog_offline", db=db, user_id=user.id, user_name=user.name)
                            db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Watchdog error: {e}")

        await asyncio.sleep(tick_seconds)
