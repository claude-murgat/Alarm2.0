#!/usr/bin/env python3
"""
Decodeur DTMF par algorithme de Goertzel — Alarm 2.0

Detecte les touches DTMF (0-9, *, #) dans un flux audio PCM 8kHz 16-bit mono.
Utilise quand AT+DDET n'est pas supporte par le modem (cas du SIM7600E-H).

L'audio est lu depuis le port USB audio du modem (COM6 / /dev/sim7600_audio).
Le decodeur est purement algorithmique et testable sans hardware.
"""
import math
import numpy as np

# Frequences DTMF standard (Hz)
DTMF_FREQS_LOW = [697, 770, 852, 941]
DTMF_FREQS_HIGH = [1209, 1336, 1477]

DTMF_MAP = {
    (697, 1209): "1", (697, 1336): "2", (697, 1477): "3",
    (770, 1209): "4", (770, 1336): "5", (770, 1477): "6",
    (852, 1209): "7", (852, 1336): "8", (852, 1477): "9",
    (941, 1209): "*", (941, 1336): "0", (941, 1477): "#",
}

# Parametres par defaut
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_BLOCK_SIZE = 205        # ~25.6ms a 8kHz
DEFAULT_THRESHOLD = 100.0       # Seuil d'energie minimum pour detection
DEFAULT_MIN_CONSECUTIVE = 3     # Blocs consecutifs minimum pour valider


def goertzel_magnitude(samples: np.ndarray, target_freq: float, sample_rate: int) -> float:
    """Calcule la magnitude de Goertzel pour une frequence cible.

    L'algorithme de Goertzel est un filtre IIR efficace qui calcule
    une seule composante frequentielle (comme une DFT a 1 bin).
    Complexite O(N) au lieu de O(N log N) pour une FFT complete.
    """
    n = len(samples)
    if n == 0:
        return 0.0

    # Normaliser les echantillons en float
    normalized = samples.astype(np.float64) / 32768.0

    # Coefficient de Goertzel
    k = round(n * target_freq / sample_rate)
    omega = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(omega)

    # Iteration du filtre
    s0, s1, s2 = 0.0, 0.0, 0.0
    for sample in normalized:
        s0 = sample + coeff * s1 - s2
        s2 = s1
        s1 = s0

    # Magnitude au carre (pas besoin de la racine pour la comparaison)
    magnitude = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return magnitude


class DtmfDecoder:
    """Decodeur DTMF par algorithme de Goertzel avec anti-bounce."""

    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        block_size: int = DEFAULT_BLOCK_SIZE,
        threshold: float = DEFAULT_THRESHOLD,
        min_consecutive: int = DEFAULT_MIN_CONSECUTIVE,
    ):
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.threshold = threshold
        self.min_consecutive = min_consecutive

        # Etat anti-bounce
        self._last_key = None
        self._consecutive_count = 0
        self._last_validated = None

    def reset(self):
        """Reinitialise l'etat du decodeur."""
        self._last_key = None
        self._consecutive_count = 0
        self._last_validated = None

    def _detect_block(self, block: np.ndarray) -> str | None:
        """Detecte une touche DTMF dans un bloc d'echantillons.
        Retourne le caractere DTMF ou None."""
        # Calculer les magnitudes pour les frequences basses
        low_mags = []
        for freq in DTMF_FREQS_LOW:
            mag = goertzel_magnitude(block, freq, self.sample_rate)
            low_mags.append((freq, mag))

        # Calculer les magnitudes pour les frequences hautes
        high_mags = []
        for freq in DTMF_FREQS_HIGH:
            mag = goertzel_magnitude(block, freq, self.sample_rate)
            high_mags.append((freq, mag))

        # Trouver la frequence dominante dans chaque groupe
        best_low = max(low_mags, key=lambda x: x[1])
        best_high = max(high_mags, key=lambda x: x[1])

        # Verifier que les deux sont au-dessus du seuil
        if best_low[1] < self.threshold or best_high[1] < self.threshold:
            return None

        # Verifier que la frequence dominante est significativement plus forte
        # que les autres (rapport > 2x) pour eviter les faux positifs
        for freq, mag in low_mags:
            if freq != best_low[0] and mag > best_low[1] * 0.5:
                return None  # Pas assez de separation
        for freq, mag in high_mags:
            if freq != best_high[0] and mag > best_high[1] * 0.5:
                return None

        # Mapper vers la touche DTMF
        key = DTMF_MAP.get((best_low[0], best_high[0]))
        return key

    def detect(self, samples: np.ndarray) -> str | None:
        """Detecte une touche DTMF dans un buffer d'echantillons.
        Retourne le premier caractere DTMF detecte, ou None.
        Applique l'anti-bounce (min_consecutive blocs identiques)."""
        offset = 0
        while offset + self.block_size <= len(samples):
            block = samples[offset:offset + self.block_size]
            key = self._detect_block(block)

            if key is not None:
                if key == self._last_key:
                    self._consecutive_count += 1
                else:
                    self._last_key = key
                    self._consecutive_count = 1

                if self._consecutive_count >= self.min_consecutive:
                    if key != self._last_validated:
                        self._last_validated = key
                        return key
            else:
                # Pas de detection → reset du compteur
                if self._consecutive_count > 0:
                    self._last_key = None
                    self._consecutive_count = 0
                    self._last_validated = None

            offset += self.block_size

        return None

    def detect_stream(self, samples: np.ndarray) -> list[str]:
        """Detecte toutes les touches DTMF dans un flux audio.
        Retourne la liste ordonnee des touches detectees (avec anti-bounce)."""
        events = []
        offset = 0

        while offset + self.block_size <= len(samples):
            block = samples[offset:offset + self.block_size]
            key = self._detect_block(block)

            if key is not None:
                if key == self._last_key:
                    self._consecutive_count += 1
                else:
                    self._last_key = key
                    self._consecutive_count = 1

                if self._consecutive_count >= self.min_consecutive:
                    if key != self._last_validated:
                        self._last_validated = key
                        events.append(key)
            else:
                if self._consecutive_count > 0:
                    self._last_key = None
                    self._consecutive_count = 0
                    self._last_validated = None

            offset += self.block_size

        return events
