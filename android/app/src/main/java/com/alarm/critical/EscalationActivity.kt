package com.alarm.critical

/** INV-ANDROID-011 : alarme en cours, son palier et la chaine (GET /api/alarms/active/escalation). */
class EscalationActivity : MenuScreenActivity("Escalade") {

    override suspend fun load() {
        val esc = api { getActiveEscalation(it) } ?: return
        val steps = esc.steps.orEmpty()
        content.removeAllViews()

        section("Alarme en cours")
        val alarm = esc.alarm
        if (alarm == null) {
            row("Aucune alarme en cours")
        } else {
            val palier = steps.firstOrNull { it.role == "current" }?.position ?: (alarm.escalation_count + 1)
            val state = if (alarm.status == "acknowledged") "acquittée par ${alarm.acknowledged_by_name ?: "?"}"
                        else "en attente d'acquittement"
            row("${alarm.siteName} · ${alarm.title}", "Palier $palier · $state")
        }

        section("Chaîne d'escalade" + (esc.escalation_delay_minutes?.let { " (${it.fmt()} min par palier)" } ?: ""))
        if (steps.isEmpty()) row("Chaîne vide")
        steps.forEach { s ->
            val status = listOfNotNull(
                "De garde".takeIf { s.position == 1 },
                when (s.role) { "current" -> "appelé en ce moment"; "notified" -> "déjà appelé"; else -> null },
                "a acquitté".takeIf { s.acked },
            ).joinToString(" · ").ifEmpty { "En renfort" }
            row("${s.position}. ${s.name}", status)
        }
    }
}
