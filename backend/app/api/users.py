import time
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
from ..database import get_db
from ..models import User, Alarm, EscalationConfig
from ..schemas import UserCreate, UserUpdate, UserResponse, LoginRequest, TokenResponse, RefreshRequest
from ..auth import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    validate_refresh_token,
    get_current_user,
    get_current_user_optional,
    get_current_admin,
)
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
        phone_number=user_data.phone_number,  # INV-069 : saisi a la creation
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
    # INV-079 : émettre un refresh token persistant (UUID opaque) en plus du
    # JWT access. Le client stocke les deux, utilise refresh pour renouveler.
    refresh = create_refresh_token(db, user.id)

    # Calculer is_oncall + escalation_position
    from ..models import EscalationConfig
    first_pos = db.query(EscalationConfig).order_by(EscalationConfig.position).first()
    is_oncall = first_pos is not None and first_pos.user_id == user.id

    # Position de l'utilisateur dans la chaine d'escalade (None si absent)
    user_esc = db.query(EscalationConfig).filter(EscalationConfig.user_id == user.id).first()
    escalation_position = user_esc.position if user_esc else None

    log_event("user_login", db=db, user_id=user.id, name=user.name)
    return TokenResponse(
        access_token=token,
        refresh_token=refresh,
        user=UserResponse.model_validate(user),
        is_oncall=is_oncall,
        escalation_position=escalation_position,
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
def refresh_token(
    payload: Optional[RefreshRequest] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """INV-074 + INV-079 : refresh l'access token.

    Deux modes acceptés pendant la phase de transition :

    1. **Mode INV-079 (recommandé, Gmail-style)** : body `{refresh_token: ...}`
       avec un UUID opaque persisté en DB. Valable indéfiniment sauf si
       révoqué. Cas d'usage : tél éteint plusieurs semaines, l'access JWT a
       expiré (24h), `Depends(get_current_user)` lève 401 sur l'access mort.
       → Le client tente alors un /refresh **sans** Authorization header,
       avec le refresh dans le body, et récupère un nouveau access.

    2. **Mode legacy (INV-074 historique)** : pas de body, juste un Bearer
       valide. Renvoie un nouveau access. Conservé pour compat avec les
       clients existants qui ne stockent pas encore le refresh.

    Si AUCUN des deux n'est fourni → 401.
    """
    # Mode INV-079 : refresh dans le body. Prioritaire car c'est le seul
    # mode utilisable quand l'access est expiré (le legacy nécessite un
    # Bearer valide via get_current_user, qui aura déjà raise 401).
    if payload is not None and payload.refresh_token:
        user = validate_refresh_token(db, payload.refresh_token)
        if user is None:
            raise HTTPException(status_code=401, detail="refresh_token invalide ou révoqué")
        new_token = create_access_token(user.id)
        return {
            "access_token": new_token,
            "token_type": "bearer",
        }

    # Mode legacy INV-074 : Bearer valide via get_current_user_optional
    if current_user is None:
        raise HTTPException(status_code=401, detail="Bearer manquant ou invalide, et refresh_token absent du body")
    new_token = create_access_token(current_user.id)
    return {
        "access_token": new_token,
        "token_type": "bearer",
    }


@users_router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    payload: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Met à jour les champs d'un utilisateur (ex: phone_number).
    Un utilisateur peut modifier son propre profil. Modifier un autre profil requiert admin.

    INV-069 §3 : la validation regex de phone_number est portee par
    `UserUpdate` (schemas.py) — un format non composable par le modem
    SIM7600 -> 422 avant toucher la DB.
    """
    if user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Seul un admin peut modifier un autre utilisateur",
        )
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    # `model_fields_set` distingue "champ absent du body" de "champ envoye
    # avec None" (intent: efface vers NULL, cf INV-061).
    if "phone_number" in payload.model_fields_set:
        user.phone_number = payload.phone_number
    db.commit()
    db.refresh(user)
    return user
