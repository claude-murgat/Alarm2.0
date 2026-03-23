from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class UserCreate(BaseModel):
    email: str
    password: str
    name: str
    is_admin: bool = False


class UserResponse(BaseModel):
    id: int
    email: str
    name: str
    is_admin: bool
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class DeviceRegister(BaseModel):
    device_token: str


class DeviceResponse(BaseModel):
    id: int
    user_id: int
    device_token: str
    last_heartbeat: Optional[datetime]
    is_online: bool

    class Config:
        from_attributes = True


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
    suspended_until: Optional[datetime]
    escalation_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


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
