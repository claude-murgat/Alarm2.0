package com.alarm.critical

import android.content.res.ColorStateList
import android.graphics.Color
import android.view.View
import android.widget.ProgressBar
import com.alarm.critical.update.AppUpdate
import com.alarm.critical.update.UpdateProvider
import com.alarm.critical.update.UpdateState
import com.alarm.critical.update.showDownload
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * INV-ANDROID-706 : versions installee et annoncee par le serveur, etat en une phrase, derniere verification,
 * « Vérifier maintenant » et « Installer ». Meme etat que le bandeau du tableau de bord (AppUpdate.state).
 */
class UpdateActivity : MenuScreenActivity("Mises à jour") {

    private fun line(id: Int, sizeSp: Float, color: String = "#FFFFFF", bold: Boolean = false) =
        label("", sizeSp, color, bold).apply {
            this.id = id
            setPadding(0, 4.dp, 0, 4.dp)
            content.addView(this)
        }

    override suspend fun load() {
        content.removeAllViews()
        section("Versions")
        line(R.id.updateInstalled, 15f).text =
            "Version installée : ${UpdateProvider.installedVersion ?: BuildConfig.VERSION_NAME}"  // null : build debug
        val server = line(R.id.updateServer, 15f)
        section("État")
        val state = line(R.id.updateState, 16f, bold = true)
        val progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            id = R.id.updateProgress
            max = 100
            progressTintList = ColorStateList.valueOf(Color.parseColor("#00A2E5"))  // comme le bandeau
            contentDescription = "Progression du téléchargement de la mise à jour"
            content.addView(this)
        }
        val lastCheck = line(R.id.updateLastCheck, 12f, "#94a3b8")
        val check = button("Vérifier maintenant") { AppUpdate.check(this) }.apply {
            id = R.id.updateCheckButton
            contentDescription = "Vérifier maintenant s'il existe une mise à jour"
        }
        val install = button("Installer") { AppUpdate.install(this) }.apply {
            id = R.id.updateInstallButton
            contentDescription = "Installer la mise à jour"
        }

        AppUpdate.check(this)
        AppUpdate.state.collect { s ->
            server.text = "Dernière version annoncée par le serveur : ${s.server ?: "inconnue"}"
            state.text = s.sentence
            progress.showDownload(s)
            lastCheck.text = "Dernière vérification : " +
                (s.lastCheck?.let { SimpleDateFormat("dd/MM HH:mm", Locale.getDefault()).format(Date(it)) } ?: "jamais")
            check.isEnabled = !s.checking
            install.visibility = if (s.phase == UpdateState.Phase.READY) View.VISIBLE else View.GONE
        }
    }
}
