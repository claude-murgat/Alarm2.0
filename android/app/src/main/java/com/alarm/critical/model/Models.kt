package com.alarm.critical.model

data class LoginRequest(val name: String, val password: String)

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
    val acknowledged_by_name: String?,
    val suspended_until: String?,
    val escalation_count: Int,
    val created_at: String,
    val ack_remaining_seconds: Int? = null
)

data class DeviceRegister(val device_token: String)

data class HeartbeatResponse(val status: String, val timestamp: String)
