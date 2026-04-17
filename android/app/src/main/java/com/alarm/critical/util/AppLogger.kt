package com.alarm.critical.util

import android.os.Build
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.ConcurrentLinkedDeque

/**
 * Logger applicatif qui stocke les N derniers evenements en memoire.
 * Permet a l'utilisateur d'exporter les logs pour debug via messagerie.
 */
object AppLogger {
    private const val MAX_ENTRIES = 500
    private val entries = ConcurrentLinkedDeque<String>()
    private val sdf = SimpleDateFormat("dd/MM HH:mm:ss", Locale.getDefault())

    fun log(tag: String, message: String) {
        val ts = sdf.format(Date())
        val entry = "[$ts] $tag: $message"
        entries.addLast(entry)
        while (entries.size > MAX_ENTRIES) {
            entries.pollFirst()
        }
        android.util.Log.d("AppLogger", entry)
    }

    fun exportLogs(userName: String, appVersion: String): String {
        val header = buildString {
            appendLine("=== Alarme Murgat - Logs de diagnostic ===")
            appendLine("Utilisateur: $userName")
            appendLine("Version: $appVersion")
            appendLine("Appareil: ${Build.MANUFACTURER} ${Build.MODEL}")
            appendLine("Android: ${Build.VERSION.RELEASE} (SDK ${Build.VERSION.SDK_INT})")
            appendLine("Export: ${sdf.format(Date())}")
            appendLine("Entries: ${entries.size}")
            appendLine("==========================================")
            appendLine()
        }
        return header + entries.joinToString("\n")
    }

    fun clear() {
        entries.clear()
    }
}
