package com.alarm.critical.model

data class LoginRequest(val email: String, val password: String)

data class TokenResponse(
    val access_token: String,
    val token_type: String,
    val user: UserResponse
)

data class UserResponse(
    val id: Int,
    val email: String,
    val name: String,
    val is_admin: Boolean,
    val is_active: Boolean
)

data class AlarmResponse(
    val id: Int,
    val title: String,
    val message: String,
    val severity: String,
    val status: String,
    val assigned_user_id: Int?,
    val acknowledged_at: String?,
    val suspended_until: String?,
    val escalation_count: Int,
    val created_at: String
)

data class DeviceRegister(val device_token: String)

data class DeviceResponse(
    val id: Int,
    val user_id: Int,
    val device_token: String,
    val is_online: Boolean
)

data class HeartbeatResponse(val status: String, val timestamp: String)
