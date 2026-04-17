from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Literal, Optional
import re


class UserCreate(BaseModel):
    name: str
    password: str
    is_admin: bool = False

    @field_validator("name")
    @classmethod
    def name_no_spaces(cls, v: str) -> str:
        if " " in v:
            raise ValueError("Le nom ne doit pas contenir d'espaces")
        return v.lower()


class UserResponse(BaseModel):
    id: int
    name: str
    is_admin: bool
    is_active: bool
    is_online: bool = False
    last_heartbeat: Optional[datetime] = None
    phone_number: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    name: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
    is_oncall: bool = False
    escalation_position: Optional[int] = None


class FcmTokenRequest(BaseModel):
    token: str
    device_id: str


class FcmTokenDeleteRequest(BaseModel):
    device_id: str


class AlarmCreate(BaseModel):
    title: str = Field(..., max_length=200)
    message: str = Field(..., max_length=2000)
    severity: Literal["low", "medium", "high", "critical"] = "critical"
    assigned_user_id: Optional[int] = None


class AlarmResponse(BaseModel):
    id: int
    title: str
    message: str
    severity: str
    status: str
    assigned_user_id: Optional[int]
    acknowledged_at: Optional[datetime]
    acknowledged_by: Optional[int]
    acknowledged_by_name: Optional[str] = None
    suspended_until: Optional[datetime]
    ack_remaining_seconds: Optional[int] = None
    notified_user_ids: list[int] = []
    notified_user_names: list[str] = []
    is_oncall_alarm: bool = False
    escalation_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_alarm(cls, alarm, db=None):
        """Build AlarmResponse with notified_user_ids/names from alarm_notifications table."""
        from .clock import now as clock_now

        # Lire les IDs notifiés depuis la table de liaison
        parsed_ids = []
        names = []
        if db:
            from .models import AlarmNotification, User
            notifs = (
                db.query(AlarmNotification)
                .filter(AlarmNotification.alarm_id == alarm.id)
                .order_by(AlarmNotification.notified_at)
                .all()
            )
            parsed_ids = [n.user_id for n in notifs]
            if parsed_ids:
                users = db.query(User).filter(User.id.in_(parsed_ids)).all()
                id_to_name = {u.id: u.name for u in users}
                names = [id_to_name.get(uid, "?") for uid in parsed_ids]

        ack_remaining = None
        if alarm.suspended_until and alarm.status == "acknowledged":
            remaining = (alarm.suspended_until - clock_now()).total_seconds()
            ack_remaining = max(0, int(remaining))
        return cls(
            id=alarm.id, title=alarm.title, message=alarm.message,
            severity=alarm.severity, status=alarm.status,
            assigned_user_id=alarm.assigned_user_id,
            acknowledged_at=alarm.acknowledged_at,
            acknowledged_by=alarm.acknowledged_by,
            acknowledged_by_name=alarm.acknowledged_by_name,
            suspended_until=alarm.suspended_until,
            ack_remaining_seconds=ack_remaining,
            notified_user_ids=parsed_ids,
            notified_user_names=names,
            is_oncall_alarm=alarm.is_oncall_alarm or False,
            escalation_count=alarm.escalation_count,
            created_at=alarm.created_at,
            updated_at=alarm.updated_at,
        )


class EscalationConfigCreate(BaseModel):
    position: int
    user_id: int
    delay_minutes: float = 15.0


class EscalationConfigResponse(BaseModel):
    id: int
    position: int
    user_id: int
    delay_minutes: float

    class Config:
        from_attributes = True


class SystemConfigUpdate(BaseModel):
    key: str
    value: str


class SmsQueueItem(BaseModel):
    id: int
    to_number: str
    body: str
    retries: int
    created_at: datetime
    sent_at: Optional[datetime] = None
    error: Optional[str] = None

    class Config:
        from_attributes = True
