"""
Tier 2 integration test : INV-084 (issue #75) — `escalation_tick_seconds` et
`watchdog_tick_seconds` doivent etre lus depuis SystemConfig a chaque iteration
de leur boucle respective.

Pourquoi (issue #75) :
- Avant ce fix, `escalation.py` faisait `await asyncio.sleep(10)` en dur et
  `watchdog.py` faisait `await asyncio.sleep(30)` en dur. Modifier la config
  admin n'avait AUCUN effet observable sur la cadence.
- Surtout : empechait d'accelerer les tests. Avec `escalation_tick_seconds=1`,
  une attente metier de 10s par tick passe a 1s — gain massif sur la suite tier 3.

Decision projet (2026-05-12) : 2 cles separees, pas une cle partagee. Permet
des leviers independants ("frequence escalade" != "frequence check offline").

Invariant vise : INV-084 — « Aucun delai metier ne doit etre hardcode.
Chaque valeur est lue depuis SystemConfig a chaque usage. »

Budget P4 : 4 tests cibles (un par cle + un seed/default + un fallback invalide).

Approche : appelle directement les helpers `_get_escalation_tick_seconds` et
`_get_watchdog_tick_seconds` qui sont consommes par les boucles asyncio. Les
tester par timing reel des `asyncio.sleep()` serait flaky (P6 interdit) ; ces
helpers sont le bon point d'observation parce qu'ils encapsulent la lecture
SystemConfig que la boucle effectue a chaque iteration.
"""
import pytest

pytestmark = pytest.mark.integration


def test_escalation_tick_seconds_read_from_system_config(client, admin_headers):
    """INV-084 : la cadence de la boucle d'escalade doit etre lue depuis
    SystemConfig, pas figee sur la constante hardcodee.

    RED avant fix : `_get_escalation_tick_seconds` n'existe pas et le code
    fait `await asyncio.sleep(10)` en dur. Modifier la config ne change rien.
    GREEN apres fix : la fonction lit la cle SystemConfig avec fallback default.
    """
    from backend.app.escalation import (
        _get_escalation_tick_seconds,
        ESCALATION_TICK_SECONDS_DEFAULT,
    )
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig

    # Sanity : le default est bien 10s (cf decision projet)
    assert ESCALATION_TICK_SECONDS_DEFAULT == 10.0, (
        f"Le default doit rester 10s (decision 2026-05-12), got "
        f"{ESCALATION_TICK_SECONDS_DEFAULT}"
    )

    # Admin pousse une nouvelle cadence via /api/config/system
    # (1s : valeur typique pour accelerer les tests).
    r = client.post(
        "/api/config/system",
        json={"key": "escalation_tick_seconds", "value": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        # La fonction utilisee a chaque iteration de la boucle d'escalade
        # doit refleter la nouvelle valeur immediatement (pas de cache).
        observed = _get_escalation_tick_seconds(db)
        assert observed == 1.0, (
            "INV-084 (issue #75) : apres POST escalation_tick_seconds=1, "
            f"la lecture doit retourner 1.0, got {observed}. "
            "Le code utilise probablement encore la constante hardcodee."
        )
    finally:
        # Restaure le default pour ne pas polluer les tests suivants
        # (INV-902 : independance d'ordre).
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "escalation_tick_seconds"
        ).first()
        if cfg is not None:
            cfg.value = "10"
            db.commit()
        db.close()


def test_watchdog_tick_seconds_read_from_system_config(client, admin_headers):
    """INV-084 : la cadence du watchdog doit etre lue depuis SystemConfig,
    independamment de `escalation_tick_seconds` (decision 2026-05-12 : 2 cles).

    RED avant fix : `_get_watchdog_tick_seconds` n'existe pas et le code fait
    `await asyncio.sleep(30)` en dur. Modifier la config ne change rien.
    GREEN apres fix : la fonction lit la cle SystemConfig avec fallback default.
    """
    from backend.app.watchdog import (
        _get_watchdog_tick_seconds,
        WATCHDOG_TICK_SECONDS_DEFAULT,
    )
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig

    # Sanity : le default est bien 30s (cf decision projet)
    assert WATCHDOG_TICK_SECONDS_DEFAULT == 30.0, (
        f"Le default doit rester 30s (decision 2026-05-12), got "
        f"{WATCHDOG_TICK_SECONDS_DEFAULT}"
    )

    # Admin pousse une nouvelle cadence (5s : valeur typique pour les tests).
    r = client.post(
        "/api/config/system",
        json={"key": "watchdog_tick_seconds", "value": "5"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        observed = _get_watchdog_tick_seconds(db)
        assert observed == 5.0, (
            "INV-084 (issue #75) : apres POST watchdog_tick_seconds=5, "
            f"la lecture doit retourner 5.0, got {observed}. "
            "Le code utilise probablement encore la constante hardcodee."
        )

        # Cle separee : modifier watchdog_tick_seconds ne doit pas affecter
        # escalation_tick_seconds (verification de l'isolement, decision 2 cles).
        from backend.app.escalation import _get_escalation_tick_seconds
        observed_escalation = _get_escalation_tick_seconds(db)
        assert observed_escalation != 5.0, (
            "Decision projet 2026-05-12 : 2 cles separees. "
            "Modifier watchdog_tick_seconds ne doit pas changer la cadence "
            "d'escalade. Si les deux sont a 5.0, le code utilise probablement "
            "une cle partagee — bug de partage de cle."
        )
    finally:
        # Restaure le default.
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "watchdog_tick_seconds"
        ).first()
        if cfg is not None:
            cfg.value = "30"
            db.commit()
        db.close()


def test_tick_seconds_fallback_to_default_when_key_absent(client, admin_headers):
    """INV-084 : si la cle SystemConfig est absente (DB legacy ou suppression),
    les helpers doivent retourner le default au lieu de planter.

    Garde-fou contre une regression future qui ferait `float(cfg.value)` sans
    check du None.
    """
    from backend.app.escalation import (
        _get_escalation_tick_seconds,
        ESCALATION_TICK_SECONDS_DEFAULT,
    )
    from backend.app.watchdog import (
        _get_watchdog_tick_seconds,
        WATCHDOG_TICK_SECONDS_DEFAULT,
    )
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig

    db = SessionLocal()
    saved_esc = None
    saved_wd = None
    try:
        # Snapshot + suppression temporaire des 2 cles
        for key in ("escalation_tick_seconds", "watchdog_tick_seconds"):
            cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
            if cfg is not None:
                if key == "escalation_tick_seconds":
                    saved_esc = cfg.value
                else:
                    saved_wd = cfg.value
                db.delete(cfg)
        db.commit()

        # Sans cle, les helpers doivent renvoyer leurs defaults respectifs.
        assert _get_escalation_tick_seconds(db) == ESCALATION_TICK_SECONDS_DEFAULT, (
            "Sans cle escalation_tick_seconds en DB, le helper doit retourner "
            f"le default ({ESCALATION_TICK_SECONDS_DEFAULT})."
        )
        assert _get_watchdog_tick_seconds(db) == WATCHDOG_TICK_SECONDS_DEFAULT, (
            "Sans cle watchdog_tick_seconds en DB, le helper doit retourner "
            f"le default ({WATCHDOG_TICK_SECONDS_DEFAULT})."
        )
    finally:
        # Restaure les cles supprimees pour ne pas casser les tests suivants
        # qui dependent du seed (INV-902).
        if saved_esc is not None:
            db.add(SystemConfig(key="escalation_tick_seconds", value=saved_esc))
        if saved_wd is not None:
            db.add(SystemConfig(key="watchdog_tick_seconds", value=saved_wd))
        db.commit()
        db.close()


def test_tick_seconds_fallback_when_value_is_invalid(client, admin_headers):
    """INV-084 : si SystemConfig contient une valeur non-numerique (saisie admin
    invalide ou data corrompue), les helpers doivent retourner le default plutot
    que de lever ValueError et faire crasher la boucle.

    L'endpoint POST /api/config/system n'a aucune validation (proxy brut,
    cf backend/app/api/config.py:181-195 : `existing.value = config.value`),
    donc la garde try/except dans les helpers est la SEULE defense. Ce test
    verrouille cette garde contre un mutant qui la supprimerait — sinon la
    boucle escalade/watchdog crasherait en silence et INV-050 (continuite
    oncall) serait compromis par effet de bord.
    """
    from backend.app.escalation import (
        _get_escalation_tick_seconds,
        ESCALATION_TICK_SECONDS_DEFAULT,
    )
    from backend.app.watchdog import (
        _get_watchdog_tick_seconds,
        WATCHDOG_TICK_SECONDS_DEFAULT,
    )
    from backend.app.database import SessionLocal
    from backend.app.models import SystemConfig

    # Pousser une valeur invalide via l'endpoint admin (pas via DB directe :
    # on veut prouver que le chemin admin reel ne crashe pas la boucle).
    try:
        for key in ("escalation_tick_seconds", "watchdog_tick_seconds"):
            r = client.post(
                "/api/config/system",
                json={"key": key, "value": "abc"},
                headers=admin_headers,
            )
            # L'endpoint est un proxy brut, il accepte n'importe quoi (200 OK).
            # C'est precisement le scenario que cette garde protege.
            assert r.status_code == 200, (
                f"POST /api/config/system {key}=abc devrait reussir "
                f"(endpoint sans validation), got {r.status_code}: {r.text}"
            )

        db = SessionLocal()
        try:
            # La garde try/except (TypeError, ValueError) DOIT renvoyer le default
            # plutot que de propager l'exception et tuer la boucle.
            observed_esc = _get_escalation_tick_seconds(db)
            assert observed_esc == ESCALATION_TICK_SECONDS_DEFAULT, (
                "INV-084 : avec escalation_tick_seconds='abc' en DB, le helper "
                f"doit fallback sur le default ({ESCALATION_TICK_SECONDS_DEFAULT}), "
                f"got {observed_esc}. Si ce test echoue avec une ValueError, "
                "la garde try/except a ete supprimee — la boucle d'escalade "
                "crasherait en prod sur une saisie admin invalide."
            )

            observed_wd = _get_watchdog_tick_seconds(db)
            assert observed_wd == WATCHDOG_TICK_SECONDS_DEFAULT, (
                "INV-084 : avec watchdog_tick_seconds='abc' en DB, le helper "
                f"doit fallback sur le default ({WATCHDOG_TICK_SECONDS_DEFAULT}), "
                f"got {observed_wd}. Idem : sans la garde, la boucle watchdog "
                "crasherait en silence (-> INV-050 viole par effet de bord)."
            )
        finally:
            db.close()
    finally:
        # Restaure les defaults proprement, quel que soit l'issue du test
        # (INV-902 : independance d'ordre).
        db = SessionLocal()
        try:
            for key, default_value in (
                ("escalation_tick_seconds", "10"),
                ("watchdog_tick_seconds", "30"),
            ):
                cfg = db.query(SystemConfig).filter(SystemConfig.key == key).first()
                if cfg is not None:
                    cfg.value = default_value
            db.commit()
        finally:
            db.close()
