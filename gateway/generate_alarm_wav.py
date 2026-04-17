#!/usr/bin/env python3
"""
Genere le fichier audio WAV qui est joue pendant les appels d'alarme.

Utilise Windows SAPI (via PowerShell) pour synthetiser le message en
PCM 8kHz mono 16-bit — format exige par le SIM7600 pour l'injection
PCM pendant un appel (AT+CPCMREG=1).

Usage : python generate_alarm_wav.py
Sortie : gateway/alarm_message.wav
"""
import os
import subprocess
import sys
import tempfile
import wave

# Message joue lors de chaque appel (fixe — le detail est dans le SMS envoye avant)
ALARM_MESSAGE = (
    "Alarme critique sur le site. "
    "Consultez votre SMS. "
    "Appuyez sur 1 pour acquitter."
)

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "alarm_message.wav")


def generate_with_sapi(text: str, output_path: str) -> bool:
    """Genere un WAV 8kHz mono 16-bit via Windows SAPI (PowerShell)."""
    # SAPI genere du 22kHz/44kHz par defaut, on doit convertir en 8kHz
    # Strategie : generer haute qualite, puis resampler avec wave standard lib
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    # Escaper les quotes dans le texte
    text_escaped = text.replace("'", "''")
    tmp_path_ps = tmp_path.replace("\\", "\\\\")
    ps_script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
# Essayer de selectionner une voix francaise
foreach ($voice in $synth.GetInstalledVoices()) {{
    if ($voice.VoiceInfo.Culture.Name -like 'fr-*') {{
        $synth.SelectVoice($voice.VoiceInfo.Name)
        break
    }}
}}
$synth.Rate = -1
# Format: 8kHz mono 16-bit (directement compatible SIM7600)
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(8000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$synth.SetOutputToWaveFile('{tmp_path_ps}', $format)
$synth.Speak('{text_escaped}')
$synth.Dispose()
"""

    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", ps_script],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            err = result.stderr.decode(errors='replace').encode('ascii', errors='replace').decode()
            print(f"ERREUR PowerShell (code {result.returncode}): {err}")
            return False

        # Copier vers la destination finale
        with open(tmp_path, "rb") as src, open(output_path, "wb") as dst:
            dst.write(src.read())

        return True
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def main():
    print(f"Generation du message d'alarme...")
    print(f"Texte : {ALARM_MESSAGE}")
    print(f"Sortie : {OUTPUT_PATH}")

    if not generate_with_sapi(ALARM_MESSAGE, OUTPUT_PATH):
        print("Echec generation SAPI")
        return 1

    # Verifier le fichier genere
    with wave.open(OUTPUT_PATH, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        nframes = w.getnframes()
        duration = nframes / sr
        print(f"Fichier genere :")
        print(f"  Sample rate : {sr} Hz")
        print(f"  Channels    : {ch}")
        print(f"  Sample width: {sw * 8} bits")
        print(f"  Duration    : {duration:.2f} s")
        print(f"  Frames      : {nframes}")

    if sr != 8000 or ch != 1 or sw != 2:
        print("ATTENTION : format non conforme (8kHz mono 16-bit attendu)")
        return 2

    print("OK — fichier pret pour injection PCM sur le SIM7600")
    return 0


if __name__ == "__main__":
    sys.exit(main())
