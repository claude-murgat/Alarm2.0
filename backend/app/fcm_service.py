"""
Service FCM (Firebase Cloud Messaging).
En prod : POST HTTP vers FCM API v1.
En test : stocke les notifications dans une liste module-level.
Pattern identique a email_service.py.
"""

import logging
import os
import json
from typing import Optional
from sqlalchemy.orm import Session

logger = logging.getLogger("fcm_service")

# Module-level storage for test inspection
_last_fcm_list: list = []

FCM_PROJECT_ID = os.getenv("FCM_PROJECT_ID", "")
FCM_SERVICE_ACCOUNT_JSON = os.getenv("FCM_SERVICE_ACCOUNT_JSON", "")


def send_fcm_to_user(db: Session, user_id: int, title: str, body: str, data: Optional[dict] = None):
    """Envoie un FCM a tous les tokens enregistres pour un user.
    En dev/test : stocke dans _last_fcm_list.
    En prod (FCM_SERVICE_ACCOUNT_JSON configure) : POST vers FCM API v1."""
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

        # Production: send via FCM HTTP v1 API
        if FCM_PROJECT_ID and FCM_SERVICE_ACCOUNT_JSON:
            _send_fcm_http(t.fcm_token, title, body, data or {})


def _send_fcm_http(token: str, title: str, body: str, data: dict):
    """POST vers FCM API v1 (production uniquement)."""
    try:
        import urllib.request

        url = f"https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send"
        message = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": {k: str(v) for k, v in data.items()},
                "android": {"priority": "high"},
            }
        }

        # TODO: implement OAuth2 token from service account JSON
        # For now, log and skip actual HTTP call
        logger.info(f"FCM HTTP would send to {url}: {title}")

    except Exception as e:
        logger.error(f"FCM HTTP send failed: {e}")


def get_last_fcm_list() -> list:
    """Retourne la liste de tous les FCM envoyes (pour tests)."""
    return list(_last_fcm_list)


def reset_last_fcm():
    """Reset la liste FCM (pour tests)."""
    _last_fcm_list.clear()
