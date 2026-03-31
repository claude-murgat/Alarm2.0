package com.alarm.critical

import androidx.test.espresso.IdlingResource
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * IdlingResource qui attend que le polling ait effectué au moins N appels API.
 *
 * Usage :
 *   val idling = PollingIdlingResource("alarm-poll", targetCalls = 2)
 *   IdlingRegistry.getInstance().register(idling)
 *   // ... Espresso attend automatiquement
 *   IdlingRegistry.getInstance().unregister(idling)
 *
 * Le FakeApiService incrémente le compteur à chaque appel getMyAlarms().
 */
class PollingIdlingResource(
    private val resourceName: String,
    private val targetCalls: Int
) : IdlingResource {

    private val callCount = AtomicInteger(0)
    private val isIdle = AtomicBoolean(false)

    @Volatile
    private var callback: IdlingResource.ResourceCallback? = null

    fun onApiCallComplete() {
        val count = callCount.incrementAndGet()
        if (count >= targetCalls && !isIdle.getAndSet(true)) {
            callback?.onTransitionToIdle()
        }
    }

    fun getCallCount(): Int = callCount.get()

    override fun getName(): String = resourceName

    override fun isIdleNow(): Boolean = callCount.get() >= targetCalls

    override fun registerIdleTransitionCallback(callback: IdlingResource.ResourceCallback?) {
        this.callback = callback
    }
}
