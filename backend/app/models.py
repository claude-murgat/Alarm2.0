from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    hashed_password = Column(String, nullable=False)
    name = Column(String, unique=True, index=True, nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    last_heartbeat = Column(DateTime, nullable=True)
    is_online = Column(Boolean, default=False)
    phone_number = Column(String, nullable=True)  # For SMS gateway notifications
    created_at = Column(DateTime, default=datetime.utcnow)

    alarms = relationship("Alarm", back_populates="assigned_user", foreign_keys="Alarm.assigned_user_id")


class Alarm(Base):
    __tablename__ = "alarms"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    severity = Column(String, default="critical")  # critical, high, medium, low
    status = Column(String, default="active")  # active, acknowledged, resolved, escalated
    assigned_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    acknowledged_by = Column(Integer, nullable=True)
    acknowledged_by_name = Column(String, nullable=True)
    suspended_until = Column(DateTime, nullable=True)
    notified_user_ids = Column(String, default="")  # Comma-separated IDs of all notified users
    is_oncall_alarm = Column(Boolean, default=False)  # True if auto-generated for on-call disconnection
    escalation_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    # INV-018 : timestamp original immuable (jamais reset par l'escalade ni l'ack expiry).
    # created_at reste le "timer" remis a zero a chaque palier, original_created_at fige t0.
    original_created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # INV-120 V2 : origine de l'alarme. Permet à la reconcile (cf alarms_internal.py)
    # de ne RESOLVE automatiquement QUE les alarmes gateway (les alarmes "api"/"oncall"
    # restent intactes même si le contact se referme).
    source = Column(String, nullable=False, default="api")  # api | oncall | gateway_dry_contact
    # INV-123 : timestamp du premier instant où des gateways alive ont reporté
    # des états divergents. Reset à NULL quand la cohérence est retrouvée.
    sensor_dissensus_since = Column(DateTime, nullable=True)
    # INV-123 anti-spam : un seul email sysadmin par épisode de dissensus.
    sensor_dissensus_email_sent_at = Column(DateTime, nullable=True)

    assigned_user = relationship("User", back_populates="alarms", foreign_keys=[assigned_user_id])
    notifications = relationship("AlarmNotification", back_populates="alarm",
                                 cascade="all, delete-orphan", order_by="AlarmNotification.notified_at")


class AlarmNotification(Base):
    """Table de liaison : quels utilisateurs ont été notifiés pour quelle alarme."""
    __tablename__ = "alarm_notifications"
    __table_args__ = (
        UniqueConstraint("alarm_id", "user_id", name="uq_alarm_user"),
    )
    id = Column(Integer, primary_key=True, index=True)
    alarm_id = Column(Integer, ForeignKey("alarms.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    notified_at = Column(DateTime, default=datetime.utcnow)
    sms_sent = Column(Boolean, default=False)
    call_sent = Column(Boolean, default=False)

    alarm = relationship("Alarm", back_populates="notifications")
    user = relationship("User")


class EscalationConfig(Base):
    __tablename__ = "escalation_config"
    id = Column(Integer, primary_key=True, index=True)
    position = Column(Integer, nullable=False)  # Order in escalation chain
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    delay_minutes = Column(Float, default=15.0)  # Minutes before escalating to next

    user = relationship("User")


class SystemConfig(Base):
    __tablename__ = "system_config"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(String, nullable=False)


class DeviceToken(Base):
    """Tokens FCM enregistres par les apps Android."""
    __tablename__ = "device_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="uq_user_device"),
    )
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    fcm_token = Column(String, nullable=False)
    device_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")


class SmsQueue(Base):
    __tablename__ = "sms_queue"
    id = Column(Integer, primary_key=True, index=True)
    to_number = Column(String, nullable=False)
    body = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)
    error = Column(String, nullable=True)
    retries = Column(Integer, default=0)


class CallQueue(Base):
    __tablename__ = "call_queue"
    id = Column(Integer, primary_key=True, index=True)
    to_number = Column(String, nullable=False)
    alarm_id = Column(Integer, ForeignKey("alarms.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    tts_message = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    called_at = Column(DateTime, nullable=True)
    result = Column(String, nullable=True)  # ack_dtmf|ack_sms|no_answer|busy|error|escalate
    retries = Column(Integer, default=0)

    alarm = relationship("Alarm")
    user = relationship("User")


class AuditEvent(Base):
    """Audit trail : trace de toutes les actions importantes du système."""
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True, index=True)
    alarm_id = Column(Integer, ForeignKey("alarms.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(String, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    details = Column(String, nullable=True)  # JSON string
    correlation_id = Column(String, nullable=True)


class GatewayState(Base):
    """INV-120 V2 : état courant rapporté par chaque gateway on-site.

    Une row par gateway. Upsert à chaque POST /internal/alarms/report-state.
    Source de vérité pour la reconciliation level-based + politique OR
    fail-to-alarm multi-gateway (INV-122) + détection dissensus (INV-123).
    """
    __tablename__ = "gateway_states"
    gateway_id = Column(String, primary_key=True)
    state = Column(String, nullable=False)  # "open" | "closed"
    last_seen = Column(DateTime, nullable=False)


class DeploymentEvent(Base):
    """Trace des actions CD (pull, canary, rollback, promote).

    Cf docs/CD_DESIGN.md §6 (Observabilité du déploiement). Réplication native
    Patroni (table dans la même DB `alarm_db`), cohérent avec INV-100 audit_events.

    Inséré par :
    - L'orchestrateur canary sur NODE3 (cf PR 5/6) via POST /api/deployments/events
      avec X-Gateway-Key.
    - Le workflow promote-stable (cf PR 7) après re-tag GHCR.

    Lu par :
    - Le dashboard admin (GET /api/deployments/events).
    - L'orchestrateur lui-même pour décider du rollback (retrouve `:stable-prev`
      via l'historique des digests).
    """
    __tablename__ = "deployment_events"
    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    node = Column(String, nullable=False, index=True)
    image = Column(String, nullable=False)
    kind = Column(String, nullable=False, index=True)
    # kind ∈ {'pull', 'canary_start', 'canary_promoted', 'rollback', 'abort',
    #         'manual_override', 'emergency_promote', 'emergency_aborted_network'}
    from_digest = Column(String, nullable=True)  # sha256:... avant
    to_digest = Column(String, nullable=True)    # sha256:... après
    status = Column(String, nullable=False)      # 'success' | 'failure' | 'in_progress'
    actor = Column(String, nullable=True)
    details = Column(String, nullable=True)      # JSON string
