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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
