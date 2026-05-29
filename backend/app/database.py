import os
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

logger = logging.getLogger("database")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/alarm.db")

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def run_migrations(engine):
    """Ajoute les colonnes/tables manquantes sur les tables existantes (idempotent)."""
    is_sqlite = engine.dialect.name == "sqlite"
    with engine.connect() as conn:
        if is_sqlite:
            # SQLite : vérifier la présence de la colonne avant ALTER
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
            if "phone_number" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN phone_number VARCHAR"))
                conn.commit()
                logger.info("Migration: users.phone_number ajoutée")
        else:
            # PostgreSQL supporte IF NOT EXISTS
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number VARCHAR"))
            conn.commit()
            logger.info("Migration: users.phone_number vérifiée/ajoutée")

        # ── Migration : table alarm_notifications ──────────────────────────
        _migrate_alarm_notifications(conn, is_sqlite)

        # ── Migration : table device_tokens ───────────────────────────────
        _migrate_device_tokens(conn, is_sqlite)

        # ── Migration : table audit_events ────────────────────────────────
        _migrate_audit_events(conn, is_sqlite)

        # ── Migration : table call_queue ──────────────────────────────────
        _migrate_call_queue(conn, is_sqlite)

        # ── Migration : colonnes sms_sent/call_sent sur alarm_notifications
        _migrate_alarm_notifications_sms_call(conn, is_sqlite)

        # ── INV-120 V2 : table gateway_states ─────────────────────────────
        _migrate_gateway_states(conn, is_sqlite)

        # ── INV-120 V2 : colonne alarms.source (+ backfill oncall/api) ────
        _migrate_alarm_source(conn, is_sqlite)

        # ── INV-123 : colonnes alarms.sensor_dissensus_since/_email_sent_at
        _migrate_alarm_sensor_dissensus(conn, is_sqlite)

        # ── INV-056 : table connectivity_events (transitions online/offline)
        _migrate_connectivity_events(conn, is_sqlite)

        # ── INV-085 : table quorum_state (singleton, persistance incident)
        _migrate_quorum_state(conn, is_sqlite)


def _migrate_alarm_notifications(conn, is_sqlite: bool):
    """Crée la table alarm_notifications et migre les données CSV si nécessaire."""
    # Vérifier si la table existe déjà
    if is_sqlite:
        exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='alarm_notifications'")
        ).fetchone()
    else:
        exists = conn.execute(
            text("SELECT to_regclass('public.alarm_notifications')")
        ).fetchone()
        exists = exists[0] if exists else None

    if not exists:
        conn.execute(text("""
            CREATE TABLE alarm_notifications (
                id SERIAL PRIMARY KEY,
                alarm_id INTEGER NOT NULL REFERENCES alarms(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (alarm_id, user_id)
            )
        """))
        conn.commit()
        logger.info("Migration: table alarm_notifications créée")

        # Migrer les données CSV existantes vers la nouvelle table
        rows = conn.execute(text("SELECT id, notified_user_ids FROM alarms WHERE notified_user_ids IS NOT NULL AND notified_user_ids != ''")).fetchall()
        for row in rows:
            alarm_id = row[0]
            csv_ids = row[1]
            for uid_str in csv_ids.split(","):
                uid_str = uid_str.strip()
                if uid_str:
                    try:
                        uid = int(uid_str)
                        conn.execute(text(
                            "INSERT INTO alarm_notifications (alarm_id, user_id) VALUES (:aid, :uid) ON CONFLICT DO NOTHING"
                        ), {"aid": alarm_id, "uid": uid})
                    except (ValueError, Exception):
                        pass
        conn.commit()
        logger.info(f"Migration: {len(rows)} alarmes migrées vers alarm_notifications")


def _migrate_device_tokens(conn, is_sqlite: bool):
    """Cree la table device_tokens si elle n'existe pas (idempotent)."""
    if is_sqlite:
        exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='device_tokens'")
        ).fetchone()
    else:
        exists = conn.execute(
            text("SELECT to_regclass('public.device_tokens')")
        ).fetchone()
        exists = exists[0] if exists else None

    if not exists:
        if is_sqlite:
            conn.execute(text("""
                CREATE TABLE device_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    fcm_token VARCHAR NOT NULL,
                    device_id VARCHAR NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, device_id)
                )
            """))
        else:
            conn.execute(text("""
                CREATE TABLE device_tokens (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    fcm_token VARCHAR NOT NULL,
                    device_id VARCHAR NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, device_id)
                )
            """))
        conn.commit()
        logger.info("Migration: table device_tokens creee")


def _migrate_audit_events(conn, is_sqlite: bool):
    """Cree la table audit_events si elle n'existe pas (idempotent)."""
    if is_sqlite:
        exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_events'")
        ).fetchone()
    else:
        exists = conn.execute(
            text("SELECT to_regclass('public.audit_events')")
        ).fetchone()
        exists = exists[0] if exists else None

    if not exists:
        if is_sqlite:
            conn.execute(text("""
                CREATE TABLE audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alarm_id INTEGER REFERENCES alarms(id) ON DELETE SET NULL,
                    event_type VARCHAR NOT NULL,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    details VARCHAR,
                    correlation_id VARCHAR
                )
            """))
        else:
            conn.execute(text("""
                CREATE TABLE audit_events (
                    id SERIAL PRIMARY KEY,
                    alarm_id INTEGER REFERENCES alarms(id) ON DELETE SET NULL,
                    event_type VARCHAR NOT NULL,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    details VARCHAR,
                    correlation_id VARCHAR
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_event_type ON audit_events (event_type)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_timestamp ON audit_events (timestamp)"))
        conn.commit()
        logger.info("Migration: table audit_events creee")


def _migrate_call_queue(conn, is_sqlite: bool):
    """Cree la table call_queue si elle n'existe pas (idempotent)."""
    if is_sqlite:
        exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='call_queue'")
        ).fetchone()
    else:
        exists = conn.execute(
            text("SELECT to_regclass('public.call_queue')")
        ).fetchone()
        exists = exists[0] if exists else None

    if not exists:
        if is_sqlite:
            conn.execute(text("""
                CREATE TABLE call_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    to_number VARCHAR NOT NULL,
                    alarm_id INTEGER NOT NULL REFERENCES alarms(id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    tts_message VARCHAR NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    called_at TIMESTAMP,
                    result VARCHAR,
                    retries INTEGER DEFAULT 0
                )
            """))
        else:
            conn.execute(text("""
                CREATE TABLE call_queue (
                    id SERIAL PRIMARY KEY,
                    to_number VARCHAR NOT NULL,
                    alarm_id INTEGER NOT NULL REFERENCES alarms(id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    tts_message VARCHAR NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    called_at TIMESTAMP,
                    result VARCHAR,
                    retries INTEGER DEFAULT 0
                )
            """))
        conn.commit()
        logger.info("Migration: table call_queue creee")


def _migrate_alarm_notifications_sms_call(conn, is_sqlite: bool):
    """Ajoute les colonnes sms_sent et call_sent a alarm_notifications (idempotent)."""
    if is_sqlite:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(alarm_notifications)")).fetchall()]
        if "sms_sent" not in cols:
            conn.execute(text("ALTER TABLE alarm_notifications ADD COLUMN sms_sent BOOLEAN DEFAULT FALSE"))
            conn.commit()
            logger.info("Migration: alarm_notifications.sms_sent ajoutee")
        if "call_sent" not in cols:
            conn.execute(text("ALTER TABLE alarm_notifications ADD COLUMN call_sent BOOLEAN DEFAULT FALSE"))
            conn.commit()
            logger.info("Migration: alarm_notifications.call_sent ajoutee")
    else:
        conn.execute(text("ALTER TABLE alarm_notifications ADD COLUMN IF NOT EXISTS sms_sent BOOLEAN DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE alarm_notifications ADD COLUMN IF NOT EXISTS call_sent BOOLEAN DEFAULT FALSE"))
        conn.commit()
        logger.info("Migration: alarm_notifications.sms_sent/call_sent verifiees/ajoutees")


def _migrate_gateway_states(conn, is_sqlite: bool):
    """INV-120 V2 : crée la table gateway_states (état rapporté par chaque
    gateway, source de vérité pour la reconciliation level-based)."""
    if is_sqlite:
        exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='gateway_states'")
        ).fetchone()
    else:
        exists = conn.execute(
            text("SELECT to_regclass('public.gateway_states')")
        ).fetchone()
        exists = exists[0] if exists else None

    if not exists:
        conn.execute(text("""
            CREATE TABLE gateway_states (
                gateway_id VARCHAR PRIMARY KEY,
                state VARCHAR NOT NULL,
                last_seen TIMESTAMP NOT NULL
            )
        """))
        conn.commit()
        logger.info("Migration: table gateway_states creee (INV-120 V2)")


def _migrate_alarm_source(conn, is_sqlite: bool):
    """INV-120 V2 : ajoute alarms.source + backfill rows existantes.

    Backfill : is_oncall_alarm=TRUE → 'oncall', sinon 'api'. Aucune alarme
    historique 'gateway_dry_contact' (V1 n'écrivait pas le champ)."""
    if is_sqlite:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(alarms)")).fetchall()]
        if "source" not in cols:
            conn.execute(text("ALTER TABLE alarms ADD COLUMN source VARCHAR NOT NULL DEFAULT 'api'"))
            conn.commit()
            logger.info("Migration: alarms.source ajoutee (INV-120 V2)")
    else:
        conn.execute(text(
            "ALTER TABLE alarms ADD COLUMN IF NOT EXISTS source VARCHAR NOT NULL DEFAULT 'api'"
        ))
        conn.commit()
        logger.info("Migration: alarms.source verifiee/ajoutee (INV-120 V2)")

    conn.execute(text(
        "UPDATE alarms SET source = 'oncall' WHERE is_oncall_alarm = TRUE AND source = 'api'"
    ))
    conn.commit()


def _migrate_alarm_sensor_dissensus(conn, is_sqlite: bool):
    """INV-123 : ajoute alarms.sensor_dissensus_since + _email_sent_at
    (toutes NULL au backfill — V1 ne savait pas détecter)."""
    if is_sqlite:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(alarms)")).fetchall()]
        if "sensor_dissensus_since" not in cols:
            conn.execute(text("ALTER TABLE alarms ADD COLUMN sensor_dissensus_since TIMESTAMP"))
            conn.commit()
            logger.info("Migration: alarms.sensor_dissensus_since ajoutee (INV-123)")
        if "sensor_dissensus_email_sent_at" not in cols:
            conn.execute(text("ALTER TABLE alarms ADD COLUMN sensor_dissensus_email_sent_at TIMESTAMP"))
            conn.commit()
            logger.info("Migration: alarms.sensor_dissensus_email_sent_at ajoutee (INV-123)")
    else:
        conn.execute(text(
            "ALTER TABLE alarms ADD COLUMN IF NOT EXISTS sensor_dissensus_since TIMESTAMP"
        ))
        conn.execute(text(
            "ALTER TABLE alarms ADD COLUMN IF NOT EXISTS sensor_dissensus_email_sent_at TIMESTAMP"
        ))
        conn.commit()
        logger.info("Migration: alarms.sensor_dissensus_* verifiees/ajoutees (INV-123)")


def _migrate_connectivity_events(conn, is_sqlite: bool):
    """INV-056 : crée la table connectivity_events qui trace chaque transition
    online <-> offline d'un user (remplace l'alarme oncall_offline INV-050
    dépréciée le 2026-05-26)."""
    if is_sqlite:
        exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='connectivity_events'")
        ).fetchone()
    else:
        exists = conn.execute(
            text("SELECT to_regclass('public.connectivity_events')")
        ).fetchone()
        exists = exists[0] if exists else None

    if not exists:
        conn.execute(text("""
            CREATE TABLE connectivity_events (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event VARCHAR NOT NULL,
                ts TIMESTAMP NOT NULL
            )
        """) if not is_sqlite else text("""
            CREATE TABLE connectivity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event VARCHAR NOT NULL,
                ts TIMESTAMP NOT NULL
            )
        """))
        conn.execute(text(
            "CREATE INDEX idx_connectivity_events_user_ts "
            "ON connectivity_events(user_id, ts DESC)"
        ))
        conn.commit()
        logger.info("Migration: table connectivity_events creee (INV-056)")


def _migrate_quorum_state(conn, is_sqlite: bool):
    """INV-085 : crée la table quorum_state (singleton id=1) qui persiste
    l'incident quorum en cours à travers les redémarrages backend.

    - lost_since : début de la série non-saine continue (NULL si sain).
    - email_sent_at : timestamp 1er email d'alerte (NULL = pas encore envoyé).
    - reminders_sent_at : JSON liste des fenêtres reminders envoyées (secondes).
    """
    if is_sqlite:
        exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='quorum_state'")
        ).fetchone()
    else:
        exists = conn.execute(
            text("SELECT to_regclass('public.quorum_state')")
        ).fetchone()
        exists = exists[0] if exists else None

    if not exists:
        conn.execute(text("""
            CREATE TABLE quorum_state (
                id INTEGER PRIMARY KEY,
                lost_since TIMESTAMP,
                email_sent_at TIMESTAMP,
                reminders_sent_at VARCHAR NOT NULL DEFAULT '[]'
            )
        """))
        # Singleton : insert la row id=1 vide
        conn.execute(text(
            "INSERT INTO quorum_state (id, lost_since, email_sent_at, reminders_sent_at) "
            "VALUES (1, NULL, NULL, '[]')"
        ))
        conn.commit()
        logger.info("Migration: table quorum_state creee + row singleton (INV-085)")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
