from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..clock import now as clock_now
from ..database import get_db
from ..models import User
from ..schemas import UserResponse
from ..auth import get_current_user

router = APIRouter(prefix="/api/devices", tags=["devices"])

# Global flag for heartbeat pause (toggled via /api/test/toggle-heartbeat-pause)
heartbeat_paused = False


@router.post("/register")
def register_device(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """No-op for mobile compatibility. Just returns success."""
    return {"status": "ok", "user_id": current_user.id}


@router.post("/heartbeat")
def heartbeat(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if heartbeat_paused:
        raise HTTPException(status_code=503, detail="Heartbeat en pause (test)")

    now = clock_now()
    current_user.last_heartbeat = now
    current_user.is_online = True
    db.commit()
    return {"status": "ok", "timestamp": now.isoformat()}


@router.get("/", response_model=List[UserResponse])
def list_devices(db: Session = Depends(get_db)):
    """Return list of users with their online status (replaces device list)."""
    return db.query(User).all()
