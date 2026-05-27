"""
Tier 2 integration test : INV-084 — oncall_offline_delay_minutes lu depuis SystemConfig.

Couvre l'issue #24 : quand un admin change `oncall_offline_delay_minutes`
via POST /api/config/system, la nouvelle valeur doit etre effectivement utilisee
par la surveillance oncall.

Note 2026-05-26 : INV-050/051/052/054 deprecies (cf tests/INVARIANTS.md §5
encadre "Changement de strategie 2026-05-26"). Le test originel verifiait que
la clef `oncall_offline_delay_minutes` etait lue via la creation d'alarme
oncall_offline. Comme cette creation a ete supprimee, on adapte : on verifie
que la cle est utilisee comme delai du declenchement INV-053 (email
"personne en ligne") via le marker SystemConfig.

Invariant vise : INV-084 — « Aucun delai metier ne doit etre hardcode. Chaque
valeur est lue depuis SystemConfig a chaque usage, pour qu'un changement admin
prenne effet immediat. »

Budget P4 : 1 test cible qui prouve le comportement attendu.
"""
from datetime import timedelta

import pytest

pytestmark = pytest.mark.integration


def test_oncall_delay_read_from_system_config_not_hardcoded(client, admin_headers):
    """INV-084 + INV-053 : `oncall_offline_delay_minutes` doit etre lu depuis
    SystemConfig a chaque evaluation, pas depuis une constante hardcodee.

    Scenario adapte post-deprecation INV-050 (2026-05-26) :
      - Admin POST /api/config/system {oncall_offline_delay_minutes: 2}
      - User1 (pos 1, de garde) est offline depuis 3 min (> 2, < 15)
      - TOUS les autres users sont offline aussi (declenche INV-053)
      - Un cycle de surveillance oncall tourne

    Attendu : le marker `nobody_online_email_sent_at` est pose dans SystemConfig,
    preuve que la branche INV-053 a ete prise (donc le delai 2 min de la config
    a ete lu et applique).
    Buggy : aucun marker car le code compare 3 < 15 (constante ignorant la config).
    """
    import asyncio
    from backend.app.database import SessionLocal
    from backend.app.models import EscalationConfig, SystemConfig, User
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

    # 2) Setup : user1 offline 3 min, TOUS les autres offline (declenche INV-053)
    db = SessionLocal()
    try:
        chain = db.query(EscalationConfig).order_by(EscalationConfig.position).all()
        assert len(chain) >= 2, (
            f"seed chain should have >=2 entries, got {[(c.position, c.user_id) for c in chain]}"
        )

        now_ref = clock_now()
        # Mettre TOUS les users offline avec un last_heartbeat ancien
        for user in db.query(User).all():
            user.is_online = False
            user.last_heartbeat = now_ref - timedelta(minutes=3)
        db.commit()

        # Effacer le marker s'il etait pose par un test precedent
        marker = db.query(SystemConfig).filter(
            SystemConfig.key == "nobody_online_email_sent_at"
        ).first()
        if marker is not None:
            marker.value = ""
            db.commit()

        # 3) Cycle de surveillance oncall (cf escalation_loop section 4)
        # Bug #105 : _apply_oncall_heartbeat est devenu async (envoi SMTP via
        # asyncio.to_thread pour ne pas geler l'event loop). On le pilote ici
        # via asyncio.run depuis un contexte de test sync.
        asyncio.run(_apply_oncall_heartbeat(db, now_ref, chain))
        db.expire_all()  # relire l'etat apres commit interne

        # 4) Verification : le marker INV-053 est pose, preuve que le delai 2 min
        # configure a bien ete lu (sinon : 3 min < 15 min hardcode, branche pas prise).
        marker = db.query(SystemConfig).filter(
            SystemConfig.key == "nobody_online_email_sent_at"
        ).first()
        assert marker is not None and marker.value, (
            "INV-084 (bug issue #24, adapte 2026-05-26) : avec "
            "oncall_offline_delay_minutes=2 et tous users offline depuis 3 min, "
            "INV-053 doit declencher l'email + poser le marker "
            "`nobody_online_email_sent_at`. Le marker est absent ou vide -> le "
            "delai configure est ignore au profit d'une constante hardcodee."
        )
    finally:
        # Cleanup deterministe : remettre users online + clear marker
        # + restaurer la config par defaut pour ne pas polluer les tests suivants.
        for user in db.query(User).all():
            user.is_online = True
            user.last_heartbeat = clock_now()
        marker = db.query(SystemConfig).filter(
            SystemConfig.key == "nobody_online_email_sent_at"
        ).first()
        if marker is not None:
            marker.value = ""
        db.commit()
        db.close()

        client.post(
            "/api/config/system",
            json={"key": "oncall_offline_delay_minutes", "value": "15"},
            headers=admin_headers,
        )
