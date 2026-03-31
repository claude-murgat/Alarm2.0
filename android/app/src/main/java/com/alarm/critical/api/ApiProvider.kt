package com.alarm.critical.api

/**
 * DI holder for ApiService. Production code uses ApiClient.service by default.
 * Tests can swap in a mock via override().
 */
object ApiProvider {
    var service: ApiService = ApiClient.service
        @Synchronized get
        @Synchronized set

    fun override(mock: ApiService) {
        service = mock
    }

    fun reset() {
        service = ApiClient.service
    }
}
