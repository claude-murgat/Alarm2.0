"""
Tier 2 integration : INV-153 — un telephone n'est inscrit qu'une fois, pour un
seul utilisateur.

Prod 2026-10-22 : timo, deconnecte de tous ses telephones, restait « En attente »
(INV-143) a cause de 2 inscriptions fantomes (meme jeton FCM, device_id
differents) ; le meme telephone etait entre-temps connecte au compte `test`.
Une installation = identifiant Firebase = partie du jeton avant `:`.
"""
import uuid

import pytest

pytestmark = pytest.mark.integration


def _headers(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _user_id(client, name):
    return client.post("/api/auth/login", json={"name": name, "password": "user123"}).json()["user"]["id"]


def _rows(user_id, fid):
    """Inscriptions (device_id) de l'utilisateur pour l'installation `fid`."""
    from backend.app.database import SessionLocal
    from backend.app.models import DeviceToken
    db = SessionLocal()
    try:
        return sorted(r.device_id for r in db.query(DeviceToken).filter(
            DeviceToken.user_id == user_id, DeviceToken.fcm_token.like(f"{fid}:%")))
    finally:
        db.close()


def _register(client, headers, token, device_id):
    r = client.post("/api/devices/fcm-token", headers=headers, json={"token": token, "device_id": device_id})
    assert r.status_code == 200, r.text


def test_phone_logged_into_another_account_drops_previous_account(client):
    """Le telephone de timo passe sur le compte test : timo n'y est plus inscrit."""
    fid = f"fid{uuid.uuid4().hex[:8]}"
    _register(client, _headers(client, "user1", "user123"), f"{fid}:ancien-jeton", "install-1")
    _register(client, _headers(client, "user2", "user123"), f"{fid}:nouveau-jeton", "install-2")

    assert _rows(_user_id(client, "user1"), fid) == [], "user1 ne doit plus etre inscrit sur ce telephone"
    assert _rows(_user_id(client, "user2"), fid) == ["install-2"]


def test_relogin_on_same_phone_keeps_one_registration(client):
    """Chaque connexion recree un device_id : l'ancienne inscription disparait."""
    fid = f"fid{uuid.uuid4().hex[:8]}"
    h = _headers(client, "user1", "user123")
    _register(client, h, f"{fid}:jeton", "connexion-1")
    _register(client, h, f"{fid}:jeton", "connexion-2")

    assert _rows(_user_id(client, "user1"), fid) == ["connexion-2"]


def test_logout_removes_ghost_registrations_of_same_phone_only(client):
    """Fantomes laisses par d'anciennes deconnexions (cas timo) : la deconnexion
    les retire tous pour ce telephone, sans toucher l'autre telephone."""
    from backend.app.database import SessionLocal
    from backend.app.models import DeviceToken
    fid, other = f"fid{uuid.uuid4().hex[:8]}", f"fid{uuid.uuid4().hex[:8]}"
    uid = _user_id(client, "user1")
    db = SessionLocal()
    db.add_all([
        DeviceToken(user_id=uid, fcm_token=f"{fid}:jeton", device_id="fantome-13-oct"),
        DeviceToken(user_id=uid, fcm_token=f"{fid}:jeton", device_id="fantome-14-oct"),
        DeviceToken(user_id=uid, fcm_token=f"{fid}:jeton", device_id="session-courante"),
        DeviceToken(user_id=uid, fcm_token=f"{other}:jeton", device_id="autre-telephone"),
    ])
    db.commit()
    db.close()

    r = client.request("DELETE", "/api/devices/fcm-token", headers=_headers(client, "user1", "user123"),
                       json={"device_id": "session-courante"})
    assert r.status_code == 200, r.text

    assert _rows(uid, fid) == [], "toutes les inscriptions de ce telephone doivent partir"
    assert _rows(uid, other) == ["autre-telephone"], "l'autre telephone reste inscrit (INV-078)"
