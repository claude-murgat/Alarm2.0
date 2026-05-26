"""API CD V1 — Trace des deployments.

Cf docs/CD_DESIGN.md §6 (Observabilité). Endpoints :

- POST /api/deployments/events : insert (auth X-Gateway-Key, orchestrateur uniquement)
- GET  /api/deployments/events : lecture admin (filtres node/kind/status/since)
- GET  /api/deployments/state  : agrégé pour dashboard (dernier event par nœud)

L'API distingue 2 chemins d'écriture :
  - Gateway (X-Gateway-Key) : orchestrateur, scripts CI, systemd timers
  - Aucune écriture user/admin via cette API (transparency : seul l'orchestrateur
    écrit, l'humain ne fait que lire — sinon on pollue l'historique).
"""
import json
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import get_current_admin
from ..database import get_db
from ..email_service import send_alert_email
from ..leader_election import is_leader
from ..models import DeploymentEvent, SystemConfig, User

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _check_gateway_key(x_gateway_key: Optional[str] = Header(None)):
    """Auth pour l'orchestrateur CD (sur NODE3). Même mécanisme que SMS/Calls."""
    expected = os.getenv("GATEWAY_KEY", "")
    if not expected or x_gateway_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Gateway-Key",
        )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_ALLOWED_KINDS = {
    "pull",
    "canary_start",
    "canary_promoted",
    "rollback",
    "abort",
    "manual_override",
    "emergency_promote",
    "emergency_aborted_network",
}
_ALLOWED_STATUS = {"success", "failure", "in_progress"}


class DeploymentEventIn(BaseModel):
    node: str = Field(..., description="node1 | node2 | node3")
    image: str = Field(..., description="alarm-backend | alarm-patroni")
    kind: str
    from_digest: Optional[str] = None
    to_digest: Optional[str] = None
    status: str
    actor: Optional[str] = None
    details: Optional[dict] = None  # JSON encodé côté serveur


class DeploymentEventOut(BaseModel):
    id: int
    ts: datetime
    node: str
    image: str
    kind: str
    from_digest: Optional[str]
    to_digest: Optional[str]
    status: str
    actor: Optional[str]
    details: Optional[dict]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/events", status_code=status.HTTP_201_CREATED)
def insert_event(
    event: DeploymentEventIn,
    _: None = Depends(_check_gateway_key),
    db: Session = Depends(get_db),
):
    """Insertion d'un event CD. Réservé à l'orchestrateur (gateway key).

    Validation stricte des `kind` et `status` pour éviter qu'un script bugué
    pollue la table avec des valeurs arbitraires (ce qui casserait le dashboard
    et le rollback automatique qui filtre par kind).
    """
    if event.kind not in _ALLOWED_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid kind '{event.kind}'. Allowed: {sorted(_ALLOWED_KINDS)}",
        )
    if event.status not in _ALLOWED_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{event.status}'. Allowed: {sorted(_ALLOWED_STATUS)}",
        )

    # Replicas ne peuvent pas INSERT (ReadOnlySqlTransaction). Pattern aligne sur
    # devices.py:88 (INV-043). Les scripts CD basculent vers le leader via
    # discover_leader() / GET /health role=primary.
    if not is_leader.is_set():
        raise HTTPException(status_code=503, detail="replica")

    row = DeploymentEvent(
        node=event.node,
        image=event.image,
        kind=event.kind,
        from_digest=event.from_digest,
        to_digest=event.to_digest,
        status=event.status,
        actor=event.actor,
        details=json.dumps(event.details) if event.details else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Hook notification email (cf §6 doc).
    # Envoyes uniquement pour les events significatifs : rollback, abort,
    # emergency_promote, et canary_promoted sur node1 (= deploiement complet
    # termine, dernier noeud de l'ordre canary NODE3 -> onsite-2 -> onsite-1).
    # Les pull/canary_start intermediaires sont silencieux pour eviter le bruit.
    _maybe_send_email(row, db)

    return {"id": row.id, "ts": row.ts.isoformat()}


_EMAIL_TRIGGER_KINDS = {
    "rollback",
    "abort",
    "emergency_promote",
    "emergency_aborted_network",
    "manual_override",
}


def _maybe_send_email(event: DeploymentEvent, db: Session) -> None:
    """Envoie un email pour les events CD significatifs.

    Lit `alert_email` depuis SystemConfig (cle existante, cf INV-053/080/085).
    Si la cle n'existe pas, on skip silencieusement (pas de fallback hardcode :
    le proprietaire doit avoir configure son email de contact).
    """
    should_send = event.kind in _EMAIL_TRIGGER_KINDS
    # canary_promoted UNIQUEMENT pour node1 = fin de la sequence canary 3 -> 2 -> 1
    if event.kind == "canary_promoted" and event.node == "node1":
        should_send = True

    if not should_send:
        return

    cfg = db.query(SystemConfig).filter(SystemConfig.key == "alert_email").first()
    if not cfg or not cfg.value:
        return

    subject_map = {
        "rollback": f"[CD] Rollback declenche sur {event.node} ({event.image})",
        "abort": f"[CD] Deploiement abandonne sur {event.node} ({event.image})",
        "emergency_promote": f"[URGENCE] Auto-promote bot sur {event.image}",
        "emergency_aborted_network": f"[CD] Detecteur urgence abandonne (reseau partiel)",
        "manual_override": f"[CD] Promotion :stable -> {event.to_digest or '?'}",
        "canary_promoted": f"[CD] Deploiement complet termine ({event.image})",
    }
    subject = subject_map.get(event.kind, f"[CD] Event {event.kind}")
    body_lines = [
        f"Event CD: {event.kind} / {event.status}",
        f"Node: {event.node}",
        f"Image: {event.image}",
        f"From: {event.from_digest or 'none'}",
        f"To: {event.to_digest or 'none'}",
        f"Actor: {event.actor or 'unknown'}",
        f"Time: {event.ts.isoformat()}",
    ]
    if event.details:
        body_lines.append(f"Details: {event.details}")
    body = "\n".join(body_lines)

    try:
        send_alert_email(subject=subject, body=body, to=cfg.value)
    except Exception:
        # On ne casse JAMAIS un POST d'event a cause d'un email rate.
        # L'event reste en base, c'est l'essentiel pour le dashboard et l'audit.
        pass


def _row_to_out(row: DeploymentEvent) -> DeploymentEventOut:
    return DeploymentEventOut(
        id=row.id,
        ts=row.ts,
        node=row.node,
        image=row.image,
        kind=row.kind,
        from_digest=row.from_digest,
        to_digest=row.to_digest,
        status=row.status,
        actor=row.actor,
        details=json.loads(row.details) if row.details else None,
    )


@router.get("/events")
def list_events(
    node: Optional[str] = None,
    kind: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    image: Optional[str] = None,
    since_hours: int = Query(168, ge=1, le=24 * 90),  # default 7j, max 90j
    limit: int = Query(200, ge=1, le=1000),
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Liste les events CD. Admin only."""
    q = db.query(DeploymentEvent).order_by(DeploymentEvent.ts.desc())

    cutoff = datetime.utcnow() - timedelta(hours=since_hours)
    q = q.filter(DeploymentEvent.ts >= cutoff)

    if node:
        q = q.filter(DeploymentEvent.node == node)
    if kind:
        q = q.filter(DeploymentEvent.kind == kind)
    if status_filter:
        q = q.filter(DeploymentEvent.status == status_filter)
    if image:
        q = q.filter(DeploymentEvent.image == image)

    rows = q.limit(limit).all()
    return {"events": [_row_to_out(r).dict() for r in rows], "count": len(rows)}


@router.get("/state")
def deployment_state(
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """État courant du CD : pour chaque (node, image), dernier event observé.

    Format pour dashboard simple (PR 4 minimale) ou Grafana future (V2).
    """
    # Dernier event par (node, image) — SQL groupby compatible Postgres + SQLite
    # (le CI tier 2 tourne sous SQLite).
    rows = (
        db.query(DeploymentEvent)
        .order_by(DeploymentEvent.ts.desc())
        .limit(500)  # garde-fou ; suffisant pour 3 nœuds × 2 images
        .all()
    )

    state: dict = {}
    for r in rows:
        key = f"{r.node}/{r.image}"
        if key not in state:
            state[key] = _row_to_out(r).dict()

    return {"state": state, "checked_at": datetime.utcnow().isoformat()}
