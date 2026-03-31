from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Optional
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


class AlarmCreate(BaseModel):
    title: str
    message: str
    severity: str = "critical"
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
        """Build AlarmResponse with parsed notified_user_ids and resolved names."""
        from .clock import now as clock_now
        raw_ids = alarm.notified_user_ids or ""
        parsed_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
        names = []
        if db and parsed_ids:
            from .models import User
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
