import os
import time as _time
import urllib.request
import json as _json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import List
from ..clock import now as clock_now
from ..database import get_db
from ..models import User, DeviceToken
from ..schemas import UserResponse, FcmTokenRequest, FcmTokenDeleteRequest
from ..auth import get_current_user, security, SECRET_KEY, ALGORITHM
from ..events import log_event
from ..leader_election import is_leader

router = APIRouter(prefix="/api/devices", tags=["devices"])

_PEER_URLS = [u.strip() for u in os.getenv("PEER_TEST_URLS", "").split(",") if u.strip()]


def _proxy_heartbeat_to_primary(token: str):
    """Forward le heartbeat vers un peer qui est primary (best-effort)."""
    import httpx
    for peer in _PEER_URLS:
        try:
            r = httpx.post(
                f"{peer}/api/devices/heartbeat",
                headers={"Authorization": f"Bearer {token}"},
                timeout=3.0,
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    raise HTTPException(status_code=503, detail="No primary available for heartbeat")

# Global flag for heartbeat pause (toggled via /api/test/toggle-heartbeat-pause)
heartbeat_paused = False

# Timestamp Unix (secondes) du dernier simulate-connection-loss.
# Les tokens émis AVANT cette valeur sont rejetés par le heartbeat endpoint.
# Mis à None par /api/test/reset. Permet de simuler une déconnexion robuste
# même quand une app externe (ex: émulateur Android) envoie des heartbeats.
connection_loss_time_int: int | None = None


@router.post("/register")
def register_device(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """No-op for mobile compatibility. Just returns success."""
    return {"status": "ok", "user_id": current_user.id}


@router.post("/heartbeat")
def heartbeat(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if heartbeat_paused:
        raise HTTPException(status_code=503, detail="Heartbeat en pause (test)")

    # Simulation de déconnexion : rejeter les tokens émis avant simulate-connection-loss.
    # - Tokens sans iat (anciens / app externe) : iat = -1 < connection_loss_time_int → rejet
    # - Tokens émis dans la même seconde que simulate-connection-loss : iat >= T → acceptés
    # - Tokens émis après : iat > T → acceptés
    if connection_loss_time_int is not None:
        try:
            from jose import jwt as _jose_jwt
            payload = _jose_jwt.decode(
                credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM]
            )
            iat = payload.get("iat", -1)  # -1 si absent (vieux token sans iat)
            if iat < connection_loss_time_int:
                raise HTTPException(
                    status_code=503,
                    detail="Connexion simulée perdue — reconnectez-vous pour rétablir le heartbeat",
                )
        except HTTPException:
            raise
        except Exception:
            pass  # Erreur de décodage inattendue : laisser passer (ne pas bloquer)

    # Si ce noeud est un replica, indiquer a l'app de switcher
    if not is_leader.is_set():
        raise HTTPException(status_code=503, detail="replica")

    was_offline = not current_user.is_online
    now = clock_now()
    current_user.last_heartbeat = now
    current_user.is_online = True
    db.commit()
    if was_offline:
        log_event("user_online", user_id=current_user.id, user_name=current_user.name)
    return {"status": "ok", "timestamp": now.isoformat()}


@router.post("/fcm-token")
def register_fcm_token(
    data: FcmTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enregistre ou met a jour un token FCM pour le device de l'utilisateur."""
    existing = (
        db.query(DeviceToken)
        .filter(DeviceToken.user_id == current_user.id, DeviceToken.device_id == data.device_id)
        .first()
    )
    if existing:
        existing.fcm_token = data.token
        existing.updated_at = clock_now()
    else:
        db.add(DeviceToken(
            user_id=current_user.id,
            fcm_token=data.token,
            device_id=data.device_id,
        ))
    db.commit()
    return {"status": "ok"}


@router.delete("/fcm-token")
def delete_fcm_token(
    data: FcmTokenDeleteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Supprime un token FCM (au logout)."""
    deleted = (
        db.query(DeviceToken)
        .filter(DeviceToken.user_id == current_user.id, DeviceToken.device_id == data.device_id)
        .delete()
    )
    db.commit()
    return {"status": "ok", "deleted": deleted}


@router.get("/", response_model=List[UserResponse])
def list_devices(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return list of users with their online status (replaces device list)."""
    return db.query(User).all()
