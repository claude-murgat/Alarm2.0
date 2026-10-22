import os
import time as _time
import urllib.request
import json as _json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import or_
from sqlalchemy.orm import Session
from typing import List
from ..clock import now as clock_now
from ..database import get_db
from ..models import ConnectivityEvent, User, DeviceToken
from ..schemas import UserResponse, FcmTokenRequest, FcmTokenDeleteRequest
from datetime import timedelta
from ..auth import get_current_user, get_current_admin, security, SECRET_KEY, ALGORITHM
from ..events import log_event
from ..leader_election import is_leader

router = APIRouter(prefix="/api/devices", tags=["devices"])

_PEER_URLS = [u.strip() for u in os.getenv("PEER_TEST_URLS", "").split(",") if u.strip()]

# INV-043 (révisé 2026-06-17) : un replica FORWARDE le heartbeat au leader (via
# WireGuard) au lieu de renvoyer 503 — sur TOUS les nœuds (défaut true).
# Raison : à terme, n'importe quel nœud peut être joignable depuis l'extérieur
# (pas seulement le cloud). Un téléphone externe qui tombe sur un replica doit
# pouvoir heartbeat même si les autres nœuds lui sont injoignables (LAN privé).
# Si le cloud tombe et qu'un onsite devient le seul point d'entrée externe, il
# doit relayer aussi → règle uniforme, pas de nœud privilégié.
# 503 n'est plus renvoyé que si AUCUN leader n'est joignable (panne cluster).
# Flag conservé comme kill-switch (HEARTBEAT_PROXY_ON_REPLICA=false → ancien
# comportement 503, utilisé par les tests pour vérifier les deux chemins).
_PROXY_ON_REPLICA = os.getenv("HEARTBEAT_PROXY_ON_REPLICA", "true").lower() == "true"


def _proxy_heartbeat_to_primary(token: str):
    """Forward le heartbeat vers un peer qui est primary (best-effort).

    Le header `X-Heartbeat-Proxied` est l'anti-boucle : un peer qui reçoit un
    heartbeat déjà proxifié ne le re-proxifie PAS (cf endpoint heartbeat) — il
    l'exécute s'il est leader, ou renvoie 503 sinon. Évite replica→replica→...
    si plusieurs nœuds avaient le flag par erreur.
    """
    import httpx
    for peer in _PEER_URLS:
        try:
            r = httpx.post(
                f"{peer}/api/devices/heartbeat",
                headers={"Authorization": f"Bearer {token}", "X-Heartbeat-Proxied": "1"},
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
    request: Request,
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

    # Si ce noeud est un replica :
    if not is_leader.is_set():
        already_proxied = request.headers.get("X-Heartbeat-Proxied") == "1"
        # Défaut : forwarder le heartbeat au leader via WG. Sans ça, un
        # téléphone externe qui ne joint QUE ce nœud (les autres sur LAN privé,
        # ou le cloud tombé) ne pourrait jamais heartbeat → "connexion perdue"
        # perpétuelle malgré un cluster sain. Anti-boucle : on ne re-proxifie
        # pas un heartbeat déjà proxifié (le peer suivant sera tenté).
        if _PROXY_ON_REPLICA and not already_proxied:
            return _proxy_heartbeat_to_primary(credentials.credentials)
        # Flag désactivé (kill-switch) OU requête déjà proxifiée : 503 → l'app
        # rotate vers le leader (INV-ANDROID-304). Aussi le cas "proxy a échoué,
        # aucun leader joignable" remonté par _proxy_heartbeat_to_primary.
        raise HTTPException(status_code=503, detail="replica")

    if _my_pause_until(current_user.id):
        raise HTTPException(status_code=503, detail="Heartbeats coupés depuis l'onglet Tests (INV-138)")

    was_offline = not current_user.is_online
    now = clock_now()
    current_user.last_heartbeat = now
    current_user.is_online = True
    if was_offline:
        # INV-056 : event "went_online" emis sur transition is_online False -> True
        # (et pas sur chaque heartbeat — uniquement le passage).
        db.add(ConnectivityEvent(user_id=current_user.id, event="went_online", ts=now))
    db.commit()
    if was_offline:
        log_event("user_online", db=db, user_id=current_user.id, user_name=current_user.name)
    return {"status": "ok", "timestamp": now.isoformat()}


# INV-138 : heartbeats de l'admin refuses jusqu'a cette date (boutons de l'onglet Tests).
# shortcut: en memoire sur le leader (les heartbeats y sont relayes, INV-043) ; une
# bascule de leader leve la pause plus tot — acceptable pour un test de 5 min max.
_paused_until: dict = {}
MY_PAUSE = timedelta(minutes=5)


def _my_pause_until(user_id: int):
    until = _paused_until.get(user_id)
    if until and clock_now() < until:
        return until
    _paused_until.pop(user_id, None)
    return None


@router.post("/me/go-offline")
def go_offline(
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """INV-138 : l'admin qui clique passe hors ligne, heartbeats refuses 5 min."""
    now = clock_now()
    _paused_until[current_user.id] = now + MY_PAUSE
    if current_user.is_online:
        db.add(ConnectivityEvent(user_id=current_user.id, event="went_offline", ts=now))  # INV-056
    current_user.is_online = False
    db.commit()
    log_event("test_go_offline", db=db, user_id=current_user.id, user_name=current_user.name)
    return {"status": "ok", "paused": True, "until": _paused_until[current_user.id].isoformat()}


@router.post("/me/heartbeat-pause")
def toggle_my_heartbeat_pause(
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """INV-138 : bascule — heartbeats de l'admin refuses (au plus 5 min) ou retablis."""
    if _my_pause_until(current_user.id):
        _paused_until.pop(current_user.id)
    else:
        _paused_until[current_user.id] = clock_now() + MY_PAUSE
    until = _my_pause_until(current_user.id)
    log_event("test_heartbeat_pause", db=db, user_id=current_user.id, paused=until is not None)
    return {"status": "ok", "paused": until is not None, "until": until.isoformat() if until else None}


def _same_install(token: str):
    """INV-153 : une installation de l'app = identifiant Firebase (jeton avant ':')."""
    fid = token.split(":", 1)[0]
    return or_(DeviceToken.fcm_token == fid, DeviceToken.fcm_token.startswith(fid + ":", autoescape=True))


@router.post("/fcm-token")
def register_fcm_token(
    data: FcmTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Enregistre ou met a jour un token FCM pour le device de l'utilisateur."""
    # INV-153 : un telephone n'est inscrit qu'une fois, pour un seul utilisateur.
    db.query(DeviceToken).filter(
        _same_install(data.token),
        ~((DeviceToken.user_id == current_user.id) & (DeviceToken.device_id == data.device_id)),
    ).delete(synchronize_session=False)
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
    mine = db.query(DeviceToken).filter(DeviceToken.user_id == current_user.id)
    row = mine.filter(DeviceToken.device_id == data.device_id).first()
    # INV-153 : aussi les inscriptions fantomes de ce meme telephone (anciennes connexions)
    same_phone = DeviceToken.device_id == data.device_id
    if row:
        same_phone = or_(same_phone, _same_install(row.fcm_token))
    deleted = mine.filter(same_phone).delete(synchronize_session=False)
    db.commit()
    return {"status": "ok", "deleted": deleted}


@router.get("/", response_model=List[UserResponse])
def list_devices(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return list of users with their online status (replaces device list)."""
    return db.query(User).all()
