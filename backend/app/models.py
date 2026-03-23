from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    name = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    devices = relationship("Device", back_populates="user")
    alarms = relationship("Alarm", back_populates="assigned_user", foreign_keys="Alarm.assigned_user_id")


class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    device_token = Column(String, unique=True, nullable=False)
    last_heartbeat = Column(DateTime, nullable=True)
    is_online = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="devices")


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
    suspended_until = Column(DateTime, nullable=True)
    escalation_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    assigned_user = relationship("User", back_populates="alarms", foreign_keys=[assigned_user_id])


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
