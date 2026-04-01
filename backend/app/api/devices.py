import time as _time
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import List
from ..clock import now as clock_now
from ..database import get_db
from ..models import User
from ..schemas import UserResponse
from ..auth import get_current_user, security, SECRET_KEY, ALGORITHM

router = APIRouter(prefix="/api/devices", tags=["devices"])

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

    now = clock_now()
    current_user.last_heartbeat = now
    current_user.is_online = True
    db.commit()
    return {"status": "ok", "timestamp": now.isoformat()}


@router.get("/", response_model=List[UserResponse])
def list_devices(db: Session = Depends(get_db)):
    """Return list of users with their online status (replaces device list)."""
    return db.query(User).all()
