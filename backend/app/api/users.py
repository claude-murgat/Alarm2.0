from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from ..database import get_db
from ..models import User, Alarm, EscalationConfig
from ..schemas import UserCreate, UserResponse, LoginRequest, TokenResponse
from ..auth import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


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
    return user


@router.post("/login", response_model=TokenResponse)
def login(login_data: LoginRequest, db: Session = Depends(get_db)):
    # Case-insensitive login
    user = db.query(User).filter(func.lower(User.name) == login_data.name.lower()).first()
    if not user or not verify_password(login_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Identifiants invalides")

    token = create_access_token(user.id)
    return TokenResponse(
        access_token=token,
        user=UserResponse.model_validate(user),
    )


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


users_router = APIRouter(prefix="/api/users", tags=["users"])


@users_router.get("/", response_model=List[UserResponse])
def list_users(db: Session = Depends(get_db)):
    return db.query(User).all()


@users_router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)):
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

    db.delete(user)
    db.commit()
    return {"status": "deleted"}


@router.post("/refresh")
def refresh_token(current_user: User = Depends(get_current_user)):
    """Refresh the access token. Returns a new token with fresh expiry."""
    new_token = create_access_token(current_user.id)
    return {
        "access_token": new_token,
        "token_type": "bearer",
    }
