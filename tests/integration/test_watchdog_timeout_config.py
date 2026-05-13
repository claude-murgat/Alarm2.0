"""
Tier 2 integration test : INV-084 — watchdog_timeout_seconds lu depuis SystemConfig.

Couvre le bug issue #74 : quand un admin change `watchdog_timeout_seconds`
via POST /api/config/system, la nouvelle valeur doit etre effectivement
utilisee par le watchdog. Aujourd'hui la constante `WATCHDOG_TIMEOUT_SECONDS`
(watchdog.py:14) est hardcodee a 60 et utilisee telle quelle dans
`watchdog_loop` — la config admin est ignoree.

Invariant vise : INV-084 — « Aucun delai metier ne doit etre hardcode. Chaque
valeur est lue depuis SystemConfig a chaque usage, pour qu'un changement admin
prenne effet immediat. »

Pourquoi c'est un bug critique : la detection des users deconnectes (INV-041)
repose sur ce seuil. Un admin qui le baisse pour des raisons operationnelles
(ex: detection plus stricte) n'aura en realite aucun effet — faille silencieuse.

Sous-cas 1/2 restant du tableau INV-084 (l'autre etant `escalation_tick_seconds`,
hors-scope de cette issue). Sous-cas `oncall_offline_delay_minutes` deja fixe
PR #25 — meme pattern applique ici.

Budget P4 : 1 test cible qui prouve le comportement attendu.
"""
from datetime import timedelta

import pytest

pytestmark = pytest.mark.integration


def test_watchdog_timeout_read_from_system_config_not_hardcoded(client, admin_headers):
    """INV-084 : `watchdog_timeout_seconds` doit etre lu depuis SystemConfig
    a chaque tick du watchdog, pas depuis une constante hardcodee.

    Scenario :
      - Admin POST /api/config/system {watchdog_timeout_seconds: 30}
      - User1 est is_online=True avec last_heartbeat = now - 35s
        (35 > 30 nouveau seuil, mais 35 < 60 ancien seuil hardcode)
      - Un cycle de watchdog tourne

    Attendu : user1.is_online == False, car 35s > 30s configures.
    Buggy : user1 reste online car le code compare 35 < 60 (constante hardcodee).
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

    # 2) Setup : user1 online avec heartbeat il y a 35s
    #    (35 > 30 nouveau seuil → DOIT etre marque offline)
    #    (35 < 60 ancien seuil → resterait online si le code etait buggy)
    db = SessionLocal()
    target_user_id = None
    try:
        # Choisir un user non-admin pour ne pas casser admin_headers du cleanup
        user = db.query(User).filter(User.name == "user1").first()
        assert user is not None, "seed user1 should exist"
        target_user_id = user.id

        now_ref = clock_now()
        user.is_online = True
        user.last_heartbeat = now_ref - timedelta(seconds=35)
        db.commit()

        # 3) Un tick de watchdog (avec le now_ref pour avoir un calcul deterministe)
        watchdog_module._run_watchdog_check(db, now_ref)
        db.expire_all()  # relire l'etat apres commit interne du watchdog

        # 4) Verification : user doit etre offline car 35s > 30s configures
        user_after = db.query(User).filter(User.id == target_user_id).first()
        assert user_after.is_online is False, (
            "INV-084 (bug issue #74) : avec watchdog_timeout_seconds=30 et "
            "last_heartbeat il y a 35s, le user doit etre marque offline. "
            "Il reste online -> la config est ignoree au profit d'une constante "
            "hardcodee (WATCHDOG_TIMEOUT_SECONDS=60 dans watchdog.py)."
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
