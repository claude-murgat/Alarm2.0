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
    """Ajoute les colonnes manquantes sur les tables existantes (idempotent)."""
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
