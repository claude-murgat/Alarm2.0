"""Logique pure de l'appel vocal d'alarme (INV-131 a INV-134).

Aucun import : testable en tier 1 sans pyserial (cf tests/unit/test_gateway_voice_call.py).
Les entrees sont le texte brut lu sur le port AT du SIM7600 (reponses + URC).
"""

# INV-132 : silence avant le message (le message demarrait trop tot sans, retour
# proprietaire 2026-10-14) et trames de 20 ms injectees au rythme reel.
LEAD_SILENCE_S = 0.8
FRAME_S = 0.02
# INV-133 : attente d'une touche apres la fin du message.
DTMF_WAIT_S = 30
# INV-133 : AT+CHUP raccroche les appels vocaux SIMCom ; ATH en secours.
HANG_UP_COMMANDS = ("AT+CHUP", "ATH")

_CONFIRMATIONS = {"1": "acquittee.wav", "2": "escaladee.wav"}
_RESULTS = {"1": "ack_dtmf", "2": "escalate"}


def call_answered(text: str) -> bool:
    """INV-131 : l'appel est decroche si le modem le dit actif — URC
    `VOICE CALL: BEGIN` ou `+CLCC` dont le 3e champ (stat) vaut 0. Les etats
    numerotation (2) et sonnerie (3) ne comptent pas."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("VOICE CALL: BEGIN"):
            return True
        if line.startswith("+CLCC:"):
            fields = line[len("+CLCC:"):].split(",")
            if len(fields) > 2 and fields[2].strip() == "0":
                return True
    return False


def call_ended(text: str) -> bool:
    """INV-131 : fin d'appel (raccroche, occupe)."""
    return any(m in text for m in ("NO CARRIER", "BUSY", "VOICE CALL: END"))


def dtmf_key(text: str) -> str | None:
    """INV-133 : derniere touche signalee par `+RXDTMF: <k>` ou `+DTMF: <k>`."""
    key = None
    for line in text.splitlines():
        line = line.strip()
        for prefix in ("+RXDTMF:", "+DTMF:"):
            if line.startswith(prefix) and line[len(prefix):].strip():
                key = line[len(prefix):].strip()[0]
    return key


def call_result(key: str | None) -> str:
    """INV-133 : 1 -> ack_dtmf, 2 -> escalate, sinon no_answer."""
    return _RESULTS.get(key, "no_answer")


def confirmation_file(key: str | None) -> str | None:
    """INV-133 : fichier de confirmation vocale de la touche (dans voice/)."""
    return _CONFIRMATIONS.get(key)


def is_audio_port(vid: int | None, location: str | None) -> bool:
    """INV-132 : port audio USB du SIM7600 = interface USB 4 (`...:1.4`). Sa
    description ("SimTech, Incorporated") ne contient pas le mot "audio"."""
    return vid == 0x1E0E and bool(location) and location.endswith(":1.4")


def cpcmfrm_for(sample_rate: int) -> int:
    """INV-132 : AT+CPCMFRM — 0 = 8 kHz, 1 = 16 kHz (seuls formats du port audio)."""
    if sample_rate == 8000:
        return 0
    if sample_rate == 16000:
        return 1
    raise ValueError(f"frequence non supportee par le port audio SIM7600 : {sample_rate}")


def pcm_frames(pcm: bytes, sample_rate: int) -> list[bytes]:
    """INV-132 : decoupe un PCM 16 bits mono en trames de 20 ms."""
    size = int(sample_rate * FRAME_S) * 2
    return [pcm[i:i + size] for i in range(0, len(pcm), size)]


def lead_silence(sample_rate: int) -> bytes:
    """INV-132 : PCM 16 bits nul de LEAD_SILENCE_S secondes."""
    return bytes(int(sample_rate * LEAD_SILENCE_S) * 2)


def call_deferred_for_sms(to_number: str, pending_sms: list | None) -> bool:
    """INV-134 : differer l'appel tant qu'un SMS jamais tente vise ce numero.
    Un SMS deja en echec (retries >= 1) ne retient pas l'appel : en prod (alarme 818) il
    l'a bloque ~3 min, jusqu'apres l'escalade. File illisible (None) -> ne jamais bloquer."""
    return any(s.get("to_number") == to_number and not s.get("retries")
               for s in pending_sms or [])
