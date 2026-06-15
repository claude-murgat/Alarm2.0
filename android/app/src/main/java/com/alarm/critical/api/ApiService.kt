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

    @GET("api/alarms/mine")
    suspend fun getMyAlarms(@Header("Authorization") auth: String): Response<List<AlarmResponse>>

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

    // INV-082 : refresh via body (refresh_token UUID), sans Bearer header.
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

    @HTTP(method = "DELETE", path = "api/devices/fcm-token", hasBody = true)
    suspend fun deleteFcmToken(
        @Header("Authorization") auth: String,
        @Body request: FcmTokenDeleteRequest
    ): Response<Map<String, String>>
}
