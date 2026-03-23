from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timezone
from ..database import get_db
from ..models import Device, User
from ..schemas import DeviceRegister, DeviceResponse
from ..auth import get_current_user

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.post("/register", response_model=DeviceResponse)
def register_device(
    device_data: DeviceRegister,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.query(Device).filter(Device.device_token == device_data.device_token).first()
    if existing:
        existing.user_id = current_user.id
        existing.is_online = True
        existing.last_heartbeat = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return existing

    device = Device(
        user_id=current_user.id,
        device_token=device_data.device_token,
        is_online=True,
        last_heartbeat=datetime.utcnow(),
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


@router.post("/heartbeat")
def heartbeat(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    devices = db.query(Device).filter(Device.user_id == current_user.id).all()
    now = datetime.utcnow()
    for device in devices:
        device.last_heartbeat = now
        device.is_online = True
    db.commit()
    return {"status": "ok", "timestamp": now.isoformat()}


@router.get("/", response_model=List[DeviceResponse])
def list_devices(db: Session = Depends(get_db)):
    return db.query(Device).all()
