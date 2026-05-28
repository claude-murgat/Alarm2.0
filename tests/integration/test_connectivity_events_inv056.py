"""Tests d'intégration tier 2 — INV-056 (tracking online/offline).

Couvre la table `connectivity_events` introduite 2026-05-26 en remplacement de
l'alarme oncall_offline (INV-050 dépréciée). Voir tests/INVARIANTS.md §5.

Tests :
- Watchdog émet `went_offline` quand il flip un user is_online=True -> False.
- Heartbeat émet `went_online` UNIQUEMENT sur transition is_online=False -> True.
- Heartbeat sur user déjà online n'émet AUCUN event (anti-doublon).
- Endpoint /api/users/{id}/connectivity-history retourne les events filtrés
  par fenêtre `days`, en ordre décroissant, admin only.
- Endpoint /api/stats/connectivity agrège le uptime par user sur la fenêtre.
"""
from datetime import timedelta

import pytest

pytestmark = pytest.mark.integration


def _admin_token(client):
    r = client.post("/api/auth/login", json={"name": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _user1_token(client):
    r = client.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _clean_events():
    from backend.app.database import SessionLocal
    from backend.app.models import ConnectivityEvent

    db = SessionLocal()
    try:
        db.query(ConnectivityEvent).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _setup():
    """Nettoyer events + remettre tous users online avant chaque test."""
    from backend.app.database import SessionLocal
    from backend.app.models import ConnectivityEvent, User
    from backend.app.clock import now as clock_now

    db = SessionLocal()
    try:
        db.query(ConnectivityEvent).delete()
        for u in db.query(User).all():
            u.is_online = True
            u.last_heartbeat = clock_now()
        db.commit()
    finally:
        db.close()
    yield


def test_watchdog_emits_went_offline_on_flip(client):
    """INV-056 : watchdog._run_watchdog_check insère un event `went_offline`
    quand il marque un user `is_online=False` (transition uniquement)."""
    from datetime import timedelta as td
    from backend.app.database import SessionLocal
    from backend.app.models import ConnectivityEvent, User
    from backend.app.watchdog import _run_watchdog_check
    from backend.app.clock import now as clock_now

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.name == "user1").first()
        assert user is not None
        # Setup : user online avec un heartbeat ancien (90s > timeout 60s)
        now_ref = clock_now()
        user.is_online = True
        user.last_heartbeat = now_ref - td(seconds=90)
        db.commit()

        # Action : un tick de watchdog
        _run_watchdog_check(db, now_ref)
        db.expire_all()

        # Assert : user.is_online flippé + event went_offline inséré
        user = db.query(User).filter(User.name == "user1").first()
        assert user.is_online is False, "watchdog doit flipper is_online à False"

        events = db.query(ConnectivityEvent).filter(
            ConnectivityEvent.user_id == user.id
        ).all()
        assert len(events) == 1, (
            f"INV-056 : 1 event went_offline attendu, got {len(events)}"
        )
        assert events[0].event == "went_offline"
        assert events[0].ts == now_ref
    finally:
        db.close()


def test_heartbeat_emits_went_online_only_on_transition(client):
    """INV-056 : `POST /api/devices/heartbeat` insère `went_online`
    UNIQUEMENT quand le heartbeat fait transitionner is_online de False vers True.

    Un heartbeat sur un user déjà online (cas nominal toutes les 3s) ne doit
    PAS générer d'event (anti-pollution)."""
    from backend.app.database import SessionLocal
    from backend.app.models import ConnectivityEvent, User

    token = _user1_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Setup : forcer user1 offline en DB
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.name == "user1").first()
        user.is_online = False
        db.commit()
        user_id = user.id
    finally:
        db.close()

    # 1er heartbeat : transition False -> True → 1 event went_online attendu
    r = client.post("/api/devices/heartbeat", headers=headers)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        events = db.query(ConnectivityEvent).filter(
            ConnectivityEvent.user_id == user_id
        ).all()
        assert len(events) == 1, (
            f"INV-056 : transition False->True → 1 went_online, got {len(events)}"
        )
        assert events[0].event == "went_online"
    finally:
        db.close()

    # 2e heartbeat : user déjà online → aucun nouvel event
    r = client.post("/api/devices/heartbeat", headers=headers)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        events = db.query(ConnectivityEvent).filter(
            ConnectivityEvent.user_id == user_id
        ).all()
        assert len(events) == 1, (
            f"INV-056 anti-pollution : heartbeat sur user déjà online → "
            f"AUCUN nouvel event, got {len(events)}"
        )
    finally:
        db.close()


def test_connectivity_history_endpoint(client):
    """INV-056 : GET /api/users/{id}/connectivity-history retourne les events
    ordonnés du plus récent au plus ancien, filtrés sur la fenêtre `days`."""
    from datetime import datetime as dt
    from backend.app.database import SessionLocal
    from backend.app.models import ConnectivityEvent, User
    from backend.app.clock import now as clock_now

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.name == "user1").first()
        now = clock_now()
        # Insérer 3 events : 1h ago, 30min ago, 5min ago
        db.add(ConnectivityEvent(user_id=user.id, event="went_offline", ts=now - timedelta(hours=1)))
        db.add(ConnectivityEvent(user_id=user.id, event="went_online", ts=now - timedelta(minutes=30)))
        db.add(ConnectivityEvent(user_id=user.id, event="went_offline", ts=now - timedelta(minutes=5)))
        db.commit()
        user_id = user.id
    finally:
        db.close()

    admin_h = {"Authorization": f"Bearer {_admin_token(client)}"}
    r = client.get(f"/api/users/{user_id}/connectivity-history?days=1", headers=admin_h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["user_id"] == user_id
    assert data["user_name"] == "user1"
    assert data["days"] == 1
    assert len(data["events"]) == 3
    # Ordre décroissant : le plus récent en premier
    assert data["events"][0]["event"] == "went_offline"  # 5min ago
    assert data["events"][1]["event"] == "went_online"   # 30min ago
    assert data["events"][2]["event"] == "went_offline"  # 1h ago


def test_connectivity_history_admin_only(client):
    """INV-056 : endpoint réservé aux admins (403 pour non-admin)."""
    user1_h = {"Authorization": f"Bearer {_user1_token(client)}"}

    from backend.app.database import SessionLocal
    from backend.app.models import User
    db = SessionLocal()
    try:
        user1_id = db.query(User).filter(User.name == "user1").first().id
    finally:
        db.close()

    r = client.get(f"/api/users/{user1_id}/connectivity-history", headers=user1_h)
    assert r.status_code == 403, (
        f"non-admin doit recevoir 403, got {r.status_code}"
    )


def test_stats_connectivity_aggregates_uptime(client):
    """INV-056 : GET /api/stats/connectivity calcule l'uptime % par user.

    Setup : sur la dernière journée, user1 a été offline 1h consécutive.
    Expected : uptime ≈ (24h - 1h) / 24h = 95.83 %.
    """
    from backend.app.database import SessionLocal
    from backend.app.models import ConnectivityEvent, User
    from backend.app.clock import now as clock_now

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.name == "user1").first()
        now = clock_now()
        # user1 went_offline il y a 2h, went_online il y a 1h → 1h offline.
        db.add(ConnectivityEvent(
            user_id=user.id, event="went_offline", ts=now - timedelta(hours=2),
        ))
        db.add(ConnectivityEvent(
            user_id=user.id, event="went_online", ts=now - timedelta(hours=1),
        ))
        db.commit()
        user_id = user.id
    finally:
        db.close()

    admin_h = {"Authorization": f"Bearer {_admin_token(client)}"}
    r = client.get("/api/stats/connectivity?days=1", headers=admin_h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["days"] == 1
    by_id = {u["user_id"]: u for u in data["users"]}
    user1_stats = by_id[user_id]
    assert user1_stats["transitions_offline"] == 1
    # 1h offline sur 24h fenêtre → 3600s offline / 86400s window → 95.83 %
    assert 3500 <= user1_stats["total_offline_seconds"] <= 3700, (
        f"~3600s offline attendus, got {user1_stats['total_offline_seconds']}"
    )
    assert 95.0 <= user1_stats["uptime_percent"] <= 96.0, (
        f"~95.83% uptime attendu, got {user1_stats['uptime_percent']}"
    )
