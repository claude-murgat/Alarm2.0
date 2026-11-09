package com.alarm.critical.api

import android.content.Context
import android.util.Log
import com.alarm.critical.model.RefreshRequest
import com.alarm.critical.util.AppLogger
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import retrofit2.Response

/**
 * INV-ANDROID-508 : appels authentifies avec le token courant des prefs ; sur 401, un seul
 * renouvellement via /auth/refresh (INV-079), puis la requete est rejouee une fois.
 */
object Auth {
    private const val TAG = "Auth"
    private val mutex = Mutex()

    private fun prefs(context: Context) = context.getSharedPreferences("alarm_prefs", Context.MODE_PRIVATE)

    suspend fun <T> call(context: Context, block: suspend ApiService.(auth: String) -> Response<T>): Response<T> {
        val token = prefs(context).getString("token", null)
        val resp = ApiProvider.service.block("Bearer $token")
        if (resp.code() != 401) return resp
        val fresh = refresh(context, token) ?: return resp
        return ApiProvider.service.block("Bearer $fresh")
    }

    /**
     * Nouvel access token stocke en prefs, ou null si le refresh echoue. [expired] : le token refuse ;
     * si les prefs en ont deja un autre (renouvele par un appel concurrent), il est rendu tel quel.
     */
    suspend fun refresh(context: Context, expired: String?): String? = mutex.withLock {
        val prefs = prefs(context)
        prefs.getString("token", null)?.let { if (it != expired) return it }
        // INV-079 : lire le refresh_token persiste au login. Si absent (cas
        // legacy : login pre-INV-079 ou apres un logout/clear), refresh impossible.
        val refresh = prefs.getString("refresh_token", null)
        if (refresh.isNullOrEmpty()) {
            Log.w(TAG, "Refresh impossible : aucun refresh_token stocke (login pre-INV-079 ?)")
            AppLogger.log("Auth", "Refresh impossible : refresh_token absent en SharedPreferences (INV-079)")
            return null
        }
        try {
            val response = ApiProvider.service.refreshToken(RefreshRequest(refresh_token = refresh))
            if (response.isSuccessful) {
                val newToken = response.body()?.access_token
                if (newToken != null) {
                    prefs.edit().putString("token", newToken).apply()
                    Log.i(TAG, "Token renouvelé avec succès")
                    AppLogger.log("Auth", "Refresh OK (INV-079) : nouveau access token stocke")
                } else {
                    AppLogger.log("Auth", "Refresh 2xx mais body sans access_token (incoherence backend)")
                }
                newToken
            } else {
                Log.w(TAG, "Refresh token échoué: ${response.code()}")
                AppLogger.log(
                    "Auth",
                    "Refresh KO HTTP ${response.code()} : refresh_token rejeté par /auth/refresh (revoque, expire, ou backend pre-INV-079)"
                )
                null
            }
        } catch (e: Exception) {
            Log.e(TAG, "Refresh token erreur: ${e.message}")
            AppLogger.log("Auth", "Refresh erreur reseau : ${e.message}")
            null
        }
    }
}
