import os
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import SmsQueue
from ..clock import now as clock_now

router = APIRouter(prefix="/internal/sms", tags=["sms-gateway"])


def _check_gateway_key(x_gateway_key: Optional[str] = Header(None)):
    """Vérifie que la clé gateway est correcte.
    Utilise Optional pour renvoyer 401 (et non 422) quand le header est absent."""
    expected = os.getenv("GATEWAY_KEY", "")
    if not expected or x_gateway_key != expected:
        raise HTTPException(status_code=401, detail="Invalid gateway key")


@router.get("/pending")
def get_pending(
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """Retourne les SMS en attente d'envoi (non envoyés, retries < 3)."""
    rows = (
        db.query(SmsQueue)
        .filter(SmsQueue.sent_at == None, SmsQueue.retries < 3)
        .order_by(SmsQueue.created_at)
        .all()
    )
    return [
        {
            "id": r.id,
            "to_number": r.to_number,
            "body": r.body,
            "retries": r.retries,
            "created_at": str(r.created_at),
        }
        for r in rows
    ]


@router.post("/{sms_id}/sent")
def mark_sent(
    sms_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """Marque un SMS comme envoyé."""
    row = db.query(SmsQueue).filter(SmsQueue.id == sms_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="SMS not found")
    row.sent_at = clock_now()
    db.commit()
    return {"status": "ok", "id": sms_id}


@router.post("/{sms_id}/error")
def mark_error(
    sms_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """Incrémente le compteur d'erreurs et enregistre le message d'erreur."""
    row = db.query(SmsQueue).filter(SmsQueue.id == sms_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="SMS not found")
    row.retries += 1
    row.error = payload.get("error", "unknown")
    db.commit()
    return {"id": row.id, "retries": row.retries, "error": row.error}
