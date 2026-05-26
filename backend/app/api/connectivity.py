"""INV-056 : endpoints lecture du tracking online/offline.

- GET /api/users/{user_id}/connectivity-history?days=<N> : paginé décroissant.
- GET /api/stats/connectivity?days=<N>                  : agrégat par user
  (nombre de transitions, durée offline cumulée, % uptime sur la fenêtre).

Auth admin uniquement (les opérateurs n'ont pas à voir l'historique des autres).
"""
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..clock import now as clock_now
from ..database import get_db
from ..models import ConnectivityEvent, User

router = APIRouter(prefix="/api", tags=["connectivity"])


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only (INV-056)")


@router.get("/users/{user_id}/connectivity-history")
def user_connectivity_history(
    user_id: int,
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Liste paginée décroissante des events de connectivité d'un user.

    Réponse :
        {
          "user_id": int,
          "user_name": str,
          "days": int,
          "events": [{"id": int, "event": str, "ts": isoformat}, ...]
        }
    """
    _require_admin(current_user)

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    since = clock_now() - timedelta(days=days)
    events = (
        db.query(ConnectivityEvent)
        .filter(ConnectivityEvent.user_id == user_id)
        .filter(ConnectivityEvent.ts >= since)
        .order_by(desc(ConnectivityEvent.ts))
        .all()
    )
    return {
        "user_id": user.id,
        "user_name": user.name,
        "days": days,
        "events": [
            {"id": e.id, "event": e.event, "ts": e.ts.isoformat()}
            for e in events
        ],
    }


@router.get("/stats/connectivity")
def stats_connectivity(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agrégat de disponibilité sur la fenêtre `days`.

    Pour chaque user :
      - `transitions_offline` : nombre de `went_offline` sur la fenêtre.
      - `total_offline_seconds` : durée cumulée offline (sommes des intervalles
        `went_offline -> went_online`).
      - `uptime_percent` : 100 * (1 - total_offline_seconds / window_seconds).

    Notes :
    - Si la fenêtre commence dans une période offline (event `went_offline`
      antérieur à `since` mais pas de `went_online` correspondant après), on
      considère l'offline comme commençant à `since`.
    - Si la fenêtre se termine dans une période offline (dernier event = offline),
      on considère que la période s'étend jusqu'à `now`.
    """
    _require_admin(current_user)

    now = clock_now()
    since = now - timedelta(days=days)
    window_seconds = (now - since).total_seconds()

    users = db.query(User).all()
    out = []
    for user in users:
        events = (
            db.query(ConnectivityEvent)
            .filter(ConnectivityEvent.user_id == user.id)
            .filter(ConnectivityEvent.ts >= since)
            .order_by(ConnectivityEvent.ts.asc())
            .all()
        )

        # Détermine l'état initial (au début de la fenêtre) : on regarde l'event
        # immédiatement antérieur, s'il existe.
        prev_event = (
            db.query(ConnectivityEvent)
            .filter(ConnectivityEvent.user_id == user.id)
            .filter(ConnectivityEvent.ts < since)
            .order_by(desc(ConnectivityEvent.ts))
            .first()
        )
        # Par défaut on suppose online au début (seed initial des users).
        currently_offline_since: Optional[object] = None
        if prev_event is not None and prev_event.event == "went_offline":
            currently_offline_since = since

        total_offline_seconds = 0.0
        transitions_offline = 0
        for e in events:
            if e.event == "went_offline":
                if currently_offline_since is None:
                    currently_offline_since = e.ts
                    transitions_offline += 1
            elif e.event == "went_online":
                if currently_offline_since is not None:
                    total_offline_seconds += (
                        e.ts - currently_offline_since
                    ).total_seconds()
                    currently_offline_since = None

        # Si l'on est encore offline à `now`, comptabiliser jusqu'à maintenant.
        if currently_offline_since is not None:
            total_offline_seconds += (now - currently_offline_since).total_seconds()

        uptime_percent = (
            100.0 * (1.0 - (total_offline_seconds / window_seconds))
            if window_seconds > 0 else 100.0
        )
        # Clamp [0, 100] pour les bords (offline avant since débordant légèrement).
        uptime_percent = max(0.0, min(100.0, uptime_percent))

        out.append({
            "user_id": user.id,
            "user_name": user.name,
            "transitions_offline": transitions_offline,
            "total_offline_seconds": int(total_offline_seconds),
            "uptime_percent": round(uptime_percent, 2),
        })

    return {
        "days": days,
        "window_start": since.isoformat(),
        "window_end": now.isoformat(),
        "users": out,
    }
