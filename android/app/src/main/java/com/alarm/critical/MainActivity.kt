package com.alarm.critical

import android.content.Intent
import android.content.SharedPreferences
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import android.util.Log
import com.alarm.critical.api.ApiClient
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.DeviceRegister
import com.alarm.critical.model.FcmTokenRequest
import com.alarm.critical.model.LoginRequest
import com.alarm.critical.service.AlarmPollingService
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await
import java.util.UUID

class MainActivity : AppCompatActivity() {
    private lateinit var prefs: SharedPreferences

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)

        // Toujours repartir de l'URL 0 au demarrage
        ApiClient.currentUrlIndex = 0
        ApiClient.consecutiveFailures = 0

        val savedToken = prefs.getString("token", null)
        if (savedToken != null) {
            goToDashboard(savedToken)
            return
        }

        val nameInput = findViewById<EditText>(R.id.nameInput)
        val passwordInput = findViewById<EditText>(R.id.passwordInput)
        val loginButton = findViewById<Button>(R.id.loginButton)
        val statusText = findViewById<TextView>(R.id.statusText)

        // Pré-rempli uniquement en mode debug (jamais en production)
        if (BuildConfig.DEBUG) {
            nameInput.setText("user1")
            passwordInput.setText("user123")
        }

        loginButton.setOnClickListener {
            val name = nameInput.text.toString().trim()
            val password = passwordInput.text.toString().trim()

            if (name.isEmpty() || password.isEmpty()) {
                statusText.text = "Veuillez entrer votre nom et mot de passe"
                return@setOnClickListener
            }

            loginButton.isEnabled = false
            statusText.text = "Connexion en cours..."

            lifecycleScope.launch {
                try {
                    // Trouver un backend qui repond avant de tenter le login
                    var response = try { ApiProvider.service.login(LoginRequest(name, password)) } catch (e: Exception) { null }
                    if (response == null || !response.isSuccessful) {
                        // L'URL courante ne marche pas — essayer les autres
                        for (i in 0 until 3) {
                            ApiClient.switchToNextUrl()
                            response = try { ApiProvider.service.login(LoginRequest(name, password)) } catch (e: Exception) { null }
                            if (response != null && response.isSuccessful) break
                        }
                    }
                    if (response != null && response.isSuccessful) {
                        val tokenResponse = response.body()!!
                        val token = tokenResponse.access_token
                        val isOncall = tokenResponse.is_oncall

                        // Clear les erreurs d'auth de la session precedente
                        AlarmPollingService.authErrorAlarm = false
                        AlarmPollingService.authErrorMessage = null

                        prefs.edit()
                            .putString("token", token)
                            .putString("user_name", tokenResponse.user.name)
                            .putInt("user_id", tokenResponse.user.id)
                            .putBoolean("is_oncall", isOncall)
                            .putBoolean("started_by_fcm", false)
                            .apply()

                        // Register device (no-op cote serveur, garde pour compat)
                        val deviceId = prefs.getString("device_token", null)
                            ?: UUID.randomUUID().toString().also {
                                prefs.edit().putString("device_token", it).apply()
                            }
                        ApiProvider.service.registerDevice(
                            "Bearer $token",
                            DeviceRegister(deviceId)
                        )

                        // Enregistrer le token FCM sur le backend
                        try {
                            val fcmToken = FirebaseMessaging.getInstance().token.await()
                            ApiProvider.service.registerFcmToken(
                                "Bearer $token",
                                FcmTokenRequest(token = fcmToken, device_id = deviceId)
                            )
                            Log.d("MainActivity", "FCM token registered: ${fcmToken.take(20)}...")
                        } catch (e: Exception) {
                            Log.e("MainActivity", "FCM token registration failed: ${e.message}")
                        }

                        if (isOncall) {
                            // Mode astreinte : demarrer le foreground service
                            goToDashboard(token)
                        } else {
                            // Mode veille : pas de foreground service, juste le dashboard
                            goToDashboard(token)
                        }
                    } else {
                        runOnUiThread {
                            statusText.text = "Échec de connexion : ${response?.code() ?: "aucun serveur disponible"}"
                            loginButton.isEnabled = true
                        }
                    }
                } catch (e: Exception) {
                    runOnUiThread {
                        statusText.text = "Erreur de connexion : ${e.message}"
                        loginButton.isEnabled = true
                    }
                }
            }
        }
    }

    private fun goToDashboard(token: String) {
        val intent = Intent(this, DashboardActivity::class.java)
        intent.putExtra("token", token)
        startActivity(intent)
        finish()
    }
}
