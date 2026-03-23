import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import Alarm, EscalationConfig

logger = logging.getLogger("escalation")


async def escalation_loop():
    """Background task that checks for unacknowledged alarms and escalates them."""
    while True:
        try:
            db: Session = SessionLocal()
            try:
                now = datetime.utcnow()
                active_alarms = (
                    db.query(Alarm)
                    .filter(Alarm.status.in_(["active", "escalated"]))
                    .all()
                )

                escalation_chain = (
                    db.query(EscalationConfig)
                    .order_by(EscalationConfig.position)
                    .all()
                )

                for alarm in active_alarms:
                    if not escalation_chain:
                        continue

                    # Find current position in escalation chain
                    current_position = -1
                    current_delay = 15.0  # default
                    for ec in escalation_chain:
                        if ec.user_id == alarm.assigned_user_id:
                            current_position = ec.position
                            current_delay = ec.delay_minutes
                            break

                    # Check if enough time has passed
                    elapsed = (now - alarm.created_at).total_seconds() / 60.0
                    escalation_threshold = current_delay * (alarm.escalation_count + 1)

                    if elapsed >= escalation_threshold:
                        # Find next user in chain
                        next_user = None
                        for ec in escalation_chain:
                            if ec.position > current_position:
                                next_user = ec
                                break

                        if next_user and next_user.user_id != alarm.assigned_user_id:
                            logger.info(
                                f"Escalating alarm {alarm.id} from user {alarm.assigned_user_id} "
                                f"to user {next_user.user_id} (position {next_user.position})"
                            )
                            alarm.assigned_user_id = next_user.user_id
                            alarm.status = "escalated"
                            alarm.escalation_count += 1
                            db.commit()

            finally:
                db.close()
        except Exception as e:
            logger.error(f"Escalation error: {e}")

        await asyncio.sleep(10)  # Check every 10 seconds
