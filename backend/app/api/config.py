from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import EscalationConfig, SystemConfig
from ..schemas import EscalationConfigCreate, EscalationConfigResponse, SystemConfigUpdate

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/escalation", response_model=List[EscalationConfigResponse])
def get_escalation_config(db: Session = Depends(get_db)):
    return db.query(EscalationConfig).order_by(EscalationConfig.position).all()


@router.post("/escalation", response_model=EscalationConfigResponse)
def add_escalation_config(config: EscalationConfigCreate, db: Session = Depends(get_db)):
    existing = db.query(EscalationConfig).filter(EscalationConfig.position == config.position).first()
    if existing:
        existing.user_id = config.user_id
        existing.delay_minutes = config.delay_minutes
        db.commit()
        db.refresh(existing)
        return existing

    ec = EscalationConfig(
        position=config.position,
        user_id=config.user_id,
        delay_minutes=config.delay_minutes,
    )
    db.add(ec)
    db.commit()
    db.refresh(ec)
    return ec


@router.delete("/escalation/{config_id}")
def delete_escalation_config(config_id: int, db: Session = Depends(get_db)):
    ec = db.query(EscalationConfig).filter(EscalationConfig.id == config_id).first()
    if not ec:
        raise HTTPException(status_code=404, detail="Config not found")
    db.delete(ec)
    db.commit()
    return {"status": "deleted"}


@router.get("/system")
def get_system_config(db: Session = Depends(get_db)):
    configs = db.query(SystemConfig).all()
    return {c.key: c.value for c in configs}


@router.post("/system")
def set_system_config(config: SystemConfigUpdate, db: Session = Depends(get_db)):
    existing = db.query(SystemConfig).filter(SystemConfig.key == config.key).first()
    if existing:
        existing.value = config.value
    else:
        existing = SystemConfig(key=config.key, value=config.value)
        db.add(existing)
    db.commit()
    return {"status": "ok"}
