package com.alarm.critical

import com.alarm.critical.api.ApiService
import com.alarm.critical.model.*
import retrofit2.Response

/**
 * Fake ApiService pour tests Espresso isolés. Aucun appel réseau.
 * Configurer les réponses via les propriétés publiques avant chaque test.
 * Notifie les IdlingResources à chaque appel pour synchroniser Espresso.
 */
class FakeApiService : ApiService {

    var loginResponse: Response<TokenResponse> = Response.success(
        TokenResponse(
            access_token = "fake-token",
            token_type = "bearer",
            user = UserResponse(1, "user1@alarm.local", "user1", false, true)
        )
    )

    var heartbeatResponse: Response<HeartbeatResponse> = Response.success(
        HeartbeatResponse("ok", "2026-01-01T00:00:00")
    )

    var myAlarmsResponses: MutableList<Response<List<AlarmResponse>>> = mutableListOf(
        Response.success(emptyList())
    )
    private var myAlarmsCallCount = 0

    var acknowledgeResponse: Response<AlarmResponse> = Response.success(
        AlarmResponse(42, "Test", "msg", "critical", "acknowledged", 1, "2026-01-01T00:01:00", "user1", "2026-01-01T00:31:00", 0, "2026-01-01T00:00:00", ack_remaining_seconds = 1800)
    )

    var alarmHistoryResponse: Response<List<AlarmResponse>> = Response.success(emptyList())

    // Suivi des appels pour vérification
    var acknowledgeCalledWith: Pair<String, Int>? = null
    var loginCalled = false
    var heartbeatCallCount = 0

    // IdlingResource : notifié à chaque appel polling/heartbeat
    var pollingIdlingResource: PollingIdlingResource? = null
    var heartbeatIdlingResource: PollingIdlingResource? = null

    override suspend fun login(request: LoginRequest): Response<TokenResponse> {
        loginCalled = true
        return loginResponse
    }

    override suspend fun registerDevice(auth: String, device: DeviceRegister): Response<Map<String, String>> {
        return Response.success(mapOf("status" to "ok"))
    }

    override suspend fun heartbeat(auth: String): Response<HeartbeatResponse> {
        heartbeatCallCount++
        val resp = heartbeatResponse
        heartbeatIdlingResource?.onApiCallComplete()
        return resp
    }

    override suspend fun getMyAlarms(auth: String): Response<List<AlarmResponse>> {
        val index = myAlarmsCallCount.coerceAtMost(myAlarmsResponses.size - 1)
        myAlarmsCallCount++
        val resp = myAlarmsResponses[index]
        pollingIdlingResource?.onApiCallComplete()
        return resp
    }

    override suspend fun acknowledgeAlarm(auth: String, alarmId: Int): Response<AlarmResponse> {
        acknowledgeCalledWith = Pair(auth, alarmId)
        return acknowledgeResponse
    }

    override suspend fun getAlarmHistory(auth: String): Response<List<AlarmResponse>> {
        return alarmHistoryResponse
    }

    var refreshTokenResponse: Response<TokenResponse>? = null  // null = use loginResponse

    override suspend fun refreshToken(auth: String): Response<TokenResponse> {
        return refreshTokenResponse ?: loginResponse
    }

    // FCM token management
    var fcmTokenRegistered: FcmTokenRequest? = null
    var fcmTokenDeleted: FcmTokenDeleteRequest? = null

    override suspend fun registerFcmToken(auth: String, request: FcmTokenRequest): Response<Map<String, String>> {
        fcmTokenRegistered = request
        return Response.success(mapOf("status" to "ok"))
    }

    override suspend fun deleteFcmToken(auth: String, request: FcmTokenDeleteRequest): Response<Map<String, String>> {
        fcmTokenDeleted = request
        return Response.success(mapOf("status" to "ok"))
    }

    /** Reset le compteur d'appels pour réutiliser avec un nouvel IdlingResource */
    fun resetCallCount() {
        myAlarmsCallCount = 0
    }
}
