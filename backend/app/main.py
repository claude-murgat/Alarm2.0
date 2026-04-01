import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from .database import engine, Base, SessionLocal, run_migrations
from .models import User, EscalationConfig, SystemConfig
from .auth import hash_password
from .api.users import router as auth_router, users_router
from .api.alarms import router as alarms_router
from .api.devices import router as devices_router
from .api.config import router as config_router
from .api.test_api import router as test_router
from .api.sms import router as sms_router
from .escalation import escalation_loop
from .watchdog import watchdog_loop
from .leader_election import leader_election_loop, is_leader
from .database import DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alarm_system")


def seed_data():
    """Create default users and escalation config if DB is empty."""
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            users = [
                User(hashed_password=hash_password("admin123"), name="admin", is_admin=True),
                User(hashed_password=hash_password("user123"), name="user1"),
                User(hashed_password=hash_password("user123"), name="user2"),
            ]
            db.add_all(users)
            db.commit()

            # Set up escalation chain: user1 -> user2 -> admin
            for user in users:
                db.refresh(user)

            escalation = [
                EscalationConfig(position=1, user_id=users[1].id, delay_minutes=15.0),
                EscalationConfig(position=2, user_id=users[2].id, delay_minutes=15.0),
                EscalationConfig(position=3, user_id=users[0].id, delay_minutes=15.0),
            ]
            db.add_all(escalation)

            # Default system config
            db.add(SystemConfig(key="escalation_delay_minutes", value="15"))
            db.add(SystemConfig(key="watchdog_timeout_seconds", value="60"))
            db.add(SystemConfig(key="ack_suspension_minutes", value="30"))
            db.commit()
            logger.info("Seed data created: 3 users, escalation chain configured")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    seed_data()

    # Démarrer l'élection de leader en premier — les autres coroutines attendent l'event
    election_task = asyncio.create_task(leader_election_loop(DATABASE_URL))
    escalation_task = asyncio.create_task(escalation_loop())
    watchdog_task = asyncio.create_task(watchdog_loop())
    logger.info("Background tasks started: leader_election + escalation + watchdog")

    yield

    # Shutdown
    election_task.cancel()
    escalation_task.cancel()
    watchdog_task.cancel()


app = FastAPI(title="Critical Alarm System", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routers
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(alarms_router)
app.include_router(devices_router)
app.include_router(config_router)
app.include_router(test_router)
app.include_router(sms_router)


@app.get("/", response_class=HTMLResponse)
async def root():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    from .escalation import last_tick_at
    from .clock import now as clock_now

    # Vérifier que la DB est accessible
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db_ok = True
        db.close()
    except Exception:
        pass

    # Vérifier que la boucle d'escalade tourne (dernier tick < 120s)
    loop_ok = (
        last_tick_at is not None
        and (clock_now() - last_tick_at).total_seconds() < 120
    )

    # Le rôle dépend du lock advisory — peut changer dynamiquement
    role = "primary" if is_leader.is_set() else "secondary"

    if not db_ok or not loop_ok:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "db": db_ok, "escalation_loop": loop_ok, "role": role}
        )
    return {"status": "ok", "db": True, "escalation_loop": True, "role": role}
