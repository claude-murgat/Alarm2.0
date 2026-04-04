import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("event")


def log_event(event_type: str, **kwargs):
    """Emit a structured [EVENT] log line parseable by the test script.
    Format: [EVENT] {"type": "...", "ts": "...", ...}
    """
    payload = {"type": event_type, "ts": datetime.now(timezone.utc).isoformat(), **kwargs}
    logger.info(f"[EVENT] {json.dumps(payload)}")
