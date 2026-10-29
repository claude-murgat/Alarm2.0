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


class TestCallRang:
    """INV-148 : `no_answer` seulement si le telephone a sonne (`+CLCC` stat 3 alerte, ou
    0 actif). Appel perdu sur le reseau avant la sonnerie -> `error`, rejoue (INV-144).
    Alarme 888 du 2026-10-22 : ATD accepte, signal 3G CSQ 3, le telephone n'a jamais
    sonne, appel enregistre `no_answer` -> jamais rejoue."""

    def test_alerting_means_rang(self):
        assert _vc.call_rang('+CLCC: 1,0,3,0,0,"+33695024008",145\r\nOK') is True

    def test_active_means_rang(self):
        assert _vc.call_rang('+CLCC: 1,0,0,0,0,"+33695024008",145') is True

    def test_dialing_only_never_rang(self):
        assert _vc.call_rang('+CLCC: 1,0,2,0,0,"+33695024008",145\r\nOK\r\nNO CARRIER') is False

    def test_nothing_never_rang(self):
        assert _vc.call_rang("") is False


class TestSiteVoice:
    """INV-159 : le message de Jessica annonce le site de l'alarme (meme personne de garde
    pour Izeron et Beaufort). Alarme sans site -> `alarme.wav`."""

    LOADED = {"alarme.wav", "alarme_izeron.wav", "alarme_beaufort.wav", "acquittee.wav", "escaladee.wav"}

    @pytest.mark.parametrize("site,name", [
        ("izeron", "alarme_izeron.wav"),
        ("beaufort", "alarme_beaufort.wav"),
        (None, "alarme.wav"),   # alarme d'astreinte
        ("", "alarme.wav"),
        ("lyon", "alarme.wav"),  # site sans message enregistre
    ])
    def test_message_of_the_alarm_site(self, site, name):
        assert _vc.alarm_voice_file(site, self.LOADED) == name

    def test_voice_dir_without_site_messages_falls_back(self):
        # Voix d'avant INV-159 encore en place (deploiement en cours) : alarme.wav, pas de KeyError.
        assert _vc.alarm_voice_file("izeron", {"alarme.wav", "acquittee.wav", "escaladee.wav"}) == "alarme.wav"

    @pytest.mark.parametrize("site,text", [
        ("izeron", "Site Izeron. Alarme critique: Pompe."),
        ("beaufort", "Site Beaufort. Alarme critique: Pompe."),
        (None, "Alarme critique: Pompe."),
        ("", "Alarme critique: Pompe."),
    ])
    def test_tts_fallback_names_the_site(self, site, text):
        assert _vc.tts_text(site, "Alarme critique: Pompe.") == text

    @pytest.mark.parametrize("name", ["alarme_izeron.wav", "alarme_beaufort.wav"])
    def test_site_messages_shipped_in_the_format_of_alarme_wav(self, name):
        # Une frequence differente ferait tomber toute la voix en repli TTS (_load_voice).
        import wave
        voice = _PATH.parent / "voice"
        with wave.open(str(voice / "alarme.wav")) as ref, wave.open(str(voice / name)) as w:
            fmt = (w.getframerate(), w.getsampwidth(), w.getnchannels())
            assert fmt == (ref.getframerate(), ref.getsampwidth(), ref.getnchannels())
            assert w.getnframes() > ref.getnframes() * 0.8  # le message entier, pas un fragment


# --- INV-169 : canal audio PCM bloque (incident onsite-2 Izeron, 2026-10-20 -> 2026-10-29) ---

REG_1 = "+CPCMREG: 1\nOK"
REG_0 = "+CPCMREG: 0\nOK"


class TestPcmWedged:
    """INV-169 : bloque = CPCMREG a 1 hors appel, et AT+CPCMREG=0 refuse (ERROR) ou sans
    effet (toujours 1). Sur onsite-2, tous les appels decroches etaient muets ; le modem
    de Beaufort (meme modele et firmware) repond 0 hors appel."""

    def test_onsite2_incident_is_wedged(self):
        assert _vc.pcm_wedged(REG_1, "ERROR", REG_1) is True

    def test_close_accepted_but_still_1_is_wedged(self):
        assert _vc.pcm_wedged(REG_1, "OK", REG_1) is True

    @pytest.mark.parametrize("close", ["ERROR", "+CME ERROR: 3"])
    def test_close_refused_without_reread_is_wedged(self, close):
        assert _vc.pcm_wedged(REG_1, close, "") is True

    def test_reply_without_space_is_read(self):
        assert _vc.pcm_wedged("+CPCMREG:1\nOK", "ERROR", "+CPCMREG:1\nOK") is True

    @pytest.mark.parametrize("close", ["OK", "ERROR"])
    def test_back_to_0_is_not_wedged(self, close):
        assert _vc.pcm_wedged(REG_1, close, REG_0) is False

    def test_healthy_modem_is_not_wedged(self):
        # Beaufort : 0 hors appel, rien a fermer.
        assert _vc.pcm_wedged(REG_0, "", "") is False

    @pytest.mark.parametrize("query", ["", "ERROR"])
    def test_no_evidence_is_not_wedged(self, query):
        # Port muet (INV-126) ou modem pas pret : jamais de redemarrage sur un doute.
        assert _vc.pcm_wedged(query, "", "") is False

    def test_close_accepted_without_reread_is_not_wedged(self):
        assert _vc.pcm_wedged(REG_1, "OK", "") is False

    @pytest.mark.parametrize("text,registered", [
        (REG_1, True), ("+CPCMREG:1", True), (REG_0, False), ("ERROR", False), ("", False),
    ])
    def test_pcm_registered(self, text, registered):
        # Decide s'il faut tenter AT+CPCMREG=0 (avant l'appel et au controle hors appel).
        assert _vc.pcm_registered(text) is registered


class TestVoiceFallback:
    """INV-169 : l'injection PCM s'arrete en cours de message (Write timeout) alors que
    l'appel continue -> synthese du module, l'appele entend au moins quelque chose.
    Avant : repli seulement si la voix n'avait jamais demarre -> silence total."""

    def test_pcm_stopped_while_call_up_falls_back(self):
        assert _vc.voice_fallback_due("VOICE CALL: BEGIN\n+CPCMREG: 1\nOK\n") is True

    @pytest.mark.parametrize("urc", [
        "VOICE CALL: BEGIN\nVOICE CALL: END: 000012\nNO CARRIER\n",
        "VOICE CALL: BEGIN\nNO CARRIER\n",
    ])
    def test_callee_hung_up_no_fallback(self, urc):
        assert _vc.voice_fallback_due(urc) is False

    def test_key_already_received_no_fallback(self):
        assert _vc.voice_fallback_due("VOICE CALL: BEGIN\n+RXDTMF: 1\n") is False
