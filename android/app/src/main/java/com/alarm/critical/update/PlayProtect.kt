package com.alarm.critical.update

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.provider.Settings
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import com.alarm.critical.util.AppLogger

/**
 * INV-ANDROID-707 : « Améliorer la détection des applis malveillantes » peut reduire la fenetre Play Protect des mises
 * a jour integrees, sans garantie (elle revient sur le telephone du proprietaire, 2026-11-12). Boite ouverte seulement
 * par le bouton de l'ecran « Mise à jour ». Aucun intent documente n'ouvre ce reglage : la boite donne le chemin dans
 * le Play Store, puis l'ouvre. Jamais l'ecran interne de Google Play services (VerifyAppsSettingsActivity) : sur un
 * vrai telephone il demarre sans erreur puis charge sans fin. L'app ne peut pas lire ce reglage.
 */
object PlayProtect {
    private const val TAG = "PlayProtect"
    const val PLAY_STORE = "com.android.vending"  // declare dans <queries> du manifest (visibilite, Android 11+)
    const val TITLE = "Réglage Play Protect"
    const val MESSAGE = "Ce réglage peut réduire la fenêtre d'analyse Play Protect lors des mises à jour, sans " +
        "garantie : sur certains téléphones, Android la montre quand même. Il suffit alors d'appuyer sur " +
        "« Installer » ou « Analyser ».\n\n" +
        "1. « Ouvrir le Play Store » ;\n" +
        "2. touchez votre photo de profil (en haut à droite) ;\n" +
        "3. « Play Protect » ;\n" +
        "4. la roue ⚙ en haut à droite ;\n" +
        "5. activez « Améliorer la détection des applis malveillantes »."
    // Courts : depuis Android 12, un toast est coupe apres deux lignes
    const val TOAST = "Activez « Améliorer la détection des applis malveillantes »"
    const val TOAST_NOTHING = "Ouvrez le Play Store à la main"

    /** Lanceur d'une app, null si elle est absente. Remplace dans les tests : les AVD n'ont pas le Play Store. */
    internal var launcher: (Context, String) -> Intent? = { c, pkg -> c.packageManager.getLaunchIntentForPackage(pkg) }
    /** Repli sans Play Store : reglages « Sécurité » d'Android, qui menent a Google Play Protect. Remplace dans les tests. */
    internal var fallback = Intent(Settings.ACTION_SECURITY_SETTINGS)

    /** Boite « Réglage Play Protect » : n'ouvre rien tant que l'utilisateur n'a pas repondu. */
    fun showDialog(activity: Activity) {
        AppLogger.log(TAG, "boîte « $TITLE » affichée")
        AlertDialog.Builder(activity)
            .setTitle(TITLE)
            .setMessage(MESSAGE)
            .setPositiveButton("Ouvrir le Play Store") { _, _ -> openPlayStore(activity) }
            .setNegativeButton("Plus tard") { _, _ -> AppLogger.log(TAG, "boîte « $TITLE » : plus tard") }
            .show()
    }

    /** Play Store, sinon reglages « Sécurité », sinon toast ; jamais de plantage. */
    private fun openPlayStore(activity: Activity) {
        val store = launcher(activity, PLAY_STORE)
        if (store == null) AppLogger.log(TAG, "Play Store absent : repli sur les réglages Sécurité")
        for (intent in listOfNotNull(store, fallback)) {
            val name = if (intent === store) "Play Store" else "écran des réglages Sécurité"
            try {
                activity.startActivity(intent)
            } catch (e: Exception) {  // introuvable (ActivityNotFoundException) ou refuse (SecurityException)
                AppLogger.log(TAG, "$name indisponible (${e.javaClass.simpleName})")
                continue
            }
            AppLogger.log(TAG, "$name ouvert pour le réglage Play Protect")
            Toast.makeText(activity, TOAST, Toast.LENGTH_LONG).show()
            return
        }
        AppLogger.log(TAG, "aucun réglage Play Protect sur ce téléphone : rien n'est ouvert")
        Toast.makeText(activity, TOAST_NOTHING, Toast.LENGTH_LONG).show()
    }
}
