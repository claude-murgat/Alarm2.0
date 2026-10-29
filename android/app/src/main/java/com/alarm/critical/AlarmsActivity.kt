package com.alarm.critical

import com.alarm.critical.model.AlarmResponse

/** INV-ANDROID-011 : historique de tous les sites, filtre par site et par periode (GET /api/alarms/?days=N). */
class AlarmsActivity : MenuScreenActivity("Alarmes") {
    private val periods = listOf(10, 30, 90)
    private var days = 10
    private var site: String? = null  // null = tous les sites
    private var alarms: List<AlarmResponse> = emptyList()

    override suspend fun load() {
        api { getAlarmHistory(it, days) }?.let { alarms = it }
        render()
    }

    private fun render() {
        content.removeAllViews()
        choices(periods.map { "$it jours" }, periods.indexOf(days)) {
            days = periods[it]
            reload()
        }
        val sites = alarms.map { it.siteName }.distinct().sorted()
        if (site !in sites) site = null
        choices(listOf("Tous les sites") + sites, sites.indexOf(site) + 1) {
            site = sites.getOrNull(it - 1)
            render()
        }
        val shown = alarms.filter { site == null || it.siteName == site }
        if (shown.isEmpty()) row("Aucune alarme sur ces $days jours")
        shown.forEach { a ->
            val status = when (a.status) {
                "resolved" -> "résolue"
                "acknowledged" -> "acquittée par ${a.acknowledged_by_name ?: "?"}"
                else -> "en cours"
            }
            row("${a.siteName} · ${a.title}", "${formatDate(a.created_at)} — $status")
        }
    }
}
