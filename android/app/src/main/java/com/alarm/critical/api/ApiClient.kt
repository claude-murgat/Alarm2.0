package com.alarm.critical.api

import android.util.Log
import com.alarm.critical.BuildConfig
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

object ApiClient {
    private val TAG = "ApiClient"

    // Liste des URLs backend : [primaire (VPS1), secondaire (VPS2)]
    // Configurées au build-time via BuildConfig (voir build.gradle.kts)
    private val BACKEND_URLS = listOf(
        BuildConfig.PRIMARY_BACKEND_URL,
        BuildConfig.FALLBACK_BACKEND_URL
    )

    // Index de l'URL courante (0 = primaire, 1 = secondaire)
    @Volatile
    var currentUrlIndex: Int = 0

    // Compteur d'échecs consécutifs — déclenche un failover à 3
    @Volatile
    var consecutiveFailures: Int = 0

    private val loggingInterceptor = HttpLoggingInterceptor().apply {
        level = HttpLoggingInterceptor.Level.BODY
    }

    private val httpClient = OkHttpClient.Builder()
        .addInterceptor(loggingInterceptor)
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()

    // Cache des instances Retrofit par URL — une seule instance créée par URL
    private val serviceCache = mutableMapOf<String, ApiService>()

    /**
     * Retourne le service Retrofit correspondant à l'URL courante.
     * Propriété calculée : si currentUrlIndex change, le prochain appel
     * utilise automatiquement la nouvelle URL.
     */
    val service: ApiService
        @Synchronized get() {
            val url = BACKEND_URLS[currentUrlIndex]
            return serviceCache.getOrPut(url) {
                Retrofit.Builder()
                    .baseUrl(url)
                    .client(httpClient)
                    .addConverterFactory(GsonConverterFactory.create())
                    .build()
                    .create(ApiService::class.java)
            }
        }

    /**
     * Bascule vers l'URL suivante dans la liste (rotation circulaire).
     * Réinitialise le compteur d'échecs.
     */
    @Synchronized
    fun switchToNextUrl() {
        val prev = currentUrlIndex
        currentUrlIndex = (currentUrlIndex + 1) % BACKEND_URLS.size
        consecutiveFailures = 0
        Log.w(TAG, "Failover: bascule URL[$prev]=${BACKEND_URLS[prev]} → URL[$currentUrlIndex]=${BACKEND_URLS[currentUrlIndex]}")
    }
}
