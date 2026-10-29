package com.alarm.critical.model

data class LoginRequest(val name: String, val password: String)

data class TokenResponse(
    val access_token: String,
    // INV-079 : refresh token persistant (UUID opaque). Renvoyé par /login,
    // null sur les anciens backends pré-INV-079. Le client doit le stocker
    // en SharedPreferences pour pouvoir renouveler l'access au-delà de 24h
    // sans demander le mot de passe.
    val refresh_token: String? = null,
    val token_type: String,
    val user: UserResponse,
    val is_oncall: Boolean = false,
    val escalation_position: Int? = null
)

// INV-079 : body de POST /auth/refresh (mode sans Bearer header)
data class RefreshRequest(val refresh_token: String)

// INV-079 : reponse minimaliste de /auth/refresh (pas user, pas escalation_position)
data class RefreshResponse(
    val access_token: String,
    val token_type: String = "bearer"
)

data class UserResponse(
    val id: Int,
    val email: String,
    val name: String,
    val is_admin: Boolean,
    val is_active: Boolean,
    // INV-151 : compte test -> l'alarme ne sonne pas (INV-ANDROID-008)
    val is_test: Boolean = false,
    // INV-ANDROID-011 : ecran Equipe (GET /api/users/)
    val phone_number: String? = null
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
    val ack_remaining_seconds: Int? = null,
    // INV-155 : "izeron" | "beaufort" ; null pour une alarme d'astreinte (ou un backend d'avant)
    val site: String? = null
) {
    /** INV-ANDROID-009 : "Izeron", "Beaufort" ; « Astreinte » si vide. */
    val siteName: String
        get() = site?.takeIf { it.isNotBlank() }?.replaceFirstChar { it.uppercase() } ?: "Astreinte"
}

/** INV-ANDROID-009 : "Izeron", ou "2 alarmes : Izeron, Beaufort" s'il y en a plusieurs. */
fun List<AlarmResponse>.sitesLabel(): String =
    if (size <= 1) firstOrNull()?.siteName.orEmpty()
    else "$size alarmes : " + joinToString(", ") { it.siteName }

data class DeviceRegister(val device_token: String)

data class FcmTokenRequest(val token: String, val device_id: String)

data class FcmTokenDeleteRequest(val device_id: String)

data class HeartbeatResponse(val status: String, val timestamp: String)

/** INV-ANDROID-606 : une entree de GET /api/config/escalation (public). */
data class EscalationEntry(val position: Int, val user_id: Int)

/** INV-ANDROID-110 : etat du modem gateway (GET /api/system/modem-status, INV-129).
 *  state : "ok" | "no_network" | "unknown" ; bars : 0-4. */
data class ModemStatusResponse(
    val state: String,
    val bars: Int,
    val tech: String?,
    val rsrp: Double?,
    val updated_at: String?,
    // INV-158 : une entree par passerelle (meme forme) ; null sur un backend d'avant
    val gateway_id: String? = null,
    val site: String? = null,
    val gateways: List<ModemStatusResponse>? = null
)

/** INV-ANDROID-012 : GET /health. `version` (sha court) absent sur un ancien backend. */
data class HealthResponse(val version: String? = null)

/** INV-ANDROID-011 : GET /api/alarms/active/escalation (INV-136). `alarm` n'a que
 *  title/site/status/escalation_count/acknowledged_by_name parmi les champs d'AlarmResponse. */
data class ActiveEscalationResponse(
    val alarm: AlarmResponse?,
    val escalation_delay_minutes: Double?,
    val steps: List<EscalationStep>?
)

/** role : "current" (appele en ce moment) | "notified" (deja appele) | "upcoming". */
data class EscalationStep(val position: Int, val user_id: Int, val name: String, val role: String, val acked: Boolean)

/** POST /api/auth/register (admin, INV-149). */
data class UserCreateRequest(
    val name: String,
    val password: String,
    val phone_number: String?,
    val is_admin: Boolean,
    val is_test: Boolean
)

/** PATCH /api/users/{id} : "" efface le numero (le backend le ramene a NULL). */
data class PhoneUpdate(val phone_number: String)

/** POST /api/config/escalation/bulk : la chaine complete, dans l'ordre. */
data class EscalationBulkRequest(val user_ids: List<Int>)

/** GET /api/cluster (INV-161). */
data class ClusterResponse(val local_node: String?, val local_location: String?, val members: List<ClusterMember>?)

data class ClusterMember(val name: String, val role: String?, val state: String?, val location: String?)

/** GET /api/stats/kpi. */
data class KpiResponse(
    val total_alarms: Int,
    val escalation_rate: Double,
    val mttr_minutes: Double,
    val hors_heures_only: Boolean
)

/** GET /api/audit/ (admin). */
data class AuditEvent(val event_type: String, val user_id: Int?, val timestamp: String?, val details: Any?)
