"""Unit test (tier 1) — appel vocal d'alarme de la gateway (`gateway/voice_call.py`).

INV-131 / INV-132 / INV-133. Regression des tests reels du 2026-10-14 sur
alarm-1 (SIM7600E-H, firmware LE20B04SIM7600M22, SIM Orange) :
  - `",0,0," in clcc` prenait la numerotation (`+CLCC: 3,0,2,0,0,...`) pour
    un decroche -> message joue dans le vide, resultat `no_answer` alors que
    le proprietaire avait decroche ;
  - `AT+CDTAM` a 0 -> le TTS du module restait local, rien au bout du fil ;
  - le port audio USB n'etait jamais trouve (description "SimTech,
    Incorporated", sans le mot "audio").

Charge uniquement `gateway/voice_call.py` (aucun import) -> pas de dependance
pyserial/numpy en tier 1, meme pattern que test_gateway_modem_health.py.
"""
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_PATH = pathlib.Path(__file__).resolve().parents[2] / "gateway" / "voice_call.py"
_spec = importlib.util.spec_from_file_location("gateway_voice_call", _PATH)
_vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vc)

NUM = '"+33695024008",145'


class TestCallAnswered:
    """INV-131 : decroche seulement si le modem dit l'appel actif."""

    @pytest.mark.parametrize("text", [
        f"+CLCC: 3,0,0,0,0,{NUM}\nOK",
        "VOICE CALL: BEGIN",
        f"+CLCC: 3,0,3,0,0,{NUM}\nOK\nVOICE CALL: BEGIN",  # URC arrive apres une sonnerie
        f"+CLCC: 1,0,0,0,0,{NUM}",
    ])
    def test_active_call_is_answered(self, text):
        assert _vc.call_answered(text) is True

    @pytest.mark.parametrize("text", [
        f"+CLCC: 3,0,2,0,0,{NUM}\nOK",  # numerotation : contient ",0,0," (bug du 2026-10-14)
        f"+CLCC: 3,0,3,0,0,{NUM}\nOK",  # sonnerie
        "OK",
        "",
    ])
    def test_dialing_or_ringing_is_not_answered(self, text):
        assert _vc.call_answered(text) is False


class TestCallEnded:
    """INV-131 : fin d'appel avant (ou pendant) la conversation."""

    @pytest.mark.parametrize("text", [
        "NO CARRIER", "BUSY", "VOICE CALL: END: 000027\n\nNO CARRIER",
    ])
    def test_end_markers(self, text):
        assert _vc.call_ended(text) is True

    @pytest.mark.parametrize("text", [
        f"+CLCC: 3,0,3,0,0,{NUM}\nOK", "VOICE CALL: BEGIN", "",
    ])
    def test_ongoing_call_is_not_ended(self, text):
        assert _vc.call_ended(text) is False


class TestDtmfKey:
    """INV-133 : touche lue sur les URC du port AT."""

    @pytest.mark.parametrize("text,key", [
        ("+RXDTMF: 1", "1"),
        ("+RXDTMF:2", "2"),
        ("+DTMF: 1", "1"),
        ("OK\n+RXDTMF: 1\n+RXDTMF: 2", "2"),  # la derniere touche fait foi
    ])
    def test_key_parsed(self, text, key):
        assert _vc.dtmf_key(text) == key

    @pytest.mark.parametrize("text", ["", "OK", "VOICE CALL: BEGIN"])
    def test_no_key(self, text):
        assert _vc.dtmf_key(text) is None


class TestCallResult:
    """INV-133 : touche -> resultat renvoye au backend."""

    def test_one_acknowledges(self):
        assert _vc.call_result("1") == "ack_dtmf"

    def test_two_escalates(self):
        assert _vc.call_result("2") == "escalate"

    @pytest.mark.parametrize("key", [None, "3", "#", "*"])
    def test_anything_else_is_no_answer(self, key):
        assert _vc.call_result(key) == "no_answer"


class TestConfirmation:
    """INV-133 : confirmation vocale de la touche."""

    def test_one_confirms_acknowledgement(self):
        assert _vc.confirmation_file("1") == "acquittee.wav"

    def test_two_confirms_escalation(self):
        assert _vc.confirmation_file("2") == "escaladee.wav"

    @pytest.mark.parametrize("key", [None, "3"])
    def test_no_confirmation_otherwise(self, key):
        assert _vc.confirmation_file(key) is None

    def test_wait_for_key_after_message(self):
        assert _vc.DTMF_WAIT_S == 30


class TestAudioPort:
    """INV-132 : port audio USB du SIM7600 = interface USB 4."""

    def test_interface_4_of_simcom_is_audio(self):
        assert _vc.is_audio_port(0x1E0E, "3-9:1.4") is True

    @pytest.mark.parametrize("vid,location", [
        (0x1E0E, "3-9:1.3"),   # port AT
        (0x1E0E, "3-9:1.2"),
        (0x10C4, "3-9:1.4"),   # CP2102 (prise "USB TO UART" de la carte Waveshare)
        (None, None),          # /dev/ttyS*
    ])
    def test_other_ports_are_not_audio(self, vid, location):
        assert _vc.is_audio_port(vid, location) is False


class TestPcmFrames:
    """INV-132 : injection au rythme reel, trames de 20 ms."""

    @pytest.mark.parametrize("rate,frm", [(8000, 0), (16000, 1)])
    def test_cpcmfrm_matches_sample_rate(self, rate, frm):
        assert _vc.cpcmfrm_for(rate) == frm

    def test_unsupported_rate_rejected(self):
        with pytest.raises(ValueError):
            _vc.cpcmfrm_for(22050)

    @pytest.mark.parametrize("rate,frame_bytes", [(8000, 320), (16000, 640)])
    def test_frames_are_20ms(self, rate, frame_bytes):
        pcm = bytes(frame_bytes * 3 + 10)
        frames = _vc.pcm_frames(pcm, rate)
        assert [len(f) for f in frames] == [frame_bytes] * 3 + [10]
        assert b"".join(frames) == pcm


class TestLeadSilence:
    """INV-132 : 0,8 s de silence avant le message (retour proprietaire 2026-10-14)."""

    def test_lead_silence_duration(self):
        assert _vc.LEAD_SILENCE_S == 0.8

    @pytest.mark.parametrize("rate", [8000, 16000])
    def test_lead_silence_pcm(self, rate):
        pcm = _vc.lead_silence(rate)
        assert len(pcm) == int(rate * 0.8) * 2
        assert set(pcm) == {0}


class TestHangUp:
    """INV-133 : raccrochage des appels vocaux SIMCom, ATH en secours."""

    def test_hang_up_commands(self):
        assert _vc.HANG_UP_COMMANDS == ("AT+CHUP", "ATH")


class TestCallDeferredForSms:
    """INV-134 : le SMS de detail part avant l'appel vers le meme numero."""

    def test_pending_sms_for_same_number_defers_call(self):
        pending = [{"id": 7, "to_number": "+33695024008", "body": "ALARME ..."}]
        assert _vc.call_deferred_for_sms("+33695024008", pending) is True

    def test_sms_for_other_number_does_not_defer(self):
        pending = [{"id": 7, "to_number": "+33600000001", "body": "ALARME ..."}]
        assert _vc.call_deferred_for_sms("+33695024008", pending) is False

    @pytest.mark.parametrize("pending", [[], None])
    def test_no_pending_sms_or_unreadable_queue_never_blocks(self, pending):
        assert _vc.call_deferred_for_sms("+33695024008", pending) is False

    @pytest.mark.parametrize("retries", [1, 2])
    def test_failing_sms_never_blocks_the_call(self, retries):
        # Prod 2026-10-20 (alarme 818) : SMS en AT_ERROR, appel differe ~3 min -> parti apres l'escalade.
        pending = [{"id": 599, "to_number": "+33695024008", "body": "ALARME ...", "retries": retries}]
        assert _vc.call_deferred_for_sms("+33695024008", pending) is False

    def test_untried_sms_still_defers(self):
        pending = [{"id": 599, "to_number": "+33695024008", "body": "ALARME ...", "retries": 0}]
        assert _vc.call_deferred_for_sms("+33695024008", pending) is True
