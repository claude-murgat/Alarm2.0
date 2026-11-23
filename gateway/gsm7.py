"""Texte d'un SMS en alphabet GSM 03.38 7 bits, un octet par caractère (INV-124, 2026-11-12).

Pour le mode texte du modem apres `AT+CMGF=1` et `AT+CSCS="GSM"`. Sans dependance : testable en
tier 1 (cf tests/unit/test_gateway_sms_gsm7.py).
"""
import unicodedata

# Table de base GSM 03.38, codes 0x00 a 0x7F. Ξ (0x1A = Ctrl-Z, fin du SMS) et ESC (0x1B, annule la
# saisie en mode texte, 3GPP TS 27.005 §3.5.1) ne sont jamais emis : table d'extension non utilisee.
_BASIC = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
          "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_CODE = {ch: bytes([i]) for i, ch in enumerate(_BASIC) if i not in (0x1A, 0x1B)}
# Hors table de base, sans equivalent par simple retrait d'accent (NFKD).
_NEAREST = {
    "ç": "Ç", "œ": "oe", "Œ": "OE", "—": "-", "–": "-", "‘": "'", "’": "'",
    "«": '"', "»": '"', "“": '"', "”": '"', "\t": " ",
    "€": "EUR", "[": "(", "{": "(", "]": ")", "}": ")", "\\": "/", "|": "/", "^": "'", "~": "-",
}


def to_gsm7(text: str) -> bytes:
    """Chaque caractere de la table de base -> son code ; tout autre -> le plus proche (accent
    retire, œ -> oe, — -> -, € -> EUR...) ; sans equivalent -> "?". Jamais 0x1A ni 0x1B."""
    out = bytearray()
    for ch in unicodedata.normalize("NFC", text):  # e + accent combinant -> é (table de base)
        if ch in _CODE:
            out += _CODE[ch]
            continue
        near = _NEAREST.get(ch) or unicodedata.normalize("NFKD", ch)
        near = "".join(c for c in near if not unicodedata.combining(c))
        out += b"".join(_CODE.get(c, b"?") for c in near) or b"?"
    return bytes(out)
