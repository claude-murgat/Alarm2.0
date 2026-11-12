"""Unit test (tier 1) — INV-124 (amendé le 2026-11-12) : la passerelle envoie les SMS en alphabet
GSM 7 bits, accents compris.

Faits du 2026-11-12 : le SMS d'alarme affichait « … DC(clenchement contact sec … » au lieu de
« Déclenchement » sur le téléphone du propriétaire. La passerelle passait `AT+CMGF=1` sans
`AT+CSCS` et écrivait `body.encode()` (UTF-8 : é = C3 A9) alors que le modem attend l'alphabet GSM.
Séquence validée sur le vrai SIM7600 le même jour : `AT+CMGF=1`, `AT+CSCS="GSM"`, `AT+CMGS`, texte
en codes GSM 03.38 (un octet par caractère), Ctrl-Z.

Le vrai `gateway/modem_gateway.py` tourne ici (même chargement que test_gateway_plug_anywhere.py) ;
seul le port série est remplacé par un faux SIM7600 qui garde chaque octet écrit.
"""
import importlib.util
import logging
import pathlib
import sys
import types

import pytest

# no_mutation : ne touche pas backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

GW = pathlib.Path(__file__).resolve().parents[2] / "gateway"
_LOCAL = ("config", "locks", "modem_detect", "modem_health", "dry_contact", "voice_call", "gsm7")


def _load():
    """Charge modem_gateway.py sans laisser ses modules (noms génériques), le faux pyserial ni son
    logging.basicConfig aux autres tests."""
    root = logging.getLogger()
    root_state = (root.level, list(root.handlers))
    stubs = {}
    if importlib.util.find_spec("serial") is None:
        serial = types.ModuleType("serial")
        serial.Serial = object
        serial.SerialException = serial.SerialTimeoutException = OSError
        tools, lp = types.ModuleType("serial.tools"), types.ModuleType("serial.tools.list_ports")
        lp.comports = lambda: []
        serial.tools, tools.list_ports = tools, lp
        stubs = {"serial": serial, "serial.tools": tools, "serial.tools.list_ports": lp}
    before = set(sys.modules)
    sys.modules.update(stubs)
    sys.path.insert(0, str(GW))
    try:
        spec = importlib.util.spec_from_file_location("gateway_modem_gateway_gsm7", GW / "modem_gateway.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        root.setLevel(root_state[0])
        root.handlers[:] = root_state[1]
        sys.path.remove(str(GW))
        for name in (*_LOCAL, *stubs):
            if name not in before:
                sys.modules.pop(name, None)


mg = _load()

# Table de base GSM 03.38 (3GPP TS 23.038 §6.2.1), code 0x00 à 0x7F. 0x1B = ESC (table d'extension).
GSM_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmno"
    "pqrstuvwxyzäöñüà"
)
assert len(GSM_BASIC) == 128

ALARM_SMS = ("ALARME Izeron: Déclenchement contact sec — Izeron\n"
             "Répondez 1 pour acquitter ou https://supervision.charlesmurgat.com")
ALARM_SMS_GSM = (b"ALARME Izeron: D\x05clenchement contact sec - Izeron\n"
                 b"R\x05pondez 1 pour acquitter ou https://supervision.charlesmurgat.com")


def _decode(data: bytes) -> str:
    return "".join(GSM_BASIC[b] for b in data)


# ── to_gsm7 : table de base, remplacements ──────────────────────────────────

def test_every_basic_table_character_is_sent_with_its_code():
    codes = [c for c in range(128) if c not in (0x1A, 0x1B)]  # Ξ et ESC : voir test_character_mapping
    assert {GSM_BASIC[c]: mg.to_gsm7(GSM_BASIC[c]) for c in codes} == {GSM_BASIC[c]: bytes([c]) for c in codes}


@pytest.mark.parametrize("text, expected", [
    # accents de la table de base (validés sur le SIM7600 le 2026-11-12)
    ("é", b"\x05"), ("è", b"\x04"), ("ù", b"\x06"), ("ì", b"\x07"), ("ò", b"\x08"),
    ("à", b"\x7f"), ("É", b"\x1f"), ("Ç", b"\x09"), ("@", b"\x00"),
    ("e\u0301", b"\x05"),  # é décomposé (e + accent combinant)
    # accents absents de la table : retirés
    ("ê", b"e"), ("â", b"a"), ("î", b"i"), ("ô", b"o"), ("û", b"u"), ("ë", b"e"), ("ï", b"i"),
    ("ÿ", b"y"), ("À", b"A"), ("Â", b"A"), ("È", b"E"), ("Ê", b"E"), ("Î", b"I"), ("Ô", b"O"),
    ("Ù", b"U"), ("Û", b"U"),
    ("ç", b"\x09"), ("œ", b"oe"), ("Œ", b"OE"),
    # ponctuation typographique
    ("—", b"-"), ("–", b"-"), ("’", b"'"), ("‘", b"'"), ("«", b'"'), ("»", b'"'),
    ("“", b'"'), ("”", b'"'), (" ", b" "), (" ", b" "), ("…", b"..."), ("\t", b" "),
    # table d'extension : ESC annulerait le SMS en mode texte, remplacée
    ("€", b"EUR"), ("[", b"("), ("]", b")"), ("{", b"("), ("}", b")"), ("\\", b"/"),
    ("|", b"/"), ("^", b"'"), ("~", b"-"),
    # sans équivalent, et les octets qui finiraient (Ctrl-Z) ou annuleraient (ESC) le SMS
    ("Ξ", b"?"), ("\x1a", b"?"), ("\x1b", b"?"), ("\x00", b"?"), ("✓", b"?"), ("😀", b"?"),
])
def test_character_mapping(text, expected):
    assert mg.to_gsm7(text) == expected


@pytest.mark.parametrize("text, readable", [
    (ALARM_SMS, ALARM_SMS.replace("—", "-")),
    ("ALARME Beaufort: Déclenchement contact sec — Beaufort", "ALARME Beaufort: Déclenchement contact sec - Beaufort"),
    ("à è ù é É", "à è ù é É"),
    ("Pompe arrêtée à Izeron : débit nul, intervention requise", "Pompe arretée à Izeron : débit nul, intervention requise"),
    ("Cuve « Ça déborde » — 2 m³", "Cuve \" Ça déborde \" - 2 m3"),
    ("ALERTE ALARME MURGAT: Aucun backend ne peut ecrire (pas de leader ou tous injoignables). "
     "Intervention requise.",
     "ALERTE ALARME MURGAT: Aucun backend ne peut ecrire (pas de leader ou tous injoignables). "
     "Intervention requise."),
])
def test_french_alarm_texts_read_back_on_the_phone(text, readable):
    """Le téléphone décode les octets avec la table de base : il affiche `readable`."""
    assert _decode(mg.to_gsm7(text)) == readable


def test_alarm_sms_is_one_byte_per_character_within_160():
    assert mg.to_gsm7(ALARM_SMS) == ALARM_SMS_GSM
    assert len(ALARM_SMS_GSM) == len(ALARM_SMS) <= 160


def test_never_emits_ctrl_z_or_esc_nor_8_bit_bytes():
    text = "".join(chr(c) for c in range(0x3000)) + "€😀\U0001F4A9"
    out = mg.to_gsm7(text)
    assert b"\x1a" not in out and b"\x1b" not in out
    assert max(out) < 0x80


# ── Octets écrits sur le port série ─────────────────────────────────────────

class FakeSim7600:
    """Faux port série : garde chaque écriture, répond comme le SIM7600 (OK, invite « > »)."""

    def __init__(self, refuse=()):
        self.written = []
        self.refuse = refuse  # commandes AT qui répondent ERROR
        self._out = b""

    def write(self, data):
        data = bytes(data)
        self.written.append(data)
        if data.endswith(b"\x1a"):
            self._out += b"\r\n+CMGS: 12\r\n\r\nOK\r\n"
        elif data.startswith(b"AT+CMGS="):
            self._out += b"\r\n> "
        elif data.startswith(b"AT"):
            self._out += b"\r\nERROR\r\n" if data.strip().decode() in self.refuse else b"\r\nOK\r\n"
        return len(data)

    @property
    def in_waiting(self):
        return len(self._out)

    def read(self, n=1):
        data, self._out = self._out[:n], self._out[n:]
        return data

    def readline(self):
        i = self._out.find(b"\n") + 1 or len(self._out)
        line, self._out = self._out[:i], self._out[i:]
        return line

    def reset_input_buffer(self):
        self._out = b""


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(mg.time, "sleep", lambda s: None)


def test_alarm_sms_is_written_in_gsm_charset():
    modem = FakeSim7600()
    result = mg.SmsSenderThread(modem)._send_sms("+33600000001", ALARM_SMS)
    assert modem.written == [
        b"AT+CMGF=1\r\n",
        b'AT+CSCS="GSM"\r\n',
        b'AT+CMGS="+33600000001"\r',
        ALARM_SMS_GSM + b"\x1a",
    ]
    assert result == (True, "")


def test_charset_refused_fails_without_sending():
    modem = FakeSim7600(refuse=('AT+CSCS="GSM"',))
    assert mg.SmsSenderThread(modem)._send_sms("+33600000001", ALARM_SMS) == (False, "CSCS_FAILED")
    assert not any(w.startswith(b"AT+CMGS") for w in modem.written)


def test_backend_alert_sms_is_written_in_gsm_charset(monkeypatch):
    monkeypatch.setattr(mg, "ALERT_RECIPIENTS", ["+33600000002"])
    modem = FakeSim7600()
    mg.HealthMonitorThread(modem)._send_alert_sms()
    assert modem.written == [
        b"AT+CMGF=1\r\n",
        b'AT+CSCS="GSM"\r\n',
        b'AT+CMGS="+33600000002"\r',
        b"ALERTE ALARME MURGAT: Aucun backend ne peut ecrire (pas de leader ou tous injoignables). "
        b"Intervention requise.\x1a",
    ]
