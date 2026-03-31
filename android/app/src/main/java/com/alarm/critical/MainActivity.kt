package com.alarm.critical

import android.content.Intent
import android.content.SharedPreferences
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.DeviceRegister
import com.alarm.critical.model.LoginRequest
import kotlinx.coroutines.launch
import java.util.UUID

class MainActivity : AppCompatActivity() {
    private lateinit var prefs: SharedPreferences

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        prefs = getSharedPreferences("alarm_prefs", MODE_PRIVATE)

        val savedToken = prefs.getString("token", null)
        if (savedToken != null) {
            goToDashboard(savedToken)
            return
        }

        val nameInput = findViewById<EditText>(R.id.nameInput)
        val passwordInput = findViewById<EditText>(R.id.passwordInput)
        val loginButton = findViewById<Button>(R.id.loginButton)
        val statusText = findViewById<TextView>(R.id.statusText)

        // Pré-rempli pour les tests
        nameInput.setText("user1")
        passwordInput.setText("user123")

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
                    val response = ApiProvider.service.login(LoginRequest(name, password))
                    if (response.isSuccessful) {
                        val tokenResponse = response.body()!!
                        val token = tokenResponse.access_token

                        prefs.edit()
                            .putString("token", token)
                            .putString("user_name", tokenResponse.user.name)
                            .putInt("user_id", tokenResponse.user.id)
                            .apply()

                        // Register (no-op côté serveur, gardé pour compat)
                        val deviceToken = prefs.getString("device_token", null)
                            ?: UUID.randomUUID().toString().also {
                                prefs.edit().putString("device_token", it).apply()
                            }
                        ApiProvider.service.registerDevice(
                            "Bearer $token",
                            DeviceRegister(deviceToken)
                        )

                        goToDashboard(token)
                    } else {
                        runOnUiThread {
                            statusText.text = "Échec de connexion : ${response.code()}"
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
