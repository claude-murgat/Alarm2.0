"""
Tier 2 integration test : INV-084 — oncall_offline_delay_minutes lu depuis SystemConfig.

Couvre le bug de l'issue #24 : quand un admin change `oncall_offline_delay_minutes`
via POST /api/config/system, la nouvelle valeur doit etre effectivement utilisee
par la surveillance oncall. Aujourd'hui la constante `ONCALL_OFFLINE_DELAY_MINUTES`
(escalation.py:24) est hardcodee a 15.0 et passee telle quelle a
`evaluate_oncall_heartbeat` — la config admin est ignoree.

Invariant vise : INV-084 — « Aucun delai metier ne doit etre hardcode. Chaque
valeur est lue depuis SystemConfig a chaque usage, pour qu'un changement admin
prenne effet immediat. »

Pourquoi c'est un bug critique : la continuite de service oncall (INV-050) repose
sur ce delai. Un admin qui reduit le delai pour des raisons operationnelles
(ex: astreinte plus stricte le week-end) n'aura en realite aucun effet — faille
silencieuse.

Budget P4 : 1 test cible qui prouve le comportement attendu.
"""
from datetime import timedelta

import pytest

pytestmark = pytest.mark.integration


def test_oncall_delay_read_from_system_config_not_hardcoded(client, admin_headers):
    """INV-084 : `oncall_offline_delay_minutes` doit etre lu depuis SystemConfig
    a chaque evaluation, pas depuis une constante hardcodee.

    Scenario de l'issue #24 :
      - Admin POST /api/config/system {oncall_offline_delay_minutes: 2}
      - User1 (pos 1, de garde) est offline depuis 3 min (> 2, < 15)
      - User2 est online
      - Un cycle de surveillance oncall tourne

    Attendu : une alarme is_oncall_alarm=True est creee, car 3 > 2 (config).
    Buggy : aucune alarme car le code compare 3 < 15 (constante ignorant la config).
    """
    from backend.app.database import SessionLocal
    from backend.app.models import Alarm, EscalationConfig, User
    from backend.app.escalation import _apply_oncall_heartbeat
    from backend.app.clock import now as clock_now

    # 1) Admin change la config (reproduction issue #24, etape 2)
    r = client.post(
        "/api/config/system",
        json={"key": "oncall_offline_delay_minutes", "value": "2"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    # Sanity (reproduction issue #24, etape 3 : GET confirme)
    r = client.get("/api/config/system")
    assert r.status_code == 200
    assert r.json().get("oncall_offline_delay_minutes") == "2", (
        f"la config doit etre persistee a '2', got {r.json().get('oncall_offline_delay_minutes')!r}"
    )

    # 2) Setup : user1 offline 3 min, user2 online, aucune alarme existante
    db = SessionLocal()
    created_alarm_id = None
    try:
        chain = db.query(EscalationConfig).order_by(EscalationConfig.position).all()
        assert len(chain) >= 2, (
            f"seed chain should have >=2 entries, got {[(c.position, c.user_id) for c in chain]}"
        )
        pos1_user_id = chain[0].user_id
        pos2_user_id = chain[1].user_id

        # Nettoyer toute alarme active laissee par un test precedent
        # (isolation INV-902 : ne jamais dependre de l'ordre des tests).
        active = (
            db.query(Alarm)
            .filter(Alarm.status.in_(["active", "escalated"]))
            .all()
        )
        for a in active:
            a.status = "resolved"
        db.commit()

        now_ref = clock_now()
        u1 = db.query(User).filter(User.id == pos1_user_id).first()
        u2 = db.query(User).filter(User.id == pos2_user_id).first()
        assert u1 is not None and u2 is not None
        u1.is_online = False
        u1.last_heartbeat = now_ref - timedelta(minutes=3)
        u2.is_online = True
        u2.last_heartbeat = now_ref
        db.commit()

        # 3) Cycle de surveillance oncall (cf escalation_loop section 4)
        _apply_oncall_heartbeat(db, now_ref, chain)
        db.expire_all()  # relire l'etat apres commit interne

        # 4) Verification : l'alarme oncall doit exister car 3 min > 2 min configures
        oncall_alarm = (
            db.query(Alarm)
            .filter(
                Alarm.is_oncall_alarm == True,  # noqa: E712 (SQLAlchemy)
                Alarm.status.in_(["active", "escalated"]),
            )
            .first()
        )
        assert oncall_alarm is not None, (
            "INV-084 (bug issue #24) : avec oncall_offline_delay_minutes=2 et "
            "user1 offline depuis 3 min, une alarme is_oncall_alarm=True doit "
            "etre creee. Elle ne l'est pas -> le delai configure est ignore au "
            "profit d'une constante hardcodee."
        )
        assert oncall_alarm.assigned_user_id == pos2_user_id, (
            f"INV-052 : alarme oncall doit etre assignee au user pos 2 "
            f"(user_id={pos2_user_id}), got {oncall_alarm.assigned_user_id}"
        )
        created_alarm_id = oncall_alarm.id
    finally:
        # Cleanup deterministe : remettre users online + resoudre l'alarme
        # + restaurer la config par defaut pour ne pas polluer les tests suivants.
        if created_alarm_id is not None:
            a = db.query(Alarm).filter(Alarm.id == created_alarm_id).first()
            if a is not None:
                a.status = "resolved"
        for user in db.query(User).all():
            user.is_online = True
            user.last_heartbeat = clock_now()
        db.commit()
        db.close()

        client.post(
            "/api/config/system",
            json={"key": "oncall_offline_delay_minutes", "value": "15"},
            headers=admin_headers,
        )
