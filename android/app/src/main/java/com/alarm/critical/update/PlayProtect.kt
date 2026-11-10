package com.alarm.critical.update

import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.provider.Settings
import android.widget.Toast
import com.alarm.critical.util.AppLogger

/**
 * INV-ANDROID-707 : ouvre le reglage Play Protect, ou « Améliorer la détection des applis malveillantes »
 * supprime la fenetre Play Protect des mises a jour integrees. L'app ne peut pas lire ce reglage.
 */
object PlayProtect {
    private const val TAG = "PlayProtect"
    const val TOAST = "Activez « Améliorer la détection des applis malveillantes » : " +
        "les mises à jour s'installeront sans fenêtre Play Protect"

    /** Essayes dans l'ordre. Pas de page Play Protect du Play Store : aucun intent documente. Remplaces dans les tests. */
    internal var targets = listOf(
        Intent().setComponent(ComponentName("com.google.android.gms",
            "com.google.android.gms.security.settings.VerifyAppsSettingsActivity")),
        Intent(Settings.ACTION_SECURITY_SETTINGS),
    )

    /** Lanceur d'une app, null si elle est absente. Remplace dans les tests : les AVD n'ont pas le Play Store. */
    internal var launcher: (android.content.Context, String) -> Intent? =
        { c, pkg -> c.packageManager.getLaunchIntentForPackage(pkg) }
    /** Repli sans Play Store. Remplace dans les tests. */
    internal var fallback = Intent(Settings.ACTION_SECURITY_SETTINGS)

    /** Ouvre le premier reglage disponible avec le toast ; false si aucun ne s'ouvre. */
    fun open(activity: Activity): Boolean {
        for (intent in targets) {
            val name = intent.component?.className ?: intent.action
            try {
                activity.startActivity(intent)
            } catch (e: Exception) {  // introuvable (ActivityNotFoundException) ou non exporte (SecurityException)
                AppLogger.log(TAG, "réglage Play Protect $name indisponible (${e.javaClass.simpleName})")
                continue
            }
            AppLogger.log(TAG, "réglage Play Protect ouvert : $name")
            Toast.makeText(activity, TOAST, Toast.LENGTH_LONG).show()
            return true
        }
        AppLogger.log(TAG, "aucun réglage Play Protect sur ce téléphone : rien n'est ouvert")
        return false
    }
}
