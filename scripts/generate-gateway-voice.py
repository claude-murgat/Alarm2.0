#!/usr/bin/env python3
"""Genere les messages vocaux de la gateway (INV-132/133/159) -> gateway/voice/*.wav.

Voix validee par le proprietaire le 2026-10-14 (appels reels sur alarm-1) :
Piper `fr_FR-upmc-medium`, locutrice `jessica`, debit 1.0, traitement pour la
ligne telephonique, PCM 16 bits mono 16 kHz (appel VoLTE). Le silence avant le
message est ajoute par la gateway (LEAD_SILENCE_S), pas ici.

Piper n'est pas deterministe : chaque execution donne une autre prise de chaque message
(longueur differente). Ne commiter que les fichiers nouveaux ou voulus (git checkout des
autres) : les prises deja validees au telephone restent celles du depot.

Prerequis (poste de dev, pas les noeuds) :
  python3 -m venv /tmp/piper && /tmp/piper/bin/pip install piper-tts numpy scipy
  /tmp/piper/bin/python scripts/generate-gateway-voice.py
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import urllib.request
import wave

import numpy as np
from scipy.signal import butter, lfilter, resample_poly, sosfilt

# Message court (demande du proprietaire du 2026-10-30) : le site, puis les touches.
# Le detail est dans le SMS, qui precede l'appel (INV-134).
ALARME = "Alarme {}. Appuyez sur 1 pour acquitter, 2 pour escalader."
MESSAGES = {
    "alarme.wav": ALARME.format("Murgat"),  # alarme sans site (astreinte)
    # INV-159 : le message annonce le site de l'alarme
    "alarme_izeron.wav": ALARME.format("Izeron"),
    "alarme_beaufort.wav": ALARME.format("Beaufort"),
    "acquittee.wav": "Alarme acquittée. Merci.",
    "escaladee.wav": "Alarme escaladée au prochain intervenant.",
}
MODEL = "fr_FR-upmc-medium"
MODEL_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/fr/fr_FR/upmc/medium/"
SPEAKER = 0  # jessica
RATE = 16000
OUT_DIR = pathlib.Path(__file__).resolve().parents[1] / "gateway" / "voice"
CACHE = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "piper"


def model_path() -> pathlib.Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    for ext in (".onnx", ".onnx.json"):
        dest = CACHE / (MODEL + ext)
        if not dest.exists():
            urllib.request.urlretrieve(MODEL_URL + MODEL + ext, dest)
    return CACHE / (MODEL + ".onnx")


def synthesize(text: str, model: pathlib.Path) -> tuple[np.ndarray, int]:
    piper = pathlib.Path(sys.executable).with_name("piper")
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        subprocess.run(
            [str(piper), "-m", str(model), "-s", str(SPEAKER), "--length-scale", "1.0",
             "--sentence-silence", "0.3", "-f", tmp.name],
            input=text.encode(), check=True, capture_output=True,
        )
        with wave.open(tmp.name) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), np.int16) / 32768.0
            return x, w.getframerate()


def for_phone(x: np.ndarray, fs: int) -> np.ndarray:
    """Passe-haut 120 Hz, presence +3 dB a 3 kHz, compression douce, 16 kHz, crete -6 dBFS."""
    x = sosfilt(butter(4, 120, "highpass", fs=fs, output="sos"), x)
    a_ = 10 ** (3.0 / 40); w0 = 2 * np.pi * 3000 / fs; al = np.sin(w0) / (2 * 0.8)
    b = np.array([1 + al * a_, -2 * np.cos(w0), 1 - al * a_]); a = np.array([1 + al / a_, -2 * np.cos(w0), 1 - al / a_])
    x = lfilter(b / a[0], a / a[0], x)
    x = x / max(1e-9, np.abs(x).max())
    k = np.exp(-1 / (0.010 * fs))
    env = np.sqrt(lfilter([1 - k], [1, -k], x ** 2)) + 1e-9
    lvl = 20 * np.log10(env)
    x = x * 10 ** (np.where(lvl > -20, (-20 - lvl) * (1 - 1 / 3.0), 0.0) / 20)
    y = resample_poly(x, RATE, fs)
    y = np.concatenate([y, np.zeros(int(RATE * 0.2))])
    return y / np.abs(y).max() * 10 ** (-6 / 20)


def main():
    model = model_path()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in MESSAGES.items():
        y = for_phone(*synthesize(text, model))
        with wave.open(str(OUT_DIR / name), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes((y * 32767).astype(np.int16).tobytes())
        print(f"{OUT_DIR / name} : {len(y) / RATE:.1f} s")


if __name__ == "__main__":
    main()
