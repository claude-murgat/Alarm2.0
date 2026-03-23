import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from .database import engine, Base, SessionLocal
from .models import User, EscalationConfig, SystemConfig
from .auth import hash_password
from .api.users import router as auth_router, users_router
from .api.alarms import router as alarms_router
from .api.devices import router as devices_router
from .api.config import router as config_router
from .api.test_api import router as test_router
from .escalation import escalation_loop
from .watchdog import watchdog_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alarm_system")


def seed_data():
    """Create default users and escalation config if DB is empty."""
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            users = [
                User(email="admin@alarm.local", hashed_password=hash_password("admin123"), name="Admin", is_admin=True),
                User(email="user1@alarm.local", hashed_password=hash_password("user123"), name="User 1"),
                User(email="user2@alarm.local", hashed_password=hash_password("user123"), name="User 2"),
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
    seed_data()

    escalation_task = asyncio.create_task(escalation_loop())
    watchdog_task = asyncio.create_task(watchdog_loop())
    logger.info("Background tasks started: escalation + watchdog")

    yield

    # Shutdown
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


@app.get("/", response_class=HTMLResponse)
async def root():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    return {"status": "ok"}
