"""INV-193 : un SMS ou un appel en file n'est envoye que par la passerelle qui l'a reserve.

Plusieurs passerelles avec modem lisent la meme file d'un site ; chacune reserve l'element juste
avant `ATD` / `AT+CMGS` (port AT tenu). Bail : appel 3 min (pire cas ~2 min 20, repli TTS compris),
SMS 1 min 30 (pire cas modem 43 s + compte rendu) ; detail dans tests/INVARIANTS.md.
"""
import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import or_

from .clock import now as clock_now
from .escalation import contact_is_obsolete

logger = logging.getLogger("queue_claim")

CALL_LEASE = timedelta(minutes=3)
SMS_LEASE = timedelta(seconds=90)


def unclaimed(model, now):
    """Jamais reserve, reservation levee, ou bail expire."""
    return or_(model.claimed_by == None, model.claimed_until < now)


def claim(db, model, item_id: int, payload, lease: timedelta, *todo) -> dict:
    """UPDATE conditionnel : une seule reservation gagne, y compris en concurrence (PostgreSQL
    reevalue la condition apres le verrou de ligne). `todo` = conditions de /pending."""
    gateway_id = str((payload or {}).get("gateway_id") or "").strip()
    if not gateway_id:
        raise HTTPException(status_code=400, detail="gateway_id required")
    row = db.get(model, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    now = clock_now()
    won = not contact_is_obsolete(db, row) and db.query(model).filter(
        model.id == item_id, *todo, unclaimed(model, now),
    ).update({"claimed_by": gateway_id, "claimed_until": now + lease}, synchronize_session=False) == 1
    db.commit()
    db.refresh(row)
    if not won:
        logger.debug(f"{model.__tablename__} {item_id} : reservation de {gateway_id} perdue "
                     f"(titulaire {row.claimed_by})")
    return {"id": item_id, "claimed": won, "claimed_by": row.claimed_by,
            "claimed_until": row.claimed_until.isoformat() if row.claimed_until else None}


def release(row, payload) -> None:
    """Compte rendu : leve la reservation de son titulaire. D'une autre passerelle (ou d'une
    passerelle d'avant INV-193, sans gateway_id) : accepte, reservation gardee, WARNING."""
    gateway_id = (payload or {}).get("gateway_id")
    if row.claimed_by == gateway_id:
        row.claimed_by = row.claimed_until = None
    elif row.claimed_by:
        logger.warning(f"{row.__tablename__} {row.id} : compte rendu de {gateway_id or 'passerelle sans gateway_id'} "
                       f"alors que {row.claimed_by} le tient : double envoi probable (INV-193)")
