package com.alarm.critical.update

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.view.View
import android.widget.ProgressBar
import androidx.appcompat.app.AlertDialog
import com.alarm.critical.BuildConfig
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.service.AlarmPollingService
import com.alarm.critical.service.AlarmSoundManager
import com.alarm.critical.update.UpdateState.Phase
import com.alarm.critical.util.AppLogger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.security.MessageDigest

/** INV-ANDROID-705 : cote reseau et installeur de la mise a jour integree, remplace par un faux dans les tests. */
interface UpdateBackend {
    /** Ecrit le contenu de [url] dans [dest] ; leve une exception en cas d'echec (dont HTTP 404).
     *  [onProgress] : pourcentage recu, quand la taille (Content-Length) est connue (INV-ANDROID-706). */
    fun download(url: String, dest: File, onProgress: (Int) -> Unit = {})
    /** Android autorise-t-il l'app a installer des applications (« applis inconnues ») ? */
    fun canInstall(context: Context): Boolean
    /** Lance l'installation de [apk] ; Android demande confirmation, sauf mise a jour silencieuse (12+). */
    fun install(context: Context, apk: File)
}

/** Meme principe qu'ApiProvider : implementation reelle par defaut, faux dans les tests. */
object UpdateProvider {
    // Un build debug ne se met jamais a jour : pas de version a comparer.
    private val releaseVersion: String? = BuildConfig.VERSION_NAME.takeUnless { BuildConfig.DEBUG }
    private var _override: UpdateBackend? = null

    val backend: UpdateBackend
        @Synchronized get() = _override ?: GitHubUpdateBackend

    /** Version installee (null : build debug). */
    @Volatile
    var installedVersion: String? = releaseVersion
        private set

    /** [version] : version installee simulee ; par defaut celle du build (null en debug). Etat remis a zero. */
    @Synchronized
    fun override(fake: UpdateBackend, version: String? = releaseVersion) {
        _override = fake
        installedVersion = version
        AppUpdate.reset()
    }

    @Synchronized
    fun reset() {
        _override = null
        installedVersion = releaseVersion
        AppUpdate.reset()
    }
}

/** INV-ANDROID-706 : etat de la mise a jour, le meme pour le bandeau du tableau de bord et l'ecran « Mises à jour ». */
data class UpdateState(
    val phase: Phase = Phase.NOT_CHECKED,
    val server: String? = null,       // version annoncee par /health a la derniere verification, null : inconnue
    val version: String? = null,      // release proposee
    val percent: Int? = null,         // telechargement de l'APK, null : taille inconnue
    val apk: File? = null,            // APK verifie (READY)
    val postponed: Boolean = false,   // installation demandee pendant une alarme : relancee a la fin de l'alarme
    val checking: Boolean = false,
    val lastCheck: Long? = null,      // System.currentTimeMillis()
) {
    enum class Phase { NOT_CHECKED, DEV, UP_TO_DATE, UNREACHABLE, DOWNLOADING, READY, FAILED }

    val percentText: String get() = percent?.let { " $it\u00A0%" }.orEmpty()

    /** L'etat en une phrase (ecran « Mises à jour »). */
    val sentence: String get() = when (phase) {
        Phase.NOT_CHECKED -> "Vérification en cours…"
        Phase.DEV -> "Version de développement : pas de mise à jour"
        Phase.UP_TO_DATE -> "À jour"
        Phase.UNREACHABLE -> "Serveur injoignable, nouvel essai à la prochaine vérification"
        Phase.DOWNLOADING -> "Version $version disponible — téléchargement…$percentText"
        Phase.READY -> if (postponed) "Mise à jour reportée : alarme en cours" else "Version $version prête à installer"
        Phase.FAILED -> "Téléchargement impossible, nouvel essai à la prochaine vérification"
    }
}

/** Barre de progression du telechargement (bandeau et ecran) : visible pendant le telechargement, indeterminee sans taille. */
fun ProgressBar.showDownload(s: UpdateState) {
    visibility = if (s.phase == Phase.DOWNLOADING) View.VISIBLE else View.GONE
    isIndeterminate = s.percent == null
    progress = s.percent ?: 0
}

object AppUpdate {
    private const val TAG = "MiseAJour"
    const val CHECK_INTERVAL_MS = 15 * 60_000L  // reprise rapide apres un 404 (release pas encore publiee)
    private const val RELEASES = "https://github.com/claude-murgat/Alarm2.0/releases/download"
    private val RELEASE = Regex("""(\d{1,6})\.(\d{1,6})\.(\d{1,6})""")

    private val _state = MutableStateFlow(UpdateState())
    val state: StateFlow<UpdateState> = _state.asStateFlow()
    private val scope = MainScope()  // pas celui d'un ecran : la verification survit a l'ecran qui l'a lancee
    @Volatile private var job: Job? = null

    private fun parse(v: String) = RELEASE.matchEntire(v)?.groupValues?.drop(1)?.map { it.toInt() }

    /** [server] s'il est une release X.Y.Z plus recente que [installed] (X.Y.Z aussi), sinon null. */
    fun newerRelease(installed: String, server: String): String? {
        val mine = parse(installed) ?: return null
        val theirs = parse(server) ?: return null
        val diff = theirs.zip(mine).firstOrNull { (s, i) -> s != i } ?: return null
        return server.takeIf { diff.first > diff.second }
    }

    /**
     * Verification (tableau de bord et « Vérifier maintenant ») ; false si une tourne deja :
     * jamais deux en meme temps, un meme APK n'est jamais telecharge deux fois a la fois.
     */
    fun check(context: Context): Boolean {
        if (job?.isActive == true) return false
        val app = context.applicationContext
        _state.update { it.copy(checking = true) }
        job = scope.launch {
            val result = verify(app)
            _state.update { cur ->
                // Coupure passagere (serveur, empreinte) : l'APK deja verifie reste propose
                val keep = cur.phase == Phase.READY &&
                    (result.phase == Phase.UNREACHABLE || result.phase == Phase.FAILED && result.version == cur.version)
                (if (keep) cur else result).copy(server = result.server, postponed = cur.postponed,
                    checking = false, lastCheck = System.currentTimeMillis())
            }
        }
        return true
    }

    /** Tests (UpdateProvider) : verification en cours abandonnee, etat neuf. */
    internal fun reset() {
        job?.cancel()
        _state.value = UpdateState()
    }

    /** Resultat d'une verification : debug, serveur injoignable, a jour, ou release plus recente prete / en echec. */
    private suspend fun verify(context: Context): UpdateState {
        val server = try { ApiProvider.service.getHealth().body()?.version } catch (e: Exception) { null }
        val installed = UpdateProvider.installedVersion ?: return UpdateState(Phase.DEV, server)
        if (server == null) return UpdateState(Phase.UNREACHABLE)
        val dir = File(context.cacheDir, "update")
        val version = newerRelease(installed, server)
        if (version == null) {
            withContext(Dispatchers.IO) { dir.deleteRecursively() }  // APK d'une mise a jour deja faite
            return UpdateState(Phase.UP_TO_DATE, server)
        }
        AppLogger.log(TAG, "version $version disponible sur le serveur (installee : $installed)")
        val apk = withContext(Dispatchers.IO) { download(dir, version) }
        return UpdateState(if (apk != null) Phase.READY else Phase.FAILED, server, version, apk = apk)
    }

    private fun download(dir: File, v: String): File? {
        val name = "alarme-murgat-v$v-$v.apk"  // nom publie par android-release.yml
        val url = "$RELEASES/v$v/$name"
        dir.mkdirs()
        val apk = File(dir, name)
        val shaFile = File(dir, "$name.sha256")
        return try {
            UpdateProvider.backend.download("$url.sha256", shaFile)
            val expected = shaFile.readText().trim().substringBefore(' ').lowercase()  // « <hex>  <nom> »
            if (!apk.exists() || sha256(apk) != expected) {
                AppLogger.log(TAG, "telechargement de $url")
                _state.update { it.copy(phase = Phase.DOWNLOADING, server = v, version = v, percent = null, apk = null) }
                UpdateProvider.backend.download(url, apk) { p -> _state.update { it.copy(percent = p) } }
            }
            if (sha256(apk) == expected) {
                AppLogger.log(TAG, "$name pret, empreinte SHA-256 verifiee")
                apk
            } else {
                apk.delete()
                _state.update { it.copy(phase = Phase.FAILED) }  // avant le log : plus de progression affichee
                AppLogger.log(TAG, "empreinte SHA-256 invalide pour $name : APK supprime, nouvel essai a la prochaine verification")
                null
            }
        } catch (e: Exception) {
            AppLogger.log(TAG, "telechargement de la version $v impossible (${e.message}) : nouvel essai a la prochaine verification")
            null
        }
    }

    private fun sha256(f: File) =
        MessageDigest.getInstance("SHA-256").digest(f.readBytes()).joinToString("") { "%02x".format(it) }

    /** Alarme en cours (acquittee ou non) ou sonnerie : jamais d'installation (telephone d'astreinte). */
    private fun alarmInProgress(context: Context) =
        AlarmPollingService.alarms.isNotEmpty() || AlarmSoundManager(context).isPlaying()

    /**
     * « Mettre à jour » (bandeau) et « Installer » (ecran) : APK pret seulement, reporte pendant une alarme
     * (relance par le tableau de bord a la fin de l'alarme), autorisation « applis inconnues » demandee d'abord.
     */
    fun install(activity: Activity) {
        val s = _state.value
        val apk = s.apk?.takeIf { s.phase == Phase.READY } ?: return
        val version = s.version
        if (alarmInProgress(activity)) {
            if (!s.postponed) AppLogger.log(TAG, "installation de $version reportee : alarme en cours")
            _state.update { it.copy(postponed = true) }
            return
        }
        _state.update { it.copy(postponed = false) }
        val backend = UpdateProvider.backend
        if (!backend.canInstall(activity)) {
            AppLogger.log(TAG, "autorisation « applis inconnues » absente : invite affichee")
            AlertDialog.Builder(activity)
                .setTitle("Autorisation nécessaire")
                .setMessage("Pour installer la mise à jour, Android doit autoriser Alarme Murgat à installer " +
                    "des applications (une seule fois). Activez « Autoriser cette source », puis revenez " +
                    "lancer la mise à jour.")
                .setPositiveButton("Ouvrir les réglages") { _, _ ->
                    activity.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:${activity.packageName}")))
                }
                .setNegativeButton("Annuler", null)
                .show()
            return
        }
        AppLogger.log(TAG, "installation de $version lancee")
        val app = activity.applicationContext
        scope.launch(Dispatchers.IO) {
            try {
                backend.install(app, apk)
            } catch (e: Exception) {
                AppLogger.log(TAG, "installation de $version impossible : ${e.message}")
            }
        }
    }
}

/** Telechargement depuis la release GitHub (depot public) et installation par PackageInstaller. */
object GitHubUpdateBackend : UpdateBackend {
    private val http by lazy { OkHttpClient() }  // suit la redirection de GitHub vers son stockage

    override fun download(url: String, dest: File, onProgress: (Int) -> Unit) {
        http.newCall(Request.Builder().url(url).build()).execute().use { r ->
            if (!r.isSuccessful) throw IOException("HTTP ${r.code}")
            val body = r.body!!
            val total = body.contentLength()  // -1 : taille inconnue, pas de pourcentage
            var done = 0L
            val buf = ByteArray(64 * 1024)
            body.byteStream().use { input ->
                dest.outputStream().use { out ->
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        if (total > 0) onProgress((done * 100 / total).toInt())
                    }
                }
            }
        }
    }

    override fun canInstall(context: Context) = context.packageManager.canRequestPackageInstalls()

    override fun install(context: Context, apk: File) {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        // Android 12+ : l'app qui se met a jour elle-meme peut le faire sans confirmation
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            session.openWrite("alarme-murgat.apk", 0, apk.length()).use { out ->
                apk.inputStream().use { it.copyTo(out) }
                session.fsync(out)
            }
            // Mutable : Android y ajoute le statut de l'installation
            val status = PendingIntent.getBroadcast(context, id, Intent(context, UpdateStatusReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
            session.commit(status.intentSender)
        }
    }
}

/** Statut de la session d'installation : ouvre la confirmation d'Android si elle est demandee. */
class UpdateStatusReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        val label = when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> "confirmation demandee"
            PackageInstaller.STATUS_SUCCESS -> "installee"
            else -> "echec $status (${intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)})"
        }
        AppLogger.log("MiseAJour", "Android : $label")
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                ?.let { context.startActivity(it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
        }
    }
}
