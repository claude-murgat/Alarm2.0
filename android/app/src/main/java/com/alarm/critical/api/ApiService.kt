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
    ): Response<DeviceResponse>

    @POST("api/devices/heartbeat")
    suspend fun heartbeat(@Header("Authorization") auth: String): Response<HeartbeatResponse>

    @GET("api/alarms/mine")
    suspend fun getMyAlarms(@Header("Authorization") auth: String): Response<List<AlarmResponse>>

    @POST("api/alarms/{id}/ack")
    suspend fun acknowledgeAlarm(
        @Header("Authorization") auth: String,
        @Path("id") alarmId: Int
    ): Response<AlarmResponse>
}
