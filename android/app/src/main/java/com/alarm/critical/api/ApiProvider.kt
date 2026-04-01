package com.alarm.critical.api

/**
 * DI holder for ApiService. Production code delegates to ApiClient.service by default.
 * Tests can swap in a mock via override().
 *
 * Le getter est calculé (pas stocké) : en production, si ApiClient.currentUrlIndex change
 * suite à un failover, le prochain appel à ApiProvider.service utilise automatiquement
 * la nouvelle URL via ApiClient.service.
 */
object ApiProvider {
    private var _override: ApiService? = null

    val service: ApiService
        @Synchronized get() = _override ?: ApiClient.service

    @Synchronized
    fun override(mock: ApiService) {
        _override = mock
    }

    @Synchronized
    fun reset() {
        _override = null
    }
}
