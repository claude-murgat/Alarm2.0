package com.alarm.critical

import android.widget.LinearLayout
import com.alarm.critical.view.SignalBarsView

/** INV-ANDROID-011 : une ligne par passerelle, son site et son signal (GET /api/system/modem-status, INV-158). */
class ModemsActivity : MenuScreenActivity("Sites et modems") {

    override suspend fun load() {
        val status = api { getModemStatus(it) } ?: return
        content.removeAllViews()
        // Backend d'avant INV-158 : pas de liste, un seul modem au premier niveau
        val gateways = status.gateways ?: listOf(status)
        if (gateways.isEmpty()) row("Aucune passerelle n'a rapporté l'état de son modem")
        gateways.forEach { g ->
            val name = listOfNotNull(g.site?.let { place(it) }, g.gateway_id?.let { "($it)" })
                .joinToString(" ").ifEmpty { "Modem" }
            val state = when (g.state) { "ok" -> "OK"; "no_network" -> "Sans réseau"; else -> "Inconnu" }
            val details = listOfNotNull(state, g.tech, g.rsrp?.let { "$it dBm" }, g.updated_at?.let { "maj ${formatDate(it)}" })
            row(name, details.joinToString(" · ")).addView(SignalBarsView(this).apply {
                layoutParams = LinearLayout.LayoutParams(28.dp, 22.dp).apply { marginEnd = 14.dp }
                show(g)
            }, 0)
        }
    }
}
