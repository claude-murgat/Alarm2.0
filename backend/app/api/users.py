import time
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from ..database import get_db
from ..models import User, Alarm, EscalationConfig
from ..schemas import UserCreate, UserResponse, LoginRequest, TokenResponse
from ..auth import hash_password, verify_password, create_access_token, get_current_user, get_current_admin
from ..events import log_event

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── Rate limiting par username ────────────────────────────────────────────────
# Stocke les timestamps des tentatives échouées par nom (en mémoire).
_login_failures: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # secondes
RATE_LIMIT_MAX_FAILURES = 10  # max tentatives échouées par fenêtre


def _check_rate_limit(username: str):
    """Vérifie si le username est rate-limited. Raise 429 si oui."""
    now = time.time()
    key = username.lower()
    # Purger les tentatives hors fenêtre
    _login_failures[key] = [t for t in _login_failures[key] if now - t < RATE_LIMIT_WINDOW]
    if len(_login_failures[key]) >= RATE_LIMIT_MAX_FAILURES:
        raise HTTPException(
            status_code=429,
            detail="Trop de tentatives de connexion. Réessayez dans une minute.",
        )


def _record_failure(username: str):
    """Enregistre une tentative échouée."""
    _login_failures[username.lower()].append(time.time())


@router.post("/register", response_model=UserResponse)
def register(user_data: UserCreate, db: Session = Depends(get_db)):
    # name is already lowercased and validated (no spaces) by pydantic
    existing = db.query(User).filter(func.lower(User.name) == user_data.name.lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail="Nom déjà utilisé")

    user = User(
        name=user_data.name.lower(),
        hashed_password=hash_password(user_data.password),
        is_admin=user_data.is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_event("user_created", db=db, user_id=user.id, name=user.name)
    return user


@router.post("/login", response_model=TokenResponse)
def login(login_data: LoginRequest, db: Session = Depends(get_db)):
    # Rate limiting par username
    _check_rate_limit(login_data.name)

    # Case-insensitive login
    user = db.query(User).filter(func.lower(User.name) == login_data.name.lower()).first()
    if not user or not verify_password(login_data.password, user.hashed_password):
        _record_failure(login_data.name)
        log_event("user_login_failed", db=db, name=login_data.name)
        raise HTTPException(status_code=401, detail="Identifiants invalides")

    # Login réussi : effacer l'historique de tentatives
    _login_failures.pop(login_data.name.lower(), None)
    token = create_access_token(user.id)

    # Calculer is_oncall : user est-il en position 1 de la chaine d'escalade ?
    from ..models import EscalationConfig
    first_pos = db.query(EscalationConfig).order_by(EscalationConfig.position).first()
    is_oncall = first_pos is not None and first_pos.user_id == user.id

    log_event("user_login", db=db, user_id=user.id, name=user.name)
    return TokenResponse(
        access_token=token,
        user=UserResponse.model_validate(user),
        is_oncall=is_oncall,
    )


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


users_router = APIRouter(prefix="/api/users", tags=["users"])


@users_router.get("/", response_model=List[UserResponse])
def list_users(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return db.query(User).all()


@users_router.delete("/{user_id}")
def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

    # Reassign active alarms before deleting
    active_alarms = (
        db.query(Alarm)
        .filter(
            Alarm.assigned_user_id == user_id,
            Alarm.status.in_(["active", "escalated"]),
        )
        .all()
    )
    if active_alarms:
        # Find replacement: first in escalation chain (not the deleted user), or first other user
        first_ec = (
            db.query(EscalationConfig)
            .filter(EscalationConfig.user_id != user_id)
            .order_by(EscalationConfig.position)
            .first()
        )
        if first_ec:
            new_user_id = first_ec.user_id
        else:
            other_user = db.query(User).filter(User.id != user_id).first()
            new_user_id = other_user.id if other_user else None

        if new_user_id:
            for alarm in active_alarms:
                alarm.assigned_user_id = new_user_id
            db.commit()  # Commit reassignment BEFORE delete to avoid FK SET NULL

    deleted_name = user.name
    deleted_id = user.id
    db.delete(user)
    db.commit()
    log_event("user_deleted", db=db, user_id=deleted_id, name=deleted_name)
    return {"status": "deleted"}


@router.post("/refresh")
def refresh_token(current_user: User = Depends(get_current_user)):
    """Refresh the access token. Returns a new token with fresh expiry."""
    new_token = create_access_token(current_user.id)
    return {
        "access_token": new_token,
        "token_type": "bearer",
    }


@users_router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Met à jour les champs d'un utilisateur (ex: phone_number).
    Un utilisateur peut modifier son propre profil. Modifier un autre profil requiert admin."""
    if user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Seul un admin peut modifier un autre utilisateur",
        )
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    if "phone_number" in payload:
        user.phone_number = payload["phone_number"]
    db.commit()
    db.refresh(user)
    return user
