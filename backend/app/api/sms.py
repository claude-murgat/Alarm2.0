import os
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session
from ..database import get_db
from ..models import SmsQueue
from ..clock import now as clock_now
from sqlalchemy import func
from ..escalation import contact_is_obsolete
from ..sites import default_site, requested_sites
from ..queue_claim import SMS_LEASE, claim, release, unclaimed

router = APIRouter(prefix="/internal/sms", tags=["sms-gateway"])


def _check_gateway_key(x_gateway_key: Optional[str] = Header(None)):
    """Vérifie que la clé gateway est correcte.
    Utilise Optional pour renvoyer 401 (et non 422) quand le header est absent."""
    expected = os.getenv("GATEWAY_KEY", "")
    if not expected or x_gateway_key != expected:
        raise HTTPException(status_code=401, detail="Invalid gateway key")


@router.get("/pending")
def get_pending(
    site: Optional[str] = None,
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """Retourne les SMS en attente d'envoi (non envoyés, retries < 3), sauf ceux d'une
    alarme déjà acquittée ou résolue (INV-154). INV-156 : seulement ceux du ou des sites
    demandés (`site` absent -> default_site ; ligne d'avant INV-156 sans site -> default_site).
    INV-193 : ni ceux sous bail valide d'une passerelle."""
    rows = (
        db.query(SmsQueue)
        .filter(SmsQueue.sent_at == None, SmsQueue.retries < 3, unclaimed(SmsQueue, clock_now()),
                func.coalesce(SmsQueue.site, default_site(db)).in_(requested_sites(db, site)))
        .order_by(SmsQueue.created_at)
        .all()
    )
    rows = [r for r in rows if not contact_is_obsolete(db, r)]
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


@router.post("/{sms_id}/claim")
def claim_sms(
    sms_id: int,
    payload: Optional[dict] = None,
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """INV-193 : reserve le SMS pour `gateway_id` avant AT+CMGS ; `claimed: false` -> la passerelle le saute."""
    return claim(db, SmsQueue, sms_id, payload, SMS_LEASE, SmsQueue.sent_at == None, SmsQueue.retries < 3)


@router.post("/{sms_id}/sent")
def mark_sent(
    sms_id: int,
    payload: Optional[dict] = None,
    db: Session = Depends(get_db),
    _: None = Depends(_check_gateway_key),
):
    """Marque un SMS comme envoyé. INV-193 : corps `{gateway_id}` facultatif (passerelle d'avant : sans corps)."""
    row = db.query(SmsQueue).filter(SmsQueue.id == sms_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="SMS not found")
    release(row, payload)
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
    release(row, payload)  # INV-193
    row.retries += 1
    row.error = payload.get("error", "unknown")
    db.commit()
    return {"id": row.id, "retries": row.retries, "error": row.error}
