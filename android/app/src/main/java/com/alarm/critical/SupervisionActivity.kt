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

        section("Cluster" + (cluster?.local_node?.let { " — réponse de ${nodeLabel(it, cluster.local_machine)}" } ?: ""))
        if (cluster == null) row("État du cluster indisponible")
        // Un seul cluster, en groupes : `group` du backend, sinon Legacy si `retiring`, sinon le lieu
        val legacy = "Legacy (Izeron)"
        val order = listOf("Izeron", "Beaufort", "OVH")  // puis tout autre groupe, Legacy en dernier
        cluster?.members.orEmpty()
            .groupBy { m -> m.group?.takeIf { it.isNotBlank() } ?: if (m.retiring == true) legacy else place(m.location) }
            .entries.sortedBy { (g, _) -> if (g == legacy) order.size + 1 else order.indexOf(g).takeIf { it >= 0 } ?: order.size }
            .forEach { (g, members) ->
                content.addView(label(g, 13f, "#cbd5e1", bold = true).apply { setPadding(0, 4.dp, 0, 6.dp) })
                members.forEach { m ->
                    val role = when (m.role) { "leader" -> "primaire"; "replica", "sync_standby" -> "réplique"; else -> m.role ?: "?" }
                    row("${nodeLabel(m.name, m.machine)} · ${place(m.location)}" + if (m.retiring == true) " · bientôt obsolète" else "",
                        "$role · ${m.state ?: "?"}")
                }
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
