import os
import uuid
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from .database import get_db
from .models import User

SECRET_KEY = os.getenv("SECRET_KEY", "alarm-secret-key-change-in-prod")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()
# INV-082 : security scheme tolérant pour /auth/refresh — accepte un header
# Bearer absent OU expiré sans lever 401, ce qui permet à l'endpoint de
# basculer sur le mode refresh_token-dans-le-body quand l'access est mort.
security_optional = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode = {
        "sub": str(user_id),
        "exp": expire,
        "iat": now,   # Timestamp d'émission — utilisé par simulate-connection-loss
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(db: Session, user_id: int) -> str:
    """INV-082 : génère un refresh token UUID4 opaque, le persiste en DB,
    return la valeur brute (à renvoyer au client une seule fois).

    Le token n'est PAS un JWT — c'est un identifiant aléatoire dont la
    légitimité est vérifiée uniquement par lookup DB. Cela permet la
    révocation côté serveur (impossible avec un JWT stateless) et garantit
    qu'un token compromis cesse de fonctionner dès qu'on flip `revoked`.
    """
    from .models import RefreshToken
    token = str(uuid.uuid4())
    db.add(RefreshToken(user_id=user_id, token=token))
    db.commit()
    return token


def validate_refresh_token(db: Session, token: str) -> User | None:
    """INV-082 : vérifie qu'un refresh token est valide (existe, non révoqué,
    user existe encore). Update `last_used_at` au passage. Retourne le User
    associé ou None si invalide.
    """
    from .models import RefreshToken
    rt = db.query(RefreshToken).filter(
        RefreshToken.token == token,
        RefreshToken.revoked == False,  # noqa: E712 SQLAlchemy bool comparison
    ).first()
    if rt is None:
        return None
    user = db.query(User).filter(User.id == rt.user_id).first()
    if user is None:
        # Le user a été supprimé — invalider le refresh token au passage
        rt.revoked = True
        db.commit()
        return None
    rt.last_used_at = datetime.now(timezone.utc)
    db.commit()
    return user


def revoke_refresh_tokens_for_user(db: Session, user_id: int) -> int:
    """INV-082 : révoque tous les refresh tokens d'un user (logout, admin
    de sécurité, suspicion de vol). Retourne le nombre de tokens affectés.
    """
    from .models import RefreshToken
    count = db.query(RefreshToken).filter(
        RefreshToken.user_id == user_id,
        RefreshToken.revoked == False,  # noqa: E712
    ).update({"revoked": True})
    db.commit()
    return count


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency: requires authenticated user with is_admin=True."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès réservé aux administrateurs",
        )
    return current_user


def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials = Depends(security_optional),
    db: Session = Depends(get_db),
) -> User | None:
    """INV-082 : version optionnelle de get_current_user qui retourne None
    au lieu de raise 401 quand le Bearer est absent OU expiré OU invalide.

    Usage : endpoint /auth/refresh qui doit accepter deux modes (Bearer
    valide pour le legacy INV-074, OU refresh_token dans le body pour le
    nouveau INV-082) sans qu'un Bearer expiré ne bloque l'accès au mode
    body.
    """
    if credentials is None:
        return None
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        return None
    return db.query(User).filter(User.id == user_id).first()
