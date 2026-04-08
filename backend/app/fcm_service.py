"""
Service FCM (Firebase Cloud Messaging).
En prod : POST HTTP vers FCM API v1 avec OAuth2.
En test : stocke les notifications dans une liste module-level.
Pattern identique a email_service.py.
"""

import logging
import os
import json
import urllib.request
from typing import Optional
from sqlalchemy.orm import Session

logger = logging.getLogger("fcm_service")

# Module-level storage for test inspection
_last_fcm_list: list = []

# Chemin vers le fichier service account JSON (optionnel)
FCM_SERVICE_ACCOUNT_PATH = os.getenv("FCM_SERVICE_ACCOUNT_JSON", "")

# Cache des credentials OAuth2
_credentials = None
_project_id = None


def _load_credentials():
    """Charge les credentials OAuth2 depuis le service account JSON (lazy, cached)."""
    global _credentials, _project_id

    if _credentials is not None:
        return _credentials, _project_id

    if not FCM_SERVICE_ACCOUNT_PATH:
        return None, None

    try:
        # Accepte un chemin fichier ou du JSON inline
        if os.path.isfile(FCM_SERVICE_ACCOUNT_PATH):
            with open(FCM_SERVICE_ACCOUNT_PATH) as f:
                sa_info = json.load(f)
        else:
            sa_info = json.loads(FCM_SERVICE_ACCOUNT_PATH)

        from google.oauth2 import service_account
        _credentials = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        _project_id = sa_info.get("project_id", "")
        logger.info(f"FCM credentials loaded: project={_project_id}")
        return _credentials, _project_id

    except Exception as e:
        logger.error(f"FCM credentials load failed: {e}")
        return None, None


def _get_access_token() -> Optional[str]:
    """Obtient un access token OAuth2 valide (refresh automatique)."""
    creds, _ = _load_credentials()
    if creds is None:
        return None

    try:
        from google.auth.transport.requests import Request
        if not creds.valid:
            creds.refresh(Request())
        return creds.token
    except Exception as e:
        logger.error(f"FCM token refresh failed: {e}")
        return None


def send_fcm_to_user(db: Session, user_id: int, title: str, body: str, data: Optional[dict] = None):
    """Envoie un FCM a tous les tokens enregistres pour un user.
    Toujours stocke dans _last_fcm_list (pour tests).
    Si service account configure : POST HTTP reel vers FCM API v1."""
    from .models import DeviceToken

    tokens = db.query(DeviceToken).filter(DeviceToken.user_id == user_id).all()

    if not tokens:
        logger.info(f"FCM skip: user {user_id} has no registered tokens")
        return

    for t in tokens:
        entry = {
            "user_id": user_id,
            "token": t.fcm_token,
            "title": title,
            "body": body,
            "data": data or {},
        }
        _last_fcm_list.append(entry)
        logger.info(f"FCM queued: user={user_id}, token={t.fcm_token[:20]}..., title={title}")

        # Envoi reel si credentials disponibles
        _send_fcm_http(t.fcm_token, title, body, data or {})


def _send_fcm_http(token: str, title: str, body: str, data: dict):
    """POST vers FCM API v1 avec OAuth2 (si credentials disponibles)."""
    access_token = _get_access_token()
    if not access_token:
        return  # Pas de credentials = mode test, on skip l'envoi reel

    _, project_id = _load_credentials()
    if not project_id:
        return

    try:
        url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
        message = {
            "message": {
                "token": token,
                "data": {k: str(v) for k, v in data.items()},
                "android": {
                    "priority": "high",
                    "notification": {
                        "title": title,
                        "body": body,
                        "channel_id": "alarm_channel",
                    },
                },
            }
        }

        payload = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Content-Type", "application/json")

        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        logger.info(f"FCM sent OK: {result.get('name', '?')}")

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"FCM HTTP {e.code}: {error_body[:200]}")
    except Exception as e:
        logger.error(f"FCM send failed: {e}")


def get_last_fcm_list() -> list:
    """Retourne la liste de tous les FCM envoyes (pour tests)."""
    return list(_last_fcm_list)


def reset_last_fcm():
    """Reset la liste FCM (pour tests)."""
    _last_fcm_list.clear()
