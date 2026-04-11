from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, UniqueConstraint
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
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
