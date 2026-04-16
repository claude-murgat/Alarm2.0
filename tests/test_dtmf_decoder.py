"""
Tests du decodeur DTMF (algorithme de Goertzel).

Tests purement algorithmiques — pas besoin de backend Docker.
Genere des signaux PCM synthetiques et verifie la detection.

Lancer avec : python -m pytest tests/test_dtmf_decoder.py -v
"""

import numpy as np
import pytest
import sys
import os

# Ajouter le dossier gateway au path pour importer dtmf_decoder
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateway"))

from dtmf_decoder import DtmfDecoder

SAMPLE_RATE = 8000  # PCM 8kHz mono (format audio USB du SIM7600)

# Frequences DTMF standard
DTMF_FREQS = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477),
}


def _generate_dtmf_tone(key: str, duration_ms: int = 200, amplitude: float = 0.5) -> np.ndarray:
    """Genere un signal PCM pour une touche DTMF."""
    f1, f2 = DTMF_FREQS[key]
    t = np.arange(int(SAMPLE_RATE * duration_ms / 1000)) / SAMPLE_RATE
    signal = amplitude * (np.sin(2 * np.pi * f1 * t) + np.sin(2 * np.pi * f2 * t))
    return (signal * 32767).astype(np.int16)


def _generate_silence(duration_ms: int = 200) -> np.ndarray:
    """Genere un buffer de silence."""
    return np.zeros(int(SAMPLE_RATE * duration_ms / 1000), dtype=np.int16)


class TestGoertzel:
    """Tests du decodeur DTMF par algorithme de Goertzel."""

    def setup_method(self):
        self.decoder = DtmfDecoder(sample_rate=SAMPLE_RATE)

    def test_detect_each_digit(self):
        """Detection de chaque touche DTMF (0-9, *, #)."""
        for key in DTMF_FREQS:
            tone = _generate_dtmf_tone(key, duration_ms=200)
            detected = self.decoder.detect(tone)
            assert detected == key, f"Touche {key} non detectee (got {detected})"
            self.decoder.reset()

    def test_no_detection_on_silence(self):
        """Pas de detection sur un signal silencieux."""
        silence = _generate_silence(duration_ms=200)
        detected = self.decoder.detect(silence)
        assert detected is None, f"Pas de detection attendue sur silence, got {detected}"

    def test_no_detection_on_noise(self):
        """Pas de detection sur du bruit aleatoire."""
        rng = np.random.default_rng(42)
        noise = (rng.standard_normal(SAMPLE_RATE // 5) * 1000).astype(np.int16)
        detected = self.decoder.detect(noise)
        assert detected is None, f"Pas de detection attendue sur bruit, got {detected}"

    def test_no_detection_on_single_freq(self):
        """Pas de detection sur une seule frequence (pas une paire DTMF)."""
        t = np.arange(SAMPLE_RATE // 5) / SAMPLE_RATE
        signal = (0.5 * np.sin(2 * np.pi * 697 * t) * 32767).astype(np.int16)
        detected = self.decoder.detect(signal)
        assert detected is None, f"Pas de detection attendue sur frequence unique, got {detected}"

    def test_anti_bounce_single_event(self):
        """10 blocs consecutifs de la meme touche ne produisent qu'un seul evenement."""
        # Generer 10 blocs de la touche "1"
        tone = _generate_dtmf_tone("1", duration_ms=500)
        events = self.decoder.detect_stream(tone)
        assert len(events) == 1, f"1 seul evenement attendu (anti-bounce), got {len(events)}"
        assert events[0] == "1"

    def test_two_consecutive_keys(self):
        """Deux touches separees par du silence donnent 2 evenements."""
        tone1 = _generate_dtmf_tone("1", duration_ms=200)
        silence = _generate_silence(duration_ms=200)
        tone2 = _generate_dtmf_tone("2", duration_ms=200)
        signal = np.concatenate([tone1, silence, tone2])
        events = self.decoder.detect_stream(signal)
        assert events == ["1", "2"], f"Attendu ['1', '2'], got {events}"
