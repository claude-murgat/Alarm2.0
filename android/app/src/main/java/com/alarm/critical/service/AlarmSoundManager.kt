package com.alarm.critical.service

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.PowerManager

class AlarmSoundManager(private val context: Context) {
    companion object {
        // Un seul lecteur pour toute l'app : le service sonne app fermee, les ecrans
        // reutilisent le meme son, et un ack depuis un ecran le coupe partout.
        private var mediaPlayer: MediaPlayer? = null
    }

    fun startAlarmSound(): Unit = synchronized(Companion) {
        if (mediaPlayer?.isPlaying == true) return

        try {
            val alarmUri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)

            mediaPlayer = MediaPlayer().apply {
                setDataSource(context, alarmUri)
                // Garde le CPU eveille pendant la lecture, telephone en veille
                setWakeMode(context.applicationContext, PowerManager.PARTIAL_WAKE_LOCK)
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build()
                )
                isLooping = true
                prepare()
                start()
            }

            val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val maxVol = audioManager.getStreamMaxVolume(AudioManager.STREAM_ALARM)
            audioManager.setStreamVolume(AudioManager.STREAM_ALARM, (maxVol * 0.5f).toInt(), 0)
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    fun stopAlarmSound(): Unit = synchronized(Companion) {
        mediaPlayer?.let {
            if (it.isPlaying) {
                it.stop()
            }
            it.release()
        }
        mediaPlayer = null
    }

    fun isPlaying(): Boolean = mediaPlayer?.isPlaying == true
}
