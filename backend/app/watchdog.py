import asyncio
import logging
import uuid
from datetime import timedelta
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import User
from .clock import now as clock_now
from .events import log_event
from .logging_config import correlation_id_var

logger = logging.getLogger("watchdog")

WATCHDOG_TIMEOUT_SECONDS = 60


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

        await asyncio.sleep(30)  # Check every 30 seconds
