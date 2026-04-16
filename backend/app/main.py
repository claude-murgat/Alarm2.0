import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import text
from .database import engine, Base, SessionLocal, run_migrations
from .models import User, EscalationConfig, SystemConfig
from .auth import hash_password
from .logging_config import setup_logging, correlation_id_var
from .api.users import router as auth_router, users_router
from .api.alarms import router as alarms_router
from .api.devices import router as devices_router
from .api.config import router as config_router
from .api.test_api import router as test_router
from .api.sms import router as sms_router
from .api.audit import router as audit_router
from .escalation import escalation_loop
from .watchdog import watchdog_loop
from .leader_election import leader_election_loop, is_leader
from .database import DATABASE_URL

setup_logging()
logger = logging.getLogger("alarm_system")


class HeartbeatAccessLogFilter(logging.Filter):
    """Suppress uvicorn access log lines for /api/devices/heartbeat."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/devices/heartbeat" not in record.getMessage()


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
            db.add(SystemConfig(key="sms_call_delay_minutes", value="2"))
            db.commit()
            logger.info("Seed data created: 3 users, escalation chain configured")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    os.makedirs("data", exist_ok=True)

    # Filtrer les access logs heartbeat (trop frequents)
    logging.getLogger("uvicorn.access").addFilter(HeartbeatAccessLogFilter())

    # Demarrer le leader election en premier
    election_task = asyncio.create_task(leader_election_loop(DATABASE_URL))

    # Attendre que Patroni determine le role (max 30s)
    for _ in range(60):
        if is_leader.is_set():
            break
        await asyncio.sleep(0.5)

    if is_leader.is_set():
        # Primary : creer les tables et seeder
        logger.info("Ce noeud est PRIMARY — init DB + seed")
        Base.metadata.create_all(bind=engine)
        run_migrations(engine)
        seed_data()
    else:
        # Replica : attendre que la DB soit accessible en lecture
        logger.info("Ce noeud est REPLICA — attente DB en lecture")
        for _ in range(60):
            try:
                db = SessionLocal()
                db.execute(text("SELECT 1"))
                db.close()
                break
            except Exception:
                await asyncio.sleep(1)

    escalation_task = asyncio.create_task(escalation_loop())
    watchdog_task = asyncio.create_task(watchdog_loop())
    logger.info("Background tasks started: leader_election + escalation + watchdog")

    yield

    # Shutdown
    election_task.cancel()
    escalation_task.cancel()
    watchdog_task.cancel()


app = FastAPI(title="Alarme Murgat", version="1.0.0", lifespan=lifespan)

# CORS : origines autorisées depuis la variable d'env ALLOWED_ORIGINS (comma-separated).
# En dev, "*" est toléré mais déconseillé en production.
_raw_origins = os.getenv("ALLOWED_ORIGINS", "").strip()
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()] if _raw_origins else []

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Injects a correlation ID into every request for log tracing."""
    async def dispatch(self, request: Request, call_next):
        corr_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        correlation_id_var.set(corr_id)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = corr_id
        return response


app.add_middleware(CorrelationIdMiddleware)

# Register API routers
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(alarms_router)
app.include_router(devices_router)
app.include_router(config_router)
app.include_router(test_router)
app.include_router(sms_router)
from .api.calls import router as calls_router
app.include_router(calls_router)
app.include_router(audit_router)

from .api.stats import router as stats_router
app.include_router(stats_router)


@app.get("/", response_class=HTMLResponse)
async def root():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    from .escalation import last_tick_at
    from .clock import now as clock_now
    from .auth import SECRET_KEY
    from .api.test_api import ENABLE_TEST_ENDPOINTS

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

    # Flags de sécurité
    secret_key_default = (SECRET_KEY == "alarm-secret-key-change-in-prod")

    base = {
        "status": "ok",
        "db": db_ok,
        "escalation_loop": loop_ok,
        "role": role,
        "test_endpoints_enabled": ENABLE_TEST_ENDPOINTS,
        "secret_key_default": secret_key_default,
    }

    if not db_ok or not loop_ok:
        base["status"] = "degraded"
        return JSONResponse(status_code=503, content=base)
    return base


@app.get("/api/cluster")
async def cluster_status():
    """Expose l'etat du cluster Patroni (quorum, membres, roles)."""
    import urllib.request
    import json as _json

    patroni_url = os.getenv("PATRONI_URL", "http://patroni:8008")
    node_name = os.getenv("NODE_NAME", "unknown")
    local_role = "primary" if is_leader.is_set() else "secondary"

    try:
        req = urllib.request.Request(f"{patroni_url}/cluster", method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        cluster = _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"error": f"Patroni unreachable: {e}", "local_node": node_name, "local_role": local_role}
        )

    members = cluster.get("members", [])
    healthy_count = sum(1 for m in members if m.get("state") in ("running", "streaming"))
    total_count = len(members)

    return {
        "local_node": node_name,
        "local_role": local_role,
        "quorum": {
            "total": total_count,
            "healthy": healthy_count,
            "has_quorum": healthy_count > total_count / 2,
        },
        "members": members,
    }
