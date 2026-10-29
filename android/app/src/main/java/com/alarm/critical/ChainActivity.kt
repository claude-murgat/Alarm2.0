package com.alarm.critical

import android.widget.EditText
import android.widget.LinearLayout
import androidx.lifecycle.lifecycleScope
import com.alarm.critical.api.ApiService
import com.alarm.critical.model.EscalationBulkRequest
import kotlinx.coroutines.launch
import retrofit2.Response
import java.util.Collections

/** INV-ANDROID-011 (admin) : ordre de la chaine (▲/▼ puis POST /api/config/escalation/bulk)
 *  et delais d'escalade / avant SMS et appel. */
class ChainActivity : MenuScreenActivity("Chaîne d'escalade") {
    private var chain = mutableListOf<Int>()  // user_ids, position 1 en tete
    private var names = mapOf<Int, String>()
    private lateinit var chainRows: LinearLayout

    override suspend fun load() {
        val entries = api { getEscalationChain() } ?: return
        names = api { getUsers(it) }?.associate { it.id to it.name } ?: return
        val delay = api { getEscalationDelay() }?.get("minutes")
        val smsDelay = api { getSmsCallDelay() }?.get("delay_minutes")
        chain = entries.sortedBy { it.position }.map { it.user_id }.toMutableList()

        content.removeAllViews()
        section("Ordre d'appel")
        chainRows = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        content.addView(chainRows)
        renderChain()
        button("Enregistrer l'ordre") {
            lifecycleScope.launch {
                api { saveEscalationChain(it, EscalationBulkRequest(chain.toList())) } ?: return@launch
                toast("Chaîne enregistrée, chacun est prévenu par notification")
            }
        }

        section("Délais")
        val delayInput = numberField("Délai avant de passer au suivant (min, 1 à 60)", delay)
        button("Enregistrer le délai d'escalade") {
            saveMinutes(delayInput, "minutes") { auth, body -> setEscalationDelay(auth, body) }
        }
        val smsInput = numberField("Délai avant SMS et appel (min, 1 à 30)", smsDelay)
        button("Enregistrer le délai SMS et appel") {
            saveMinutes(smsInput, "delay_minutes") { auth, body -> setSmsCallDelay(auth, body) }
        }
    }

    private fun renderChain() {
        chainRows.removeAllViews()
        if (chain.isEmpty()) row("Chaîne vide", into = chainRows)
        chain.forEachIndexed { i, uid ->
            val name = names[uid] ?: "#$uid"
            val moves = listOfNotNull(
                action("▲", "Monter $name") { move(i, -1) }.takeIf { i > 0 },
                action("▼", "Descendre $name") { move(i, +1) }.takeIf { i < chain.lastIndex },
            )
            row("${i + 1}. $name", if (i == 0) "De garde" else null, moves, chainRows)
        }
    }

    private fun move(i: Int, delta: Int) {
        Collections.swap(chain, i, i + delta)
        renderChain()
    }

    private fun saveMinutes(
        input: EditText,
        key: String,
        post: suspend ApiService.(String, Map<String, Double>) -> Response<Map<String, Double>>,
    ) {
        val minutes = input.text.toString().replace(',', '.').toDoubleOrNull()
        if (minutes == null) {
            toast("Saisir un nombre de minutes")
            return
        }
        // Bornes verifiees par le serveur (422 -> toast avec son message)
        lifecycleScope.launch {
            api { post(it, mapOf(key to minutes)) } ?: return@launch
            toast("Délai enregistré : ${minutes.fmt()} min")
        }
    }
}
