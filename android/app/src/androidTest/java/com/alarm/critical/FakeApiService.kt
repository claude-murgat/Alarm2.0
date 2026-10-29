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
    var myAlarmsCallCount = 0
        private set

    var acknowledgeResponse: Response<AlarmResponse> = Response.success(
        AlarmResponse(42, "Test", "msg", "critical", "acknowledged", 1, "2026-01-01T00:01:00", "user1", "2026-01-01T00:31:00", 0, "2026-01-01T00:00:00", ack_remaining_seconds = 1800)
    )

    var alarmHistoryResponse: Response<List<AlarmResponse>> = Response.success(emptyList())

    // INV-ANDROID-606 : chaine d'escalade (defaut : user1 = id 1 en position 1)
    var escalationChainResponse: Response<List<EscalationEntry>> = Response.success(
        listOf(EscalationEntry(position = 1, user_id = 1))
    )

    // INV-ANDROID-110 : etat du modem gateway (defaut : inconnu)
    var modemStatusResponse: Response<ModemStatusResponse> = Response.success(
        ModemStatusResponse("unknown", 0, null, null, null)
    )

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

    /** Si vrai, registerDevice() lance une RuntimeException — sert a tester
     *  le branch catch (e: Exception) de MainActivity.login() (cf INV-ANDROID-108). */
    var registerDeviceShouldThrow: Boolean = false

    override suspend fun registerDevice(auth: String, device: DeviceRegister): Response<Map<String, String>> {
        if (registerDeviceShouldThrow) {
            throw RuntimeException("simulated registerDevice failure")
        }
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

    override suspend fun getModemStatus(auth: String): Response<ModemStatusResponse> = modemStatusResponse

    override suspend fun getEscalationChain(): Response<List<EscalationEntry>> = escalationChainResponse

    // INV-ANDROID-009 : une reponse par alarme (multi-site), sinon acknowledgeResponse
    var acknowledgeResponses: Map<Int, Response<AlarmResponse>> = emptyMap()
    val acknowledgedIds = mutableListOf<Int>()

    override suspend fun acknowledgeAlarm(auth: String, alarmId: Int): Response<AlarmResponse> {
        acknowledgeCalledWith = Pair(auth, alarmId)
        acknowledgedIds += alarmId
        return acknowledgeResponses[alarmId] ?: acknowledgeResponse
    }

    // `days` ajoute a ApiService (GET /api/alarms/?days=N). Le fake rend
    // `alarmHistoryResponse` tel quel, le filtrage est teste cote backend.
    var lastHistoryDays: Int? = null

    override suspend fun getAlarmHistory(auth: String, days: Int): Response<List<AlarmResponse>> {
        lastHistoryDays = days
        return alarmHistoryResponse
    }

    // INV-082 : signature mise a jour pour matcher ApiService.refreshToken
    // qui prend maintenant un RefreshRequest body au lieu d'un Authorization
    // header. Defaut : un access token "fake-refreshed-token" pour distinguer
    // d'un login frais dans les tests.
    var refreshTokenResponse: Response<RefreshResponse> = Response.success(
        RefreshResponse(access_token = "fake-refreshed-token", token_type = "bearer")
    )
    var lastRefreshRequestPayload: RefreshRequest? = null

    override suspend fun refreshToken(request: RefreshRequest): Response<RefreshResponse> {
        lastRefreshRequestPayload = request
        return refreshTokenResponse
    }

    // FCM token management
    var fcmTokenRegistered: FcmTokenRequest? = null
    var fcmTokenDeleted: FcmTokenDeleteRequest? = null

    override suspend fun registerFcmToken(auth: String, request: FcmTokenRequest): Response<Map<String, String>> {
        fcmTokenRegistered = request
        return Response.success(mapOf("status" to "ok"))
    }

    /** INV-ANDROID-503 : latence reseau simulee (un vrai appel n'est jamais instantane). */
    var deleteFcmDelayMs = 0L

    override suspend fun deleteFcmToken(auth: String, request: FcmTokenDeleteRequest): Response<Map<String, String>> {
        kotlinx.coroutines.delay(deleteFcmDelayMs)
        fcmTokenDeleted = request
        return Response.success(mapOf("status" to "ok"))
    }

    // ===== INV-ANDROID-010/011/012 : menu et ecrans admin =====
    // Donnees : 2 sites (Izeron, Beaufort), 3 utilisateurs, 5 noeuds.


    var meResponse: Response<UserResponse> = Response.success(
        UserResponse(1, "user1@alarm.local", "user1", false, true)
    )
    override suspend fun getMe(auth: String): Response<UserResponse> = meResponse

    var activeEscalationResponse: Response<ActiveEscalationResponse> = Response.success(
        ActiveEscalationResponse(
            alarm = AlarmResponse(7, "Déclenchement contact sec — Beaufort", "", "critical", "escalated", 2,
                null, null, null, 1, "2026-10-29T08:00:00", site = "beaufort"),
            escalation_delay_minutes = 15.0,
            steps = listOf(
                EscalationStep(1, 1, "user1", "notified", false),
                EscalationStep(2, 2, "user2", "current", false),
                EscalationStep(3, 3, "admin", "upcoming", false),
            )
        )
    )
    override suspend fun getActiveEscalation(auth: String): Response<ActiveEscalationResponse> = activeEscalationResponse

    var usersResponse: Response<List<UserResponse>> = Response.success(listOf(
        UserResponse(1, "", "user1", false, true, phone_number = "+33611111111"),
        UserResponse(2, "", "user2", false, true, phone_number = "+33622222222"),
        UserResponse(3, "", "admin", true, true),
    ))
    override suspend fun getUsers(auth: String): Response<List<UserResponse>> = usersResponse

    var registeredUser: UserCreateRequest? = null
    override suspend fun registerUser(auth: String, request: UserCreateRequest): Response<UserResponse> {
        registeredUser = request
        return Response.success(UserResponse(4, "", request.name, request.is_admin, true, request.is_test, request.phone_number))
    }

    var deletedUserId: Int? = null
    override suspend fun deleteUser(auth: String, userId: Int): Response<Map<String, String>> {
        deletedUserId = userId
        return Response.success(mapOf("status" to "deleted"))
    }

    var phoneUpdate: Pair<Int, PhoneUpdate>? = null
    override suspend fun updateUserPhone(auth: String, userId: Int, request: PhoneUpdate): Response<UserResponse> {
        phoneUpdate = userId to request
        return Response.success(UserResponse(userId, "", "user$userId", false, true, phone_number = request.phone_number))
    }

    var savedChain: List<Int>? = null
    override suspend fun saveEscalationChain(auth: String, request: EscalationBulkRequest): Response<List<EscalationEntry>> {
        savedChain = request.user_ids
        return Response.success(request.user_ids.mapIndexed { i, uid -> EscalationEntry(i + 1, uid) })
    }

    var escalationDelay = 15.0
    override suspend fun getEscalationDelay(): Response<Map<String, Double>> = Response.success(mapOf("minutes" to escalationDelay))
    override suspend fun setEscalationDelay(auth: String, request: Map<String, Double>): Response<Map<String, Double>> {
        escalationDelay = request.getValue("minutes")
        return Response.success(request)
    }

    var smsCallDelay = 2.0
    override suspend fun getSmsCallDelay(): Response<Map<String, Double>> = Response.success(mapOf("delay_minutes" to smsCallDelay))
    override suspend fun setSmsCallDelay(auth: String, request: Map<String, Double>): Response<Map<String, Double>> {
        smsCallDelay = request.getValue("delay_minutes")
        return Response.success(request)
    }

    var clusterResponse: Response<ClusterResponse> = Response.success(ClusterResponse(
        local_node = "node1", local_location = "izeron",
        members = listOf(
            ClusterMember("node1", "leader", "running", "izeron"),
            ClusterMember("node2", "replica", "streaming", "izeron"),
            ClusterMember("node3", "replica", "streaming", "ovh"),
            ClusterMember("node4", "replica", "streaming", "beaufort"),
            ClusterMember("node5", "sync_standby", "streaming", "beaufort"),
        )
    ))
    override suspend fun getCluster(): Response<ClusterResponse> = clusterResponse

    var kpiResponse: Response<KpiResponse> = Response.success(KpiResponse(12, 25.0, 18.5, true))
    override suspend fun getKpi(auth: String, weeks: Int): Response<KpiResponse> = kpiResponse

    var auditResponse: Response<List<AuditEvent>> = Response.success(listOf(
        AuditEvent("config_changed", 3, "2026-10-29T07:59:00.123456", mapOf("key" to "escalation_chain")),
        AuditEvent("user_login", 1, "2026-10-29T07:58:00", mapOf("name" to "user1")),
    ))
    override suspend fun getAudit(auth: String, pageSize: Int): Response<List<AuditEvent>> = auditResponse

    /** Reset le compteur d'appels pour réutiliser avec un nouvel IdlingResource */
    fun resetCallCount() {
        myAlarmsCallCount = 0
    }
}
