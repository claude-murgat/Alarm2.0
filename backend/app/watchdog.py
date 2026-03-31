import asyncio
import logging
from datetime import timedelta
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import User
from .clock import now as clock_now

logger = logging.getLogger("watchdog")

WATCHDOG_TIMEOUT_SECONDS = 60


async def watchdog_loop():
    """Background task that checks user heartbeats and marks users offline."""
    while True:
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
                        db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Watchdog error: {e}")

        await asyncio.sleep(30)  # Check every 30 seconds
