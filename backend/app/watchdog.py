import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import Device

logger = logging.getLogger("watchdog")

WATCHDOG_TIMEOUT_SECONDS = 60


async def watchdog_loop():
    """Background task that checks device heartbeats and marks devices offline."""
    while True:
        try:
            db: Session = SessionLocal()
            try:
                now = datetime.utcnow()
                threshold = now - timedelta(seconds=WATCHDOG_TIMEOUT_SECONDS)

                devices = db.query(Device).filter(Device.is_online == True).all()
                for device in devices:
                    if device.last_heartbeat and device.last_heartbeat < threshold:
                        logger.warning(
                            f"Device {device.id} (user {device.user_id}) missed heartbeat. "
                            f"Last: {device.last_heartbeat}"
                        )
                        device.is_online = False
                        db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Watchdog error: {e}")

        await asyncio.sleep(30)  # Check every 30 seconds
