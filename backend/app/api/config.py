from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import EscalationConfig, SystemConfig
from ..schemas import EscalationConfigCreate, EscalationConfigResponse, SystemConfigUpdate
from ..models import User

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


@router.get("/escalation-delay")
def get_escalation_delay(db: Session = Depends(get_db)):
    """Retourne le delai global d'escalade en minutes."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "escalation_delay_minutes").first()
    minutes = float(config.value) if config else 15.0
    return {"minutes": minutes}


@router.post("/escalation-delay")
def set_escalation_delay(payload: dict, db: Session = Depends(get_db)):
    """Met a jour le delai global d'escalade (1-60 minutes)."""
    minutes = payload.get("minutes")
    if minutes is None or not isinstance(minutes, (int, float)):
        raise HTTPException(status_code=422, detail="minutes requis (nombre)")
    if minutes < 1 or minutes > 60:
        raise HTTPException(status_code=422, detail="Le delai doit etre entre 1 et 60 minutes")

    config = db.query(SystemConfig).filter(SystemConfig.key == "escalation_delay_minutes").first()
    if config:
        config.value = str(minutes)
    else:
        db.add(SystemConfig(key="escalation_delay_minutes", value=str(minutes)))
    db.commit()
    return {"minutes": minutes}


@router.post("/escalation/bulk")
def save_escalation_chain_bulk(payload: dict, db: Session = Depends(get_db)):
    """Remplace toute la chaine d'escalade. Body: {"user_ids": [2, 3, 1]}"""
    user_ids = payload.get("user_ids", [])
    if not user_ids:
        raise HTTPException(status_code=422, detail="La chaine ne peut pas etre vide")
    if len(user_ids) != len(set(user_ids)):
        raise HTTPException(status_code=422, detail="Un utilisateur ne peut pas apparaitre deux fois")

    # Verifier que tous les user_ids existent
    for uid in user_ids:
        if not db.query(User).filter(User.id == uid).first():
            raise HTTPException(status_code=422, detail=f"Utilisateur {uid} introuvable")

    # Supprimer l'ancienne chaine
    db.query(EscalationConfig).delete()

    # Creer la nouvelle chaine avec positions auto-numerotees
    for i, uid in enumerate(user_ids):
        db.add(EscalationConfig(position=i + 1, user_id=uid, delay_minutes=15.0))
    db.commit()

    return db.query(EscalationConfig).order_by(EscalationConfig.position).all()


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
