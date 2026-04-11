import json
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import AuditEvent, User
from ..auth import get_current_admin

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/")
def list_audit_events(
    alarm_id: int | None = None,
    event_type: str | None = None,
    user_id: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """List audit events with optional filters. Admin only."""
    q = db.query(AuditEvent).order_by(AuditEvent.timestamp.desc())

    if alarm_id is not None:
        q = q.filter(AuditEvent.alarm_id == alarm_id)
    if event_type is not None:
        q = q.filter(AuditEvent.event_type == event_type)
    if user_id is not None:
        q = q.filter(AuditEvent.user_id == user_id)
    if start_date is not None:
        q = q.filter(AuditEvent.timestamp >= start_date)
    if end_date is not None:
        q = q.filter(AuditEvent.timestamp <= end_date)

    total = q.count()
    offset = (page - 1) * page_size
    events = q.offset(offset).limit(page_size).all()

    data = []
    for e in events:
        details = None
        if e.details:
            try:
                details = json.loads(e.details)
            except (json.JSONDecodeError, TypeError):
                details = e.details
        data.append({
            "id": e.id,
            "alarm_id": e.alarm_id,
            "event_type": e.event_type,
            "user_id": e.user_id,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "details": details,
            "correlation_id": e.correlation_id,
        })

    response = JSONResponse(content=data)
    response.headers["X-Total-Count"] = str(total)
    return response
