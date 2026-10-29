package com.alarm.critical

import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.api.ApiService
import com.google.android.material.chip.Chip
import com.google.android.material.chip.ChipGroup
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import retrofit2.Response
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone

/**
 * INV-ANDROID-011 : base des ecrans du menu. Titre + retour, contenu construit en
 * code (comme l'historique du dashboard). Un 403 ferme l'ecran : le menu cache les
 * entrees admin aux autres, mais c'est le serveur qui garde.
 */
abstract class MenuScreenActivity(private val screenTitle: String) : AppCompatActivity() {
    protected lateinit var content: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_menu_screen)
        findViewById<TextView>(R.id.screenTitle).text = screenTitle
        findViewById<View>(R.id.backButton).setOnClickListener { finish() }
        content = findViewById(R.id.screenContent)
        reload()
    }

    /** Charge les donnees et reconstruit [content]. */
    protected abstract suspend fun load()

    protected fun reload() {
        lifecycleScope.launch { load() }
    }

    /** Appel API avec le token courant (relu : le service peut l'avoir rafraichi).
     *  Renvoie le corps si 2xx, sinon null apres un toast ; un 403 ferme l'ecran. */
    protected suspend fun <T> api(call: suspend ApiService.(auth: String) -> Response<T>): T? {
        if (isFinishing) return null
        val token = getSharedPreferences("alarm_prefs", MODE_PRIVATE).getString("token", "")
        val resp = try {
            ApiProvider.service.call("Bearer $token")
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            toast("Serveur injoignable")
            return null
        }
        if (resp.isSuccessful) return resp.body()
        if (resp.code() == 403) {
            toast("Réservé aux administrateurs")
            finish()
        } else {
            toast("Échec (HTTP ${resp.code()})" + errorDetail(resp))
        }
        return null
    }

    /** `detail` de FastAPI : un texte, ou la liste d'erreurs pydantic (422) dont on garde la 1re. */
    private fun errorDetail(resp: Response<*>): String = try {
        when (val d = JSONObject(resp.errorBody()?.string().orEmpty()).get("detail")) {
            is JSONArray -> " : " + d.getJSONObject(0).getString("msg")
            else -> " : $d"
        }
    } catch (_: Exception) {
        ""
    }

    protected fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    protected val Int.dp: Int get() = (this * resources.displayMetrics.density).toInt()

    protected fun label(text: String, sizeSp: Float = 14f, color: String = "#FFFFFF", bold: Boolean = false) =
        TextView(this).apply {
            this.text = text
            textSize = sizeSp
            setTextColor(Color.parseColor(color))
            if (bold) typeface = Typeface.DEFAULT_BOLD
        }

    protected fun section(title: String) {
        content.addView(label(title.uppercase(), 12f, "#94a3b8", bold = true).apply {
            setPadding(0, 16.dp, 0, 8.dp)
        })
    }

    /** Une carte : titre, sous-titre, puis [trailing] a droite (boutons, ...). */
    protected fun row(
        title: String,
        subtitle: String? = null,
        trailing: List<View> = emptyList(),
        into: LinearLayout = content,
    ): LinearLayout {
        val texts = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f)
            addView(label(title, 15f, bold = true))
            subtitle?.let { addView(label(it, 12f, "#94a3b8")) }
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundResource(R.drawable.card_default)
            setPadding(14.dp, 12.dp, 8.dp, 12.dp)
            layoutParams = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { bottomMargin = 8.dp }
            addView(texts)
            trailing.forEach { addView(it) }
            into.addView(this)
        }
    }

    /** Petit bouton texte d'une ligne (▲, ▼, ✕) ; [description] pour TalkBack et les tests. */
    protected fun action(text: String, description: String, onClick: () -> Unit) =
        label(text, 18f, "#94a3b8").apply {
            contentDescription = description
            setPadding(12.dp, 8.dp, 12.dp, 8.dp)
            setOnClickListener { onClick() }
        }

    protected fun button(text: String, onClick: () -> Unit) = Button(this).apply {
        this.text = text
        setOnClickListener { onClick() }
        content.addView(this)
    }

    /** Saisie d'un nombre de minutes, precedee de son libelle. */
    protected fun numberField(title: String, value: Double?): EditText {
        content.addView(label(title, 13f, "#94a3b8"))
        return EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
            setText(value?.fmt().orEmpty())
            setTextColor(Color.WHITE)
            content.addView(this)
        }
    }

    /** Choix unique en puces (periode, site) ; [onPick] recoit l'index choisi. */
    protected fun choices(labels: List<String>, selected: Int, onPick: (Int) -> Unit) {
        val group = ChipGroup(this).apply {
            isSingleSelection = true
            isSelectionRequired = true
        }
        labels.forEachIndexed { i, l ->
            group.addView(Chip(this).apply {
                id = View.generateViewId()
                text = l
                isCheckable = true
                isChecked = i == selected
            })
        }
        group.setOnCheckedStateChangeListener { g, ids ->
            ids.firstOrNull()?.let { onPick(g.indexOfChild(g.findViewById(it))) }
        }
        content.addView(group)
    }
}

/** INV-ANDROID-112 : le backend envoie de l'UTC sans suffixe ; affichage dans le
 *  fuseau du telephone (sinon 13:32 au lieu de 15:32 a Paris). */
fun formatDate(dateStr: String?): String {
    if (dateStr == null) return "?"
    return try {
        val sdf = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.getDefault())
        sdf.timeZone = TimeZone.getTimeZone("UTC")
        val display = SimpleDateFormat("dd/MM HH:mm", Locale.getDefault())
        display.format(sdf.parse(dateStr)!!)
    } catch (_: Exception) {
        dateStr.take(16)
    }
}

/** 15.0 -> "15", 2.5 -> "2.5". */
fun Double.fmt(): String = if (this % 1.0 == 0.0) toLong().toString() else toString()

/** Lieu d'un noeud (INV-161), ecrit comme le web : "Izeron", "OVH", "-" si inconnu. */
fun place(location: String?): String = when {
    location.isNullOrBlank() -> "-"
    location == "ovh" -> "OVH"
    else -> location.replaceFirstChar { it.uppercase() }
}
