"""INV-176..179 : panne controlee — demandes de l'onglet Tests, executees par scripts/alarm-chaos.sh (NODE3).

- POST / GET / DELETE /api/chaos : admin (les ecritures web sont relayees au leader par replica_proxy).
- GET /internal/chaos/next, POST /internal/chaos/{id}/events : runner, cle passerelle, leader seul
  (503 « replica », motif de api/deployments.py ; /internal/* n'est pas relaye, le runner decouvre le leader).
- GET /internal/chaos/{id}/abort : runner pendant la fenetre (lecture seule) — restaurer tout de suite ?
"""
import json
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import get_current_admin
from ..database import get_db
from ..events import log_event
from ..leader_election import is_leader
from ..logic.chaos import check_request, expand_target, is_stale
from ..models import Alarm, AuditEvent, ChaosRequest, User
from ..quorum_monitor import _patroni_cluster
from ..sites import etcd_voters, node_locations
from .deployments import _check_gateway_key

router = APIRouter(tags=["chaos"])
UNFINISHED = ("pending", "claimed", "running")
ALARM_BUSY = ("active", "escalated", "acknowledged")


class ChaosIn(BaseModel):
    nodes: str  # "node4,node5" ou "site:beaufort"
    mode: Literal["stop", "netcut"] = "stop"  # netcut : coupure du tunnel wg0 du noeud (helper root alarm-chaos-net)
    minutes: int


class ChaosEventIn(BaseModel):
    kind: Literal["started", "leader_changed", "early_restore", "restored", "failed"]
    details: dict = {}
    ts: Optional[datetime] = None


def _cluster():
    try:
        return _patroni_cluster()
    except Exception:
        return None


def _busy_alarms(db: Session) -> int:
    return db.query(Alarm).filter(Alarm.status.in_(ALARM_BUSY)).count()


def _refusal(db: Session, nodes, mode, minutes, pending):
    return check_request(nodes, mode, minutes, _cluster(), node_locations(db), etcd_voters(db), _busy_alarms(db), pending)


def _append(row: ChaosRequest, kind: str, details: dict, ts: Optional[datetime] = None) -> datetime:
    ts = ts or datetime.utcnow()
    events = json.loads(row.events or "[]")
    events.append({"kind": kind, "ts": ts.isoformat(), "details": details})
    row.events = json.dumps(events)
    return ts


def _fail(row: ChaosRequest, reason: str):
    row.status = "failed"
    row.restored_at = _append(row, "failed", {"reason": reason})
    log_event("chaos_failed", chaos_id=row.id, nodes=row.nodes, reason=reason)


def _expire_stale(db: Session):
    """Ecrit : leader seul (le PG d'un replica est en lecture seule), jamais depuis un GET (INV-176)."""
    if not is_leader.is_set():
        return
    now = datetime.utcnow()
    for row in db.query(ChaosRequest).filter(ChaosRequest.status.in_(UNFINISHED)):
        if is_stale(row.status, row.minutes, len(row.nodes.split(",")), row.created_at, row.claimed_at, row.started_at, now):
            _fail(row, "timeout")
    db.commit()


def _alerts(db: Session, row: ChaosRequest) -> list:
    """Ce que le systeme a fait pendant la panne : AuditEvent de la fenetre, hors chaos_*."""
    if not row.started_at:
        return []
    q = (db.query(AuditEvent)
         .filter(AuditEvent.timestamp >= row.started_at,
                 AuditEvent.timestamp <= (row.restored_at or datetime.utcnow()),
                 ~AuditEvent.event_type.startswith("chaos_", autoescape=True))
         .order_by(AuditEvent.id))
    return [{"event_type": a.event_type, "timestamp": a.timestamp.isoformat(), "details": a.details} for a in q]


def _out(row: ChaosRequest, alerts: Optional[list] = None) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    out = {"id": row.id, "created_at": iso(row.created_at), "requested_by": row.requested_by,
           "nodes": row.nodes.split(","), "mode": row.mode, "minutes": row.minutes, "status": row.status,
           "claimed_at": iso(row.claimed_at), "started_at": iso(row.started_at), "restored_at": iso(row.restored_at),
           "cancel_requested": bool(row.cancel_requested), "events": json.loads(row.events or "[]")}
    if alerts is not None:
        out["alerts"] = alerts
    return out


def _get(db: Session, chaos_id: int) -> ChaosRequest:
    row = db.get(ChaosRequest, chaos_id)
    if not row:
        raise HTTPException(status_code=404, detail="test inconnu")
    return row


def _leader_only():
    if not is_leader.is_set():
        raise HTTPException(status_code=503, detail="replica")


@router.post("/api/chaos", status_code=201)
def request_chaos(body: ChaosIn, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    _expire_stale(db)
    nodes = expand_target(body.nodes, node_locations(db))
    pending = db.query(ChaosRequest).filter(ChaosRequest.status.in_(UNFINISHED)).count()
    reason = _refusal(db, nodes, body.mode, body.minutes, pending)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    row = ChaosRequest(requested_by=admin.id, nodes=",".join(nodes), mode=body.mode, minutes=body.minutes)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:  # ux_chaos_one_unfinished : un POST simultane a ecrit le sien d'abord
        db.rollback()
        raise HTTPException(status_code=409, detail="un test est déjà en cours")
    db.refresh(row)
    log_event("chaos_requested", chaos_id=row.id, nodes=row.nodes, mode=row.mode, minutes=row.minutes, by_user=admin.id)
    return _out(row)


@router.get("/api/chaos")
def history(_admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows =db.query(ChaosRequest).order_by(ChaosRequest.id.desc()).limit(10).all()
    return [_out(r, _alerts(db, r)) for r in rows]


@router.delete("/api/chaos/{chaos_id}")
def cancel(chaos_id: int, admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """pending -> cancelled ; claimed/running -> cancel_requested (« Restaurer maintenant », lu par le runner).
    UPDATE conditionnels : contre un claim simultane, un seul des deux gagne."""
    row = _get(db, chaos_id)
    q = db.query(ChaosRequest).filter(ChaosRequest.id == chaos_id)
    if q.filter(ChaosRequest.status == "pending").update({"status": "cancelled"}, synchronize_session=False):
        was = "pending"
    elif q.filter(ChaosRequest.status.in_(("claimed", "running"))).update({"cancel_requested": True}, synchronize_session=False):
        was = None
    else:
        raise HTTPException(status_code=409, detail=f"test {row.status} : annulation impossible")
    db.commit()
    db.refresh(row)
    log_event("chaos_cancelled", chaos_id=row.id, nodes=row.nodes, status=was or row.status, by_user=admin.id)
    return _out(row)


@router.get("/internal/chaos/next")
def next_request(_: None = Depends(_check_gateway_key), db: Session = Depends(get_db)):
    """Runner : la plus ancienne demande pending, re-verifiee au moment d'agir (refus -> failed) ; null sinon."""
    _leader_only()
    _expire_stale(db)
    row = db.query(ChaosRequest).filter(ChaosRequest.status == "pending").order_by(ChaosRequest.id).first()
    if not row:
        return None
    reason = _refusal(db, row.nodes.split(","), row.mode, row.minutes, pending=0)
    if reason:
        _fail(row, reason)
        db.commit()
        return None
    # Conditionnel : une demande annulee pendant le controle n'est jamais reclamee.
    claimed = (db.query(ChaosRequest).filter(ChaosRequest.id == row.id, ChaosRequest.status == "pending")
               .update({"status": "claimed", "claimed_at": datetime.utcnow()}, synchronize_session=False))
    db.commit()
    if not claimed:
        return None
    db.refresh(row)
    return {**_out(row), "voters": etcd_voters(db)}  # sonde etcd du runner (INV-177)


@router.get("/internal/chaos/{chaos_id}/abort")
def abort(chaos_id: int, _: None = Depends(_check_gateway_key), db: Session = Depends(get_db)):
    """Runner, toutes les 10 s pendant la fenetre : raison de restaurer tout de suite, sinon null."""
    if _get(db, chaos_id).cancel_requested:
        return {"abort": "annulé par l'admin"}
    return {"abort": "alarme en cours" if _busy_alarms(db) else None}


@router.post("/internal/chaos/{chaos_id}/events")
def add_event(chaos_id: int, ev: ChaosEventIn, _: None = Depends(_check_gateway_key), db: Session = Depends(get_db)):
    _leader_only()
    row = _get(db, chaos_id)
    ts = ev.ts.astimezone(timezone.utc).replace(tzinfo=None) if ev.ts and ev.ts.tzinfo else ev.ts
    ts = _append(row, ev.kind, ev.details, ts)
    if row.status not in UNFINISHED:  # terminee (ex. restored apres l'expiration) : evenement garde, rien d'autre (INV-179)
        db.commit()
        return _out(row)
    if ev.kind == "started":
        row.status, row.started_at = "running", ts
    elif ev.kind in ("restored", "failed"):
        row.status, row.restored_at = ev.kind, ts
    if ev.kind != "leader_changed":
        extra = {k: v for k, v in ev.details.items() if k not in ("event_type", "db", "chaos_id", "nodes")}
        log_event(f"chaos_{ev.kind}", chaos_id=row.id, nodes=row.nodes, **extra)
    db.commit()
    return _out(row)
