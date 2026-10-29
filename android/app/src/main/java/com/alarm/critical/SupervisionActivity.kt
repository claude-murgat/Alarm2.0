package com.alarm.critical

/** INV-ANDROID-011 (admin) : noeuds du cluster (GET /api/cluster, INV-161), KPI sur 8 semaines
 *  (GET /api/stats/kpi) et derniers evenements d'audit (GET /api/audit/, admin). */
class SupervisionActivity : MenuScreenActivity("Supervision") {

    override suspend fun load() {
        val cluster = api { getCluster() }  // 503 si Patroni injoignable : on affiche le reste
        val kpi = api { getKpi(it) }
        val audit = api { getAudit(it) }
        if (isFinishing) return  // 403
        content.removeAllViews()

        section("Cluster" + (cluster?.local_node?.let { " — réponse de $it" } ?: ""))
        if (cluster == null) row("État du cluster indisponible")
        cluster?.members.orEmpty().forEach { m ->
            val role = when (m.role) { "leader" -> "primaire"; "replica", "sync_standby" -> "réplique"; else -> m.role ?: "?" }
            row("${m.name} · ${place(m.location)}", "$role · ${m.state ?: "?"}")
        }

        section("Statistiques sur 8 semaines" + if (kpi?.hors_heures_only == true) " (hors heures ouvrées)" else "")
        if (kpi == null) row("Statistiques indisponibles")
        else row("${kpi.total_alarms} alarmes",
            "Escalade : ${kpi.escalation_rate.fmt()} % · Temps moyen de résolution : ${kpi.mttr_minutes.fmt()} min")

        section("Derniers événements")
        if (audit.isNullOrEmpty()) row("Aucun événement")
        audit.orEmpty().forEach { e ->
            row(e.event_type, listOfNotNull(formatDate(e.timestamp), e.details?.toString()).joinToString(" · "))
        }
    }
}
