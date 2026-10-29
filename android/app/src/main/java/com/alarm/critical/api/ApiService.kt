package com.alarm.critical.api

import com.alarm.critical.model.*
import retrofit2.Response
import retrofit2.http.*

interface ApiService {
    @POST("api/auth/login")
    suspend fun login(@Body request: LoginRequest): Response<TokenResponse>

    @POST("api/devices/register")
    suspend fun registerDevice(
        @Header("Authorization") auth: String,
        @Body device: DeviceRegister
    ): Response<Map<String, String>>

    @POST("api/devices/heartbeat")
    suspend fun heartbeat(@Header("Authorization") auth: String): Response<HeartbeatResponse>

    @GET("api/config/escalation")
    suspend fun getEscalationChain(): Response<List<EscalationEntry>>

    @GET("api/alarms/mine")
    suspend fun getMyAlarms(@Header("Authorization") auth: String): Response<List<AlarmResponse>>

    @GET("api/system/modem-status")
    suspend fun getModemStatus(@Header("Authorization") auth: String): Response<ModemStatusResponse>

    @POST("api/alarms/{id}/ack")
    suspend fun acknowledgeAlarm(
        @Header("Authorization") auth: String,
        @Path("id") alarmId: Int
    ): Response<AlarmResponse>

    @GET("api/alarms/")
    suspend fun getAlarmHistory(
        @Header("Authorization") auth: String,
        @Query("days") days: Int = 10
    ): Response<List<AlarmResponse>>

    // INV-079 : refresh via body (refresh_token UUID), sans Bearer header.
    // C'est le seul mode utilisable quand l'access JWT est expire (au-dela
    // de 24h). Backend accepte aussi un Bearer valide en plus pour le mode
    // legacy mais on n'utilise plus que le body cote app.
    @POST("api/auth/refresh")
    suspend fun refreshToken(@Body request: RefreshRequest): Response<RefreshResponse>

    @POST("api/devices/fcm-token")
    suspend fun registerFcmToken(
        @Header("Authorization") auth: String,
        @Body request: FcmTokenRequest
    ): Response<Map<String, String>>

    // ===== INV-ANDROID-011/012 : menu (ecrans Alarmes, Escalade, Sites, admin) =====

    @GET("api/auth/me")
    suspend fun getMe(@Header("Authorization") auth: String): Response<UserResponse>

    @GET("api/alarms/active/escalation")
    suspend fun getActiveEscalation(@Header("Authorization") auth: String): Response<ActiveEscalationResponse>

    @GET("api/users/")
    suspend fun getUsers(@Header("Authorization") auth: String): Response<List<UserResponse>>

    @POST("api/auth/register")
    suspend fun registerUser(
        @Header("Authorization") auth: String,
        @Body request: UserCreateRequest
    ): Response<UserResponse>

    @DELETE("api/users/{id}")
    suspend fun deleteUser(
        @Header("Authorization") auth: String,
        @Path("id") userId: Int
    ): Response<Map<String, String>>

    @PATCH("api/users/{id}")
    suspend fun updateUserPhone(
        @Header("Authorization") auth: String,
        @Path("id") userId: Int,
        @Body request: PhoneUpdate
    ): Response<UserResponse>

    @POST("api/config/escalation/bulk")
    suspend fun saveEscalationChain(
        @Header("Authorization") auth: String,
        @Body request: EscalationBulkRequest
    ): Response<List<EscalationEntry>>

    /** {"minutes": 15.0} */
    @GET("api/config/escalation-delay")
    suspend fun getEscalationDelay(): Response<Map<String, Double>>

    @POST("api/config/escalation-delay")
    suspend fun setEscalationDelay(
        @Header("Authorization") auth: String,
        @Body request: Map<String, Double>
    ): Response<Map<String, Double>>

    /** {"delay_minutes": 2.0} */
    @GET("api/config/sms-call-delay")
    suspend fun getSmsCallDelay(): Response<Map<String, Double>>

    @POST("api/config/sms-call-delay")
    suspend fun setSmsCallDelay(
        @Header("Authorization") auth: String,
        @Body request: Map<String, Double>
    ): Response<Map<String, Double>>

    @GET("api/cluster")
    suspend fun getCluster(): Response<ClusterResponse>

    @GET("api/stats/kpi")
    suspend fun getKpi(
        @Header("Authorization") auth: String,
        @Query("weeks") weeks: Int = 8
    ): Response<KpiResponse>

    @GET("api/audit/")
    suspend fun getAudit(
        @Header("Authorization") auth: String,
        @Query("page_size") pageSize: Int = 50
    ): Response<List<AuditEvent>>

    @HTTP(method = "DELETE", path = "api/devices/fcm-token", hasBody = true)
    suspend fun deleteFcmToken(
        @Header("Authorization") auth: String,
        @Body request: FcmTokenDeleteRequest
    ): Response<Map<String, String>>
}
