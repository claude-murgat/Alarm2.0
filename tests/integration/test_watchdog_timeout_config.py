"""
Tier 2 integration test : INV-084 — watchdog_timeout_seconds lu depuis SystemConfig.

Couvre le bug issue #74 : quand un admin change `watchdog_timeout_seconds`
via POST /api/config/system, la nouvelle valeur doit etre effectivement
utilisee par le watchdog. Avant ce fix, la constante `WATCHDOG_TIMEOUT_SECONDS`
(watchdog.py:14) etait hardcodee a 60 et utilisee telle quelle dans
`watchdog_loop` — la config admin etait ignoree.

Invariant vise : INV-084 — « Aucun delai metier ne doit etre hardcode. Chaque
valeur est lue depuis SystemConfig a chaque usage, pour qu'un changement admin
prenne effet immediat. »

Pourquoi c'est un bug critique : la detection des users deconnectes (INV-041)
repose sur ce seuil. Un admin qui le baisse pour des raisons operationnelles
(ex: detection plus stricte) n'aurait en realite aucun effet — faille silencieuse.

Sous-cas 1/2 restant du tableau INV-084 (l'autre etant `escalation_tick_seconds`,
hors-scope de cette issue). Sous-cas `oncall_offline_delay_minutes` deja fixe
PR #25 — meme pattern applique ici.

Strategie 2-tests miroirs (P3 anti-mutant constant) :
  - Test 1 : seuil=30s, heartbeat il y a 35s   -> DOIT etre offline
  - Test 2 : seuil=120s, heartbeat il y a 70s  -> DOIT rester online
Tout mutant qui hardcoderait une valeur constante (30, 60, 120, ou n'importe
quel autre nombre) echouerait sur au moins un des deux tests : 35>X exige X<35
pour le 1er, 70<X exige X>70 pour le 2nd — aucun X fixe ne satisfait les deux.

Budget P4 : 2 tests dans ce fichier (1 offline + 1 reste online).
"""
from datetime import timedelta

import pytest

pytestmark = pytest.mark.integration


def _count_watchdog_offline_events_for(user_id: int) -> int:
    """Compte les events `watchdog_offline` persistes pour ce user_id.

    log_event ouvre sa propre SessionLocal (cf events.py:_persist_audit_event)
    -> on ouvre aussi une session dediee pour la lecture, sinon la session
    courante du test ne voit pas l'insert audit (read-your-writes inter-session).
    """
    from backend.app.database import SessionLocal
    from backend.app.models import AuditEvent

    s = SessionLocal()
    try:
        return s.query(AuditEvent).filter(
            AuditEvent.event_type == "watchdog_offline",
            AuditEvent.user_id == user_id,
        ).count()
    finally:
        s.close()


def test_watchdog_timeout_config_30s_marks_user_offline_at_35s(client, admin_headers):
    """INV-084 : `watchdog_timeout_seconds` lu depuis SystemConfig a chaque tick.

    Scenario "offline" :
      - Admin POST /api/config/system {watchdog_timeout_seconds: 30}
      - User1 is_online=True avec last_heartbeat = now - 35s
        (35 > 30 nouveau seuil  -> DOIT devenir offline)
        (35 < 60 ancien seuil   -> resterait online si bug)
      - Un cycle de watchdog tourne

    Verifie aussi (liaison INV-041) qu'un event `watchdog_offline` est emis.
    """
    from backend.app.database import SessionLocal
    from backend.app.models import User
    from backend.app.clock import now as clock_now
    from backend.app import watchdog as watchdog_module

    # 1) Admin change la config : seuil = 30s (au lieu du defaut 60s)
    r = client.post(
        "/api/config/system",
        json={"key": "watchdog_timeout_seconds", "value": "30"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    # Sanity : la config est bien persistee
    r = client.get("/api/config/system")
    assert r.status_code == 200
    assert r.json().get("watchdog_timeout_seconds") == "30", (
        f"config doit etre persistee a '30', got "
        f"{r.json().get('watchdog_timeout_seconds')!r}"
    )

    db = SessionLocal()
    target_user_id = None
    try:
        # 2) Setup : user1 online avec heartbeat il y a 35s
        user = db.query(User).filter(User.name == "user1").first()
        assert user is not None, "seed user1 should exist"
        target_user_id = user.id

        now_ref = clock_now()
        user.is_online = True
        user.last_heartbeat = now_ref - timedelta(seconds=35)
        db.commit()
        db.refresh(user)

        # Assertion pre-tick : evite un test "vacuously true" si le seed
        # etait deja offline pour une autre raison.
        assert user.is_online is True, (
            "pre-tick : user1 doit etre online avant le tick, "
            "sinon le test prouve juste 'offline reste offline'."
        )

        events_before = _count_watchdog_offline_events_for(target_user_id)

        # 3) Un tick de watchdog (now_ref pour un calcul deterministe)
        watchdog_module._run_watchdog_check(db, now_ref)
        db.expire_all()  # relire l'etat apres commits internes du watchdog

        # 4) Verification etat : user doit etre offline car 35s > 30s configures
        user_after = db.query(User).filter(User.id == target_user_id).first()
        assert user_after.is_online is False, (
            "INV-084 (bug issue #74) : avec watchdog_timeout_seconds=30 et "
            "last_heartbeat il y a 35s, le user doit etre marque offline. "
            "S'il reste online -> la config est ignoree au profit d'une "
            "constante hardcodee (WATCHDOG_TIMEOUT_SECONDS=60)."
        )

        # 5) Verification event (liaison INV-041) : un nouveau watchdog_offline
        # a ete emis pour CE user — preuve que la transition a bien ete tracee.
        events_after = _count_watchdog_offline_events_for(target_user_id)
        assert events_after == events_before + 1, (
            f"INV-041 : exactement 1 nouvel event 'watchdog_offline' attendu "
            f"pour user_id={target_user_id} apres le tick. "
            f"Avant={events_before}, apres={events_after}."
        )
    finally:
        # Cleanup deterministe : remettre user online + restaurer la config par defaut
        if target_user_id is not None:
            u = db.query(User).filter(User.id == target_user_id).first()
            if u is not None:
                u.is_online = True
                u.last_heartbeat = clock_now()
                db.commit()
        db.close()

        client.post(
            "/api/config/system",
            json={"key": "watchdog_timeout_seconds", "value": "60"},
            headers=admin_headers,
        )


def test_watchdog_timeout_config_120s_keeps_user_online_at_70s(client, admin_headers):
    """INV-084 (miroir) : seuil augmente -> heartbeat plus vieux que l'ancien
    seuil hardcode (60s) doit rester ONLINE si la nouvelle config (120s) est lue.

    Scenario "reste online" :
      - Admin POST /api/config/system {watchdog_timeout_seconds: 120}
      - User1 is_online=True avec last_heartbeat = now - 70s
        (70 < 120 nouveau seuil -> DOIT rester online)
        (70 > 60 ancien seuil   -> deviendrait offline si bug)
      - Un cycle de watchdog tourne

    Verifie aussi qu'AUCUN event `watchdog_offline` n'a ete emis pour ce user
    (le test offline le verifie en positif ; ici on verifie le negatif pour
    fermer le couplage INV-041 dans les deux sens).

    Ce test ferme le trou mutation-mecanique : un mutant qui remplacerait la
    lecture DB par une constante fixe (30, 60, 120, n'importe laquelle) ne
    peut pas satisfaire a la fois ce test (X>70) et le test 30s (X<35).
    """
    from backend.app.database import SessionLocal
    from backend.app.models import User
    from backend.app.clock import now as clock_now
    from backend.app import watchdog as watchdog_module

    # 1) Admin change la config : seuil = 120s (au-dessus du defaut 60s)
    r = client.post(
        "/api/config/system",
        json={"key": "watchdog_timeout_seconds", "value": "120"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    # Sanity : la config est bien persistee
    r = client.get("/api/config/system")
    assert r.status_code == 200
    assert r.json().get("watchdog_timeout_seconds") == "120", (
        f"config doit etre persistee a '120', got "
        f"{r.json().get('watchdog_timeout_seconds')!r}"
    )

    db = SessionLocal()
    target_user_id = None
    try:
        # 2) Setup : user1 online avec heartbeat il y a 70s
        user = db.query(User).filter(User.name == "user1").first()
        assert user is not None, "seed user1 should exist"
        target_user_id = user.id

        now_ref = clock_now()
        user.is_online = True
        user.last_heartbeat = now_ref - timedelta(seconds=70)
        db.commit()
        db.refresh(user)

        # Assertion pre-tick (idem test offline) : evite un "vacuously true"
        # si pour une raison quelconque user1 etait deja offline avant.
        assert user.is_online is True, (
            "pre-tick : user1 doit etre online avant le tick, "
            "sinon le test prouve juste 'offline reste offline'."
        )

        events_before = _count_watchdog_offline_events_for(target_user_id)

        # 3) Un tick de watchdog
        watchdog_module._run_watchdog_check(db, now_ref)
        db.expire_all()

        # 4) Verification etat : user doit RESTER online car 70s < 120s configures
        user_after = db.query(User).filter(User.id == target_user_id).first()
        assert user_after.is_online is True, (
            "INV-084 miroir : avec watchdog_timeout_seconds=120 et "
            "last_heartbeat il y a 70s, le user doit RESTER online. "
            "S'il est offline -> le code utilise encore une constante "
            "hardcodee (probablement 60) au lieu de lire la config DB."
        )

        # 5) Verification event : AUCUN nouveau watchdog_offline pour ce user.
        # Si un event apparait alors que l'etat est online, c'est soit une
        # incoherence (event sans transition), soit que le watchdog a marque
        # offline puis quelque chose a recroise — dans tous les cas, signal
        # d'un bug a investiguer.
        events_after = _count_watchdog_offline_events_for(target_user_id)
        assert events_after == events_before, (
            f"INV-041 miroir : aucun event 'watchdog_offline' ne doit etre "
            f"emis pour user_id={target_user_id} quand il reste online. "
            f"Avant={events_before}, apres={events_after}."
        )
    finally:
        # Cleanup deterministe : restaurer la config par defaut + heartbeat frais
        if target_user_id is not None:
            u = db.query(User).filter(User.id == target_user_id).first()
            if u is not None:
                u.is_online = True
                u.last_heartbeat = clock_now()
                db.commit()
        db.close()

        client.post(
            "/api/config/system",
            json={"key": "watchdog_timeout_seconds", "value": "60"},
            headers=admin_headers,
        )
