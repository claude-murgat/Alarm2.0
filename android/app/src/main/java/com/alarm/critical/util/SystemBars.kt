package com.alarm.critical.util

import android.view.View
import androidx.core.graphics.Insets
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

/**
 * INV-ANDROID-708 : depuis Android 15, une app qui cible 35 ou plus est dessinee sous les barres systeme.
 * La vue s'en ecarte par sa marge interieure, ajoutee a la sienne ; son fond passe sous les barres.
 * Sur Android 14 et avant, Android a deja consomme ces encarts : la marge ne change pas.
 */
fun View.padForSystemBars() {
    val own = Insets.of(paddingLeft, paddingTop, paddingRight, paddingBottom)
    ViewCompat.setOnApplyWindowInsetsListener(this) { v, insets ->
        val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
        val p = Insets.add(own, bars)
        v.setPadding(p.left, p.top, p.right, p.bottom)
        WindowInsetsCompat.CONSUMED
    }
}
