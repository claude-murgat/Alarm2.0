"""Tests d'intégration tier 2 — issue #144 / INV-056 regression schema init.

Bug observé en prod 2026-05-29 ~08:48 UTC : la table `connectivity_events`
était absente sur le primary Patroni node3 alors que le sha déployé contenait
bien le modèle `ConnectivityEvent` (PR #130 mergée 2026-05-26). Symptôme :
`psycopg2.errors.UndefinedTable: relation "connectivity_events" does not exist`
en boucle sur tout INSERT depuis le middleware connectivity → API 500.

Cause root (`backend/app/main.py` lifespan, version avant fix) :

    if is_leader.is_set():
        logger.info("Ce noeud est PRIMARY — init DB + seed")
        Base.metadata.create_all(bind=engine)
        run_migrations(engine)
        _migrate_alarm_original_created_at(engine)
        seed_data()
    else:
        # Replica : pas de create_all, juste attente DB

Pendant le canary deploy V1.6, Patroni a fait des micro-failovers à chaque
container recreate. Chaque node bootait en mode secondary (is_leader.is_set()
== False), donc `create_all` était sauté. Au prochain failover, un node devenu
leader n'avait jamais matérialisé les nouvelles tables → état stuck.

Bug structurel : tout nouveau modèle SQLAlchemy reste sans table en prod si
tous les nodes étaient secondary au boot du sha qui le contient. PR #130 a
déclenché le symptôme, mais le bug touche n'importe quel futur modèle.

Fix : `create_all` + `run_migrations` doivent s'exécuter sur TOUS les nodes
(idempotents en SQL : CREATE TABLE IF NOT EXISTS, ALTER TABLE ADD COLUMN
IF NOT EXISTS). Sur replica Patroni, l'erreur read-only est attrapée
proprement et loggée. Seul `seed_data()` (INSERT users) reste gardé par
is_leader pour éviter les doublons.

Budget P4 : 3 tests max — couverts ici.

Invariant adressé : INV-056 (la table connectivity_events DOIT exister sur
le leader après déploiement du sha qui contient son modèle). La structure
du fix garantit l'invariant pour tout futur modèle, pas seulement
ConnectivityEvent.
"""
import tempfile

import pytest
from sqlalchemy import create_engine, inspect

pytestmark = pytest.mark.integration


def _fresh_engine():
    """Engine SQLite isolé sur fichier temp — ne touche pas la DB du client
    fixture (session-scoped). Fichier persistant entre les inspects pour
    éviter qu'une fermeture de connexion ne purge la DB en mémoire."""
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db", prefix="alarm-bug144-")
    f.close()
    return create_engine(
        f"sqlite:///{f.name}",
        connect_args={"check_same_thread": False},
    )


def test_init_db_schema_creates_connectivity_events_table(client):
    """Issue #144 / INV-056 : `_init_db_schema(engine)` doit créer la table
    `connectivity_events` (et toutes les tables du `Base.metadata`) sur un
    engine vierge, indépendamment du rôle Patroni.

    C'est le point d'entrée que le `lifespan` appelle inconditionnellement
    après le fix — avant le fix, l'init était inline et gardée par
    `if is_leader.is_set()`, donc skipée sur tout node ayant booté en mode
    secondary (cas reproduit en prod le 2026-05-29).

    Prouve : après l'appel, la table existe vraiment dans la DB (pas juste
    une attribut Python).
    """
    from backend.app import main as main_mod

    engine = _fresh_engine()
    inspector = inspect(engine)
    assert not inspector.has_table("connectivity_events"), (
        "Setup invalide : engine fraîchement créé, aucune table attendue."
    )

    main_mod._init_db_schema(engine)

    inspector = inspect(engine)
    assert inspector.has_table("connectivity_events"), (
        "INV-056 / regression bug 2026-05-29 : la table connectivity_events "
        "DOIT exister après _init_db_schema(). Avant le fix de l'issue #144, "
        "ce code était gardé par `if is_leader.is_set()` dans le lifespan, "
        "donc skipé sur tout node ayant booté en mode secondary Patroni → "
        "aucun node n'avait initialisé le schéma pour le sha de PR #130."
    )
    # Vérif des tables core (assure que run_migrations a tourné aussi) :
    assert inspector.has_table("users"), (
        "users DOIT exister — create_all + run_migrations doivent avoir tourné."
    )
    assert inspector.has_table("alarms")
    assert inspector.has_table("audit_events"), (
        "audit_events vient de run_migrations() — preuve que les migrations "
        "post-create_all ont aussi été exécutées sur ce path inconditionnel."
    )


def test_init_db_schema_is_idempotent(client):
    """Issue #144 : `_init_db_schema` doit pouvoir être appelée plusieurs fois
    sans crasher. Cas couverts : redémarrage répété d'un backend, failover
    Patroni qui re-déclenche le path, retry boot après crash partiel.

    Idempotence garantie par SQLite via CREATE TABLE IF NOT EXISTS et par
    PostgreSQL via ALTER TABLE ... IF NOT EXISTS (cf `run_migrations`).
    """
    from backend.app import main as main_mod

    engine = _fresh_engine()

    # 1er appel : crée le schéma complet
    main_mod._init_db_schema(engine)
    inspector = inspect(engine)
    assert inspector.has_table("connectivity_events")
    assert inspector.has_table("users")

    # 2e appel : doit être no-op (tables existent déjà)
    main_mod._init_db_schema(engine)
    inspector = inspect(engine)
    assert inspector.has_table("connectivity_events")

    # 3e appel : stabilité — pas de drift sur des appels répétés
    main_mod._init_db_schema(engine)
    inspector = inspect(engine)
    assert inspector.has_table("connectivity_events")
    assert inspector.has_table("users")


def test_init_db_schema_swallows_replica_readonly_error(monkeypatch, caplog, client):
    """Issue #144 : sur un replica Patroni, toute tentative de DDL renvoie
    `psycopg2.errors.ReadOnlySqlTransaction` (wrappé en SQLAlchemy
    OperationalError). `_init_db_schema` doit attraper ce cas précis, logger
    en INFO, et NE PAS propager l'exception — sinon le fix introduirait un
    nouveau bug (boot replica qui crash).

    Cas adjacents non couverts par cette garde : OperationalError pour
    d'autres causes (connection lost, etc.) DOIT propager (re-raise).
    """
    from sqlalchemy.exc import OperationalError
    from backend.app import main as main_mod

    def fake_create_all(*args, **kwargs):
        # Simule psycopg2 ReadOnlySqlTransaction : msg contient "read-only"
        raise OperationalError(
            "CREATE TABLE connectivity_events (...)",
            {},
            Exception(
                "cannot execute CREATE TABLE in a read-only transaction"
            ),
        )

    monkeypatch.setattr(main_mod.Base.metadata, "create_all", fake_create_all)

    engine = _fresh_engine()

    # Doit retourner SANS lever — sinon le boot replica crashe
    with caplog.at_level("INFO"):
        main_mod._init_db_schema(engine)

    # Doit avoir loggé un message INFO mentionnant le skip (replica/read-only)
    matching = [
        r for r in caplog.records
        if "read-only" in r.getMessage().lower() or "replica" in r.getMessage().lower()
    ]
    assert matching, (
        f"Issue #144 : un message INFO 'skip replica read-only' attendu pour "
        f"tracer le cas sur les replicas Patroni. Logs vus : "
        f"{[r.getMessage() for r in caplog.records]}"
    )
