from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import EscalationConfig, SystemConfig, User
from ..schemas import EscalationConfigCreate, EscalationConfigResponse, SystemConfigUpdate
from ..auth import get_current_user, get_current_admin
from ..events import log_event
from ..fcm_service import send_fcm_to_user

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/escalation", response_model=List[EscalationConfigResponse])
def get_escalation_config(db: Session = Depends(get_db)):
    return db.query(EscalationConfig).order_by(EscalationConfig.position).all()


@router.post("/escalation", response_model=EscalationConfigResponse)
def add_escalation_config(
    config: EscalationConfigCreate,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
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
def delete_escalation_config(
    config_id: int,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
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
def set_escalation_delay(
    payload: dict,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
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
    log_event("config_changed", db=db, user_id=current_user.id, key="escalation_delay_minutes", value=str(minutes))
    return {"minutes": minutes}


@router.get("/sms-call-delay")
def get_sms_call_delay(db: Session = Depends(get_db)):
    """Retourne le delai SMS/appel apres notification en minutes."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "sms_call_delay_minutes").first()
    minutes = float(config.value) if config else 2.0
    return {"delay_minutes": minutes}


@router.post("/sms-call-delay")
def set_sms_call_delay(
    payload: dict,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Met a jour le delai SMS/appel (1-30 minutes)."""
    minutes = payload.get("delay_minutes")
    if minutes is None or not isinstance(minutes, (int, float)):
        raise HTTPException(status_code=422, detail="delay_minutes requis (nombre)")
    if minutes < 1 or minutes > 30:
        raise HTTPException(status_code=422, detail="Le delai doit etre entre 1 et 30 minutes")

    config = db.query(SystemConfig).filter(SystemConfig.key == "sms_call_delay_minutes").first()
    if config:
        config.value = str(int(minutes))
    else:
        db.add(SystemConfig(key="sms_call_delay_minutes", value=str(int(minutes))))
    db.commit()
    log_event("config_changed", db=db, user_id=current_user.id, key="sms_call_delay_minutes", value=str(int(minutes)))
    return {"delay_minutes": int(minutes)}


@router.post("/escalation/bulk")
def save_escalation_chain_bulk(
    payload: dict,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
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
    log_event("config_changed", db=db, user_id=current_user.id, key="escalation_chain", value=str(user_ids))

    # Notifier tous les utilisateurs de la chaine par push FCM
    _notify_escalation_change(db, user_ids)

    return db.query(EscalationConfig).order_by(EscalationConfig.position).all()


@router.get("/system")
def get_system_config(db: Session = Depends(get_db)):
    configs = db.query(SystemConfig).all()
    return {c.key: c.value for c in configs}


@router.post("/system")
def set_system_config(
    config: SystemConfigUpdate,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(SystemConfig).filter(SystemConfig.key == config.key).first()
    if existing:
        existing.value = config.value
    else:
        existing = SystemConfig(key=config.key, value=config.value)
        db.add(existing)
    db.commit()
    log_event("config_changed", db=db, user_id=current_user.id, key=config.key, value=config.value)
    return {"status": "ok"}


def _notify_escalation_change(db: Session, user_ids: list[int]):
    """Envoie un push FCM a chaque utilisateur de la chaine pour l'informer
    de sa nouvelle position. Type 'escalation_update' pour traitement cote app."""
    import logging
    logger = logging.getLogger("config")

    for i, uid in enumerate(user_ids):
        position = i + 1
        is_oncall = position == 1
        user = db.query(User).filter(User.id == uid).first()
        name = user.name if user else "?"

        if is_oncall:
            title = "Vous etes de garde"
            body = f"Position #{position} dans la chaine d'escalade"
        else:
            title = "Chaine d'escalade modifiee"
            body = f"Vous etes en position #{position}"

        try:
            send_fcm_to_user(
                db, uid, title, body,
                data={
                    "type": "escalation_update",
                    "escalation_position": str(position),
                    "is_oncall": str(is_oncall).lower(),
                }
            )
            logger.info(f"Escalation push sent to {name} (position {position})")
        except Exception as e:
            logger.error(f"Escalation push failed for {name}: {e}")
