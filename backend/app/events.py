import json
import logging
from datetime import datetime, timezone

from .logging_config import correlation_id_var

logger = logging.getLogger("event")

AUDITABLE_EVENTS = {
    "alarm_created", "alarm_acknowledged", "alarm_resolved", "alarm_escalated",
    "user_login", "user_login_failed", "config_changed",
    "user_created", "user_deleted", "escalation_timeout", "watchdog_offline",
}


def log_event(event_type: str, db=None, **kwargs):
    """Emit a structured [EVENT] log line and persist to audit_events if auditable.

    Audit persistence is best-effort: failures (e.g. on read-only replicas)
    are silently logged at DEBUG level and never break the caller.

    Args:
        event_type: Type of event (e.g. "alarm_created").
        db: Ignored for backward compat — audit always uses its own session.
        **kwargs: Extra data (alarm_id, user_id, etc.) stored in details.
    """
    payload = {"type": event_type, "ts": datetime.now(timezone.utc).isoformat(), **kwargs}
    logger.info(f"[EVENT] {json.dumps(payload)}")

    if event_type in AUDITABLE_EVENTS:
        try:
            _persist_audit_event(event_type, **kwargs)
        except Exception:
            logger.debug(f"Audit persist failed for {event_type}", exc_info=True)


def _persist_audit_event(event_type: str, **kwargs):
    """Insert an AuditEvent row using a dedicated session."""
    from .models import AuditEvent
    from .database import SessionLocal

    corr_id = correlation_id_var.get()
    alarm_id = kwargs.pop("alarm_id", None)
    user_id = kwargs.pop("user_id", None)
    if user_id is None:
        user_id = kwargs.pop("by_user", None)

    details_str = json.dumps(kwargs, default=str) if kwargs else None

    event = AuditEvent(
        alarm_id=alarm_id,
        event_type=event_type,
        user_id=user_id,
        timestamp=datetime.now(timezone.utc),
        details=details_str,
        correlation_id=corr_id,
    )

    tmp_db = SessionLocal()
    try:
        tmp_db.add(event)
        tmp_db.commit()
    finally:
        tmp_db.close()
