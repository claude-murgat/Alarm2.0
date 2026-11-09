package com.alarm.critical.update

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import com.alarm.critical.BuildConfig
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.util.AppLogger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.security.MessageDigest

/** INV-ANDROID-705 : cote reseau et installeur de la mise a jour integree, remplace par un faux dans les tests. */
interface UpdateBackend {
    /** Ecrit le contenu de [url] dans [dest] ; leve une exception en cas d'echec (dont HTTP 404). */
    fun download(url: String, dest: File)
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

    /** [version] : version installee simulee ; par defaut celle du build (null en debug). */
    @Synchronized
    fun override(fake: UpdateBackend, version: String? = releaseVersion) {
        _override = fake
        installedVersion = version
    }

    @Synchronized
    fun reset() {
        _override = null
        installedVersion = releaseVersion
    }
}

object AppUpdate {
    private const val TAG = "MiseAJour"
    const val CHECK_INTERVAL_MS = 15 * 60_000L  // reprise rapide apres un 404 (release pas encore publiee)
    private const val RELEASES = "https://github.com/claude-murgat/Alarm2.0/releases/download"
    private val RELEASE = Regex("""(\d{1,6})\.(\d{1,6})\.(\d{1,6})""")

    private fun parse(v: String) = RELEASE.matchEntire(v)?.groupValues?.drop(1)?.map { it.toInt() }

    /** [server] s'il est une release X.Y.Z plus recente que [installed] (X.Y.Z aussi), sinon null. */
    fun newerRelease(installed: String, server: String): String? {
        val mine = parse(installed) ?: return null
        val theirs = parse(server) ?: return null
        val diff = theirs.zip(mine).firstOrNull { (s, i) -> s != i } ?: return null
        return server.takeIf { diff.first > diff.second }
    }

    /**
     * (version, APK verifie) de la release plus recente annoncee par /health, ou null : build debug,
     * a jour, serveur injoignable, telechargement ou empreinte en echec (nouvel essai a la verification suivante).
     */
    suspend fun findVerifiedUpdate(context: Context): Pair<String, File>? {
        val installed = UpdateProvider.installedVersion ?: return null
        val server = try { ApiProvider.service.getHealth().body()?.version } catch (e: Exception) { null } ?: return null
        val dir = File(context.cacheDir, "update")
        val version = newerRelease(installed, server)
        if (version == null) {
            withContext(Dispatchers.IO) { dir.deleteRecursively() }  // APK d'une mise a jour deja faite
            return null
        }
        AppLogger.log(TAG, "version $version disponible sur le serveur (installee : $installed)")
        return withContext(Dispatchers.IO) { download(dir, version) }?.let { version to it }
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
                UpdateProvider.backend.download(url, apk)
            }
            if (sha256(apk) == expected) {
                AppLogger.log(TAG, "$name pret, empreinte SHA-256 verifiee")
                apk
            } else {
                apk.delete()
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
}

/** Telechargement depuis la release GitHub (depot public) et installation par PackageInstaller. */
object GitHubUpdateBackend : UpdateBackend {
    private val http by lazy { OkHttpClient() }  // suit la redirection de GitHub vers son stockage

    override fun download(url: String, dest: File) {
        http.newCall(Request.Builder().url(url).build()).execute().use { r ->
            if (!r.isSuccessful) throw IOException("HTTP ${r.code}")
            dest.outputStream().use { r.body!!.byteStream().copyTo(it) }
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
