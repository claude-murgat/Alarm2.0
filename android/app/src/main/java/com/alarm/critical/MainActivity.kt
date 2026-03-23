package com.alarm.critical

import android.content.Intent
import android.content.SharedPreferences
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.alarm.critical.api.ApiClient
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

        // Check if already logged in
        val savedToken = prefs.getString("token", null)
        if (savedToken != null) {
            goToDashboard(savedToken)
            return
        }

        val emailInput = findViewById<EditText>(R.id.emailInput)
        val passwordInput = findViewById<EditText>(R.id.passwordInput)
        val loginButton = findViewById<Button>(R.id.loginButton)
        val statusText = findViewById<TextView>(R.id.statusText)

        // Pre-fill for testing
        emailInput.setText("user1@alarm.local")
        passwordInput.setText("user123")

        loginButton.setOnClickListener {
            val email = emailInput.text.toString().trim()
            val password = passwordInput.text.toString().trim()

            if (email.isEmpty() || password.isEmpty()) {
                statusText.text = "Please enter email and password"
                return@setOnClickListener
            }

            loginButton.isEnabled = false
            statusText.text = "Logging in..."

            lifecycleScope.launch {
                try {
                    val response = ApiClient.service.login(LoginRequest(email, password))
                    if (response.isSuccessful) {
                        val tokenResponse = response.body()!!
                        val token = tokenResponse.access_token

                        // Save token
                        prefs.edit()
                            .putString("token", token)
                            .putString("user_name", tokenResponse.user.name)
                            .putString("user_email", tokenResponse.user.email)
                            .putInt("user_id", tokenResponse.user.id)
                            .apply()

                        // Register device
                        val deviceToken = prefs.getString("device_token", null)
                            ?: UUID.randomUUID().toString().also {
                                prefs.edit().putString("device_token", it).apply()
                            }
                        ApiClient.service.registerDevice(
                            "Bearer $token",
                            DeviceRegister(deviceToken)
                        )

                        goToDashboard(token)
                    } else {
                        runOnUiThread {
                            statusText.text = "Login failed: ${response.code()}"
                            loginButton.isEnabled = true
                        }
                    }
                } catch (e: Exception) {
                    runOnUiThread {
                        statusText.text = "Connection error: ${e.message}"
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
