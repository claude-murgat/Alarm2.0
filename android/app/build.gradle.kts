import java.io.ByteArrayOutputStream

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.gms.google-services")
}

// INV-ANDROID-109 : versionCode et versionName derives automatiquement de
// git à chaque build. Aucune action manuelle requise pour "bumper" la
// version — tout commit produit une version distincte et tracable au sha.
//
// - versionCode = nombre total de commits sur la branche courante (monotone)
// - versionName = "<base>.<commits>-<shortSha>[-dirty]"
//
// Si le repo n'est pas accessible (CI sans git, etc.), fallback sur 1 / "1.0-unknown".
fun gitOutput(vararg args: String): String? {
    return try {
        val stdout = ByteArrayOutputStream()
        val result = exec {
            commandLine = listOf("git") + args.toList()
            standardOutput = stdout
            errorOutput = ByteArrayOutputStream()
            isIgnoreExitValue = true
            workingDir = rootDir.parentFile  // racine du repo (au-dessus de android/)
        }
        if (result.exitValue == 0) stdout.toString().trim().takeIf { it.isNotEmpty() } else null
    } catch (e: Exception) {
        null
    }
}

val gitCommitCount: Int = gitOutput("rev-list", "--count", "HEAD")?.toIntOrNull() ?: 1
val gitShortSha: String = gitOutput("rev-parse", "--short", "HEAD") ?: "unknown"
val gitDirty: Boolean = !gitOutput("status", "--porcelain").isNullOrBlank()
val baseVersion = "1.0"
val computedVersionName = buildString {
    append("$baseVersion.$gitCommitCount-$gitShortSha")
    if (gitDirty) append("-dirty")
}

android {
    namespace = "com.alarm.critical"
    compileSdk = 34

    buildFeatures {
        buildConfig = true
    }

    defaultConfig {
        applicationId = "com.alarm.critical"
        minSdk = 26
        targetSdk = 34
        versionCode = gitCommitCount
        versionName = computedVersionName
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // URLs backend dev (10.0.2.2 = alias localhost du PC depuis l'émulateur).
        // Overridées en `release` ci-dessous pour pointer vers le cluster prod.
        buildConfigField("String", "PRIMARY_BACKEND_URL", "\"http://10.0.2.2:8000/\"")
        buildConfigField("String", "FALLBACK_BACKEND_URL", "\"http://10.0.2.2:8001/\"")
        buildConfigField("String", "FALLBACK_BACKEND_URL_2", "\"http://10.0.2.2:8002/\"")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Cluster prod (cf .env.prod.node{1,2,3} + docs/PROVISIONING_ONSITE.md §22bis).
            // Ordre cloud-first : NODE3 OVH est le seul backend joignable hors LAN site
            // (UFW onsite-1/-2 limite l'accès à 172.16.0.0/16). En 4G ou WiFi externe,
            // les onsite ne répondent pas — donc primary = cloud évite ~6s de timeout
            // au démarrage. Sur LAN site, le failover circulaire (INV-ANDROID-402)
            // bascule sur les onsite si le cloud devient injoignable.
            buildConfigField("String", "PRIMARY_BACKEND_URL", "\"http://51.210.105.102:8000/\"")
            buildConfigField("String", "FALLBACK_BACKEND_URL", "\"http://172.16.1.121:8000/\"")
            buildConfigField("String", "FALLBACK_BACKEND_URL_2", "\"http://172.16.1.120:8000/\"")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        resources {
            excludes += setOf("META-INF/LICENSE.md", "META-INF/LICENSE-notice.md")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")

    // Retrofit for HTTP
    implementation("com.squareup.retrofit2:retrofit:2.9.0")
    implementation("com.squareup.retrofit2:converter-gson:2.9.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")

    // Coroutines
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.7.0")

    // Firebase Cloud Messaging
    implementation(platform("com.google.firebase:firebase-bom:33.0.0"))
    implementation("com.google.firebase:firebase-messaging")

    // Espresso E2E tests
    androidTestImplementation("androidx.test.ext:junit:1.1.5")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.5.1")
    androidTestImplementation("androidx.test.espresso:espresso-intents:3.5.1")
    androidTestImplementation("androidx.test:runner:1.5.2")
    androidTestImplementation("androidx.test:rules:1.5.0")
    androidTestImplementation("androidx.test.uiautomator:uiautomator:2.2.0")
}
