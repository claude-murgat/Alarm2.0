package com.alarm.critical.util

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Build
import android.telephony.PhoneStateListener
import android.telephony.ServiceState
import android.telephony.TelephonyCallback
import android.telephony.TelephonyManager
import androidx.core.content.ContextCompat

/**
 * INV-ANDROID-305 — Détection "réseau totalement perdu".
 *
 * Le téléphone est considéré "sans aucun réseau" si **les deux** conditions
 * sont vraies simultanément :
 *   1. Pas de data utilisable (ConnectivityManager.activeNetwork == null ou
 *      sans NET_CAPABILITY_INTERNET + VALIDATED).
 *   2. Pas de signal cellulaire voix/SMS (TelephonyManager.serviceState
 *      != STATE_IN_SERVICE).
 *
 * Détection réactive : callbacks `registerDefaultNetworkCallback` (data) et
 * `registerTelephonyCallback`/`listen(LISTEN_SERVICE_STATE)` (cellular).
 * Pas de polling périodique.
 *
 * Fail open : si la permission `READ_PHONE_STATE` n'est pas accordée, on
 * suppose `cellularInService = true` (conservateur — la sonnerie locale ne
 * s'arme pas tant qu'on n'est pas SÛRS d'avoir perdu les deux canaux).
 *
 * Utilisé par INV-ANDROID-302 (sonnerie locale armée seulement si
 * heartbeatLostSince > 2 min ET isNoNetwork).
 */
object NetworkAvailabilityMonitor {

    @Volatile var dataAvailable: Boolean = true
        private set

    @Volatile var cellularInService: Boolean = true
        private set

    /** True si data ET cellulaire sont tous deux indisponibles. */
    val isNoNetwork: Boolean
        get() = !dataAvailable && !cellularInService

    private var initialized = false
    private var connectivityManager: ConnectivityManager? = null
    private var telephonyManager: TelephonyManager? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private var phoneStateListener: PhoneStateListener? = null
    private var telephonyCallback: Any? = null  // TelephonyCallback (API 31+), typé Any pour compat

    @Synchronized
    fun init(context: Context) {
        if (initialized) return
        val appCtx = context.applicationContext

        // --- Data (Wi-Fi / cellular data) -----------------------------------
        val cm = appCtx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        connectivityManager = cm

        // État initial (sync, juste pour ne pas démarrer "no network" si on a déjà du data)
        dataAvailable = _computeDataAvailable(cm)

        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                dataAvailable = _computeDataAvailable(cm)
            }
            override fun onLost(network: Network) {
                dataAvailable = _computeDataAvailable(cm)
            }
            override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
                // VALIDATED peut changer après captive portal accept
                dataAvailable = _computeDataAvailable(cm)
            }
        }
        networkCallback = cb
        try {
            cm.registerDefaultNetworkCallback(cb)
        } catch (e: SecurityException) {
            // ACCESS_NETWORK_STATE manquante en théorie (install-time, donc rare)
            AppLogger.log("Network", "registerDefaultNetworkCallback denied: ${e.message}")
        }

        // --- Cellulaire (voix/SMS) ------------------------------------------
        val tm = appCtx.getSystemService(Context.TELEPHONY_SERVICE) as? TelephonyManager
        telephonyManager = tm

        val hasReadPhoneState = ContextCompat.checkSelfPermission(
            appCtx, Manifest.permission.READ_PHONE_STATE
        ) == PackageManager.PERMISSION_GRANTED

        if (tm == null || !hasReadPhoneState) {
            // Fail open : on suppose cellulaire dispo (conservateur).
            cellularInService = true
            AppLogger.log(
                "Network",
                "TelephonyManager indisponible ou READ_PHONE_STATE refusee — " +
                "cellularInService=true par defaut (fail open INV-305)"
            )
            initialized = true
            return
        }

        // Init avec l'état courant si disponible
        cellularInService = _isInServiceFrom(tm)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val tcb = object : TelephonyCallback(),
                TelephonyCallback.ServiceStateListener {
                override fun onServiceStateChanged(serviceState: ServiceState) {
                    cellularInService = serviceState.state == ServiceState.STATE_IN_SERVICE
                }
            }
            telephonyCallback = tcb
            try {
                tm.registerTelephonyCallback(appCtx.mainExecutor, tcb)
            } catch (e: SecurityException) {
                AppLogger.log("Network", "registerTelephonyCallback denied: ${e.message}")
            }
        } else {
            @Suppress("DEPRECATION")
            val psl = object : PhoneStateListener() {
                @Suppress("OVERRIDE_DEPRECATION")
                override fun onServiceStateChanged(serviceState: ServiceState?) {
                    cellularInService = serviceState?.state == ServiceState.STATE_IN_SERVICE
                }
            }
            phoneStateListener = psl
            try {
                @Suppress("DEPRECATION")
                tm.listen(psl, PhoneStateListener.LISTEN_SERVICE_STATE)
            } catch (e: SecurityException) {
                AppLogger.log("Network", "listen LISTEN_SERVICE_STATE denied: ${e.message}")
            }
        }

        initialized = true
    }

    /**
     * À appeler au stop du service polling (libérer les callbacks).
     * Idempotent : pas d'erreur si init n'a pas été appelé.
     */
    @Synchronized
    fun release() {
        networkCallback?.let { cb ->
            try {
                connectivityManager?.unregisterNetworkCallback(cb)
            } catch (_: Exception) { }
        }
        networkCallback = null

        val tm = telephonyManager
        if (tm != null) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                (telephonyCallback as? TelephonyCallback)?.let {
                    try { tm.unregisterTelephonyCallback(it) } catch (_: Exception) { }
                }
            } else {
                phoneStateListener?.let {
                    @Suppress("DEPRECATION")
                    try { tm.listen(it, PhoneStateListener.LISTEN_NONE) } catch (_: Exception) { }
                }
            }
        }
        telephonyCallback = null
        phoneStateListener = null
        connectivityManager = null
        telephonyManager = null
        initialized = false
    }

    private fun _computeDataAvailable(cm: ConnectivityManager): Boolean {
        return try {
            val network = cm.activeNetwork ?: return false
            val caps = cm.getNetworkCapabilities(network) ?: return false
            // Exige Internet + Validated (couvre les captive portals non validés)
            caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
                caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
        } catch (e: SecurityException) {
            // ACCESS_NETWORK_STATE manquante → fail open (data réputée dispo)
            true
        }
    }

    private fun _isInServiceFrom(tm: TelephonyManager): Boolean {
        return try {
            val state = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                tm.serviceState?.state
            } else {
                null  // legacy : pas d'accès direct, on attendra le 1er callback
            }
            // Null = pas encore lu → fail open
            state == null || state == ServiceState.STATE_IN_SERVICE
        } catch (e: SecurityException) {
            true
        }
    }

    // ── Helpers de test : injection directe de l'état (bypass système) ───
    // Utilisés UNIQUEMENT par les unit tests (cf NetworkAvailabilityMonitorTest).
    // En prod, les états sont pilotés par les callbacks système.

    /**
     * INV-ANDROID-305 — pure fn calculant le drapeau noNetwork à partir des 2 états bas niveau.
     * Extraite pour rester testable sans Android runtime.
     */
    fun computeIsNoNetwork(dataAvail: Boolean, cellInService: Boolean): Boolean {
        return !dataAvail && !cellInService
    }

    @androidx.annotation.VisibleForTesting
    fun _setStatesForTest(dataAvail: Boolean, cellInService: Boolean) {
        dataAvailable = dataAvail
        cellularInService = cellInService
    }
}
