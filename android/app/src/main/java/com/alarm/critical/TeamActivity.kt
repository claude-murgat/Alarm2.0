package com.alarm.critical

import android.text.InputType
import android.view.View
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import androidx.appcompat.app.AlertDialog
import androidx.lifecycle.lifecycleScope
import com.alarm.critical.api.ApiProvider
import com.alarm.critical.model.PhoneUpdate
import com.alarm.critical.model.UserCreateRequest
import com.alarm.critical.model.UserResponse
import kotlinx.coroutines.launch

/** INV-ANDROID-011 (admin) : comptes de l'equipe — liste, creation, suppression, telephone.
 *  INV-ANDROID-114 : la personne de garde (position 1 de la chaine) en tete, mise en evidence. */
class TeamActivity : MenuScreenActivity("Équipe") {

    override suspend fun load() {
        val users = api { getUsers(it) } ?: return
        // Chaine publique, hors api() : illisible -> personne de garde inconnue, sans toast
        val oncallId = runCatching { ApiProvider.service.getEscalationChain().body() }.getOrNull()
            ?.firstOrNull { it.position == 1 }?.user_id
        content.removeAllViews()
        button("Ajouter un utilisateur") { createDialog() }
        users.sortedWith(compareBy({ it.id != oncallId }, { it.name })).forEach { u ->
            val oncall = u.id == oncallId
            val details = listOfNotNull(
                u.phone_number ?: "pas de téléphone",
                "admin".takeIf { u.is_admin },
                "compte test".takeIf { u.is_test },
            )
            row(u.name, details.joinToString(" · "), listOfNotNull(
                if (oncall) oncallBadge() else null,
                action("☎", "Modifier le téléphone de ${u.name}") { phoneDialog(u) },
                action("✕", "Supprimer ${u.name}") { confirmDelete(u) },
            )).apply { if (oncall) setBackgroundResource(R.drawable.card_oncall) }
        }
    }

    /** Badge texte (pas seulement une couleur), memes couleurs que le web (INV-201). */
    private fun oncallBadge() = label("DE GARDE", 11f, "#4ade80", bold = true).apply {
        setBackgroundResource(R.drawable.badge_on_call_green)
        setPadding(10.dp, 4.dp, 10.dp, 4.dp)
        contentDescription = "De garde"
    }

    /** Lance [block] puis recharge la liste s'il a reussi. */
    private fun submit(done: String, block: suspend () -> Any?) {
        lifecycleScope.launch {
            if (block() != null) {
                toast(done)
                load()
            }
        }
    }

    private fun form(vararg views: View) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(24.dp, 8.dp, 24.dp, 0)
        views.forEach { addView(it) }
    }

    private fun field(hint: String, type: Int, value: String = "") = EditText(this).apply {
        this.hint = hint
        inputType = type
        setText(value)
    }

    private fun createDialog() {
        val name = field("Nom (minuscules, sans espace)", InputType.TYPE_CLASS_TEXT)
        val password = field("Mot de passe", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        val phone = field("Téléphone (facultatif)", InputType.TYPE_CLASS_PHONE)
        val admin = CheckBox(this).apply { text = "Administrateur" }
        val test = CheckBox(this).apply { text = "Compte test (ne sonne pas)" }
        AlertDialog.Builder(this)
            .setTitle("Nouvel utilisateur")
            .setView(form(name, password, phone, admin, test))
            .setPositiveButton("Créer") { _, _ ->
                val n = name.text.toString().trim()
                val p = password.text.toString()
                if (n.isEmpty() || p.isEmpty()) {
                    toast("Nom et mot de passe obligatoires")
                    return@setPositiveButton
                }
                val request = UserCreateRequest(n, p, phone.text.toString().trim().ifEmpty { null },
                    admin.isChecked, test.isChecked)
                submit("Utilisateur $n créé") { api { registerUser(it, request) } }
            }
            .setNegativeButton("Annuler", null)
            .show()
    }

    private fun phoneDialog(u: UserResponse) {
        val phone = field("Vide = pas de téléphone", InputType.TYPE_CLASS_PHONE, u.phone_number.orEmpty())
        AlertDialog.Builder(this)
            .setTitle("Téléphone de ${u.name}")
            .setView(form(phone))
            .setPositiveButton("Enregistrer") { _, _ ->
                val request = PhoneUpdate(phone.text.toString().trim())
                submit("Téléphone enregistré") { api { updateUserPhone(it, u.id, request) } }
            }
            .setNegativeButton("Annuler", null)
            .show()
    }

    private fun confirmDelete(u: UserResponse) {
        AlertDialog.Builder(this)
            .setTitle("Supprimer ${u.name} ?")
            .setMessage("Le compte est supprimé définitivement. Ses alarmes en cours passent au premier de la chaîne.")
            .setPositiveButton("Supprimer") { _, _ ->
                submit("${u.name} supprimé") { api { deleteUser(it, u.id) } }
            }
            .setNegativeButton("Annuler", null)
            .show()
    }
}
