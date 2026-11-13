"""Unit test (tier 1) — logique pure du contact sec (`gateway/dry_contact.py`).

Couvre le parsing des lignes du firmware hôte (`DC:<0|1>`) et le mapping
raw→état métier, partagés par les moniteurs modem et hôte de la gateway.

Test pur : charge uniquement `gateway/dry_contact.py` (stdlib only) par chemin,
donc aucune dépendance lourde (pyserial/numpy) tirée dans le tier 1.
"""
import hashlib
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_PATH = pathlib.Path(__file__).resolve().parents[2] / "gateway" / "dry_contact.py"
_spec = importlib.util.spec_from_file_location("gateway_dry_contact", _PATH)
_dc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dc)


class TestParseDcLine:
    def test_dc0(self):
        assert _dc.parse_dc_line("DC:0") == 0

    def test_dc1(self):
        assert _dc.parse_dc_line("DC:1") == 1

    def test_trailing_crlf_and_spaces(self):
        assert _dc.parse_dc_line("DC:0\r\n") == 0
        assert _dc.parse_dc_line("  DC:1  ") == 1

    @pytest.mark.parametrize("junk", [
        "", "  ", "OK", "+CGGETV: 43,1", "DC:", "DC:2", "DC:x",
        "garbage", "DC", "0", "1", "\x00DC:1",
    ])
    def test_noise_returns_none(self, junk):
        assert _dc.parse_dc_line(junk) is None

    def test_partial_line_returns_none(self):
        # ligne tronquee (lecture serie partielle)
        assert _dc.parse_dc_line("DC") is None
        assert _dc.parse_dc_line("D") is None


class TestRawToState:
    def test_host_mapping_normal_value_0(self):
        # Arduino INPUT_PULLUP : ferme=0=normal, ouvert/coupe=1=alarme
        assert _dc.raw_to_state(0, normal_value=0) == "closed"
        assert _dc.raw_to_state(1, normal_value=0) == "open"

    def test_modem_mapping_normal_value_1(self):
        # SIM7600 : ferme=1=normal, ouvert=0=alarme
        assert _dc.raw_to_state(1, normal_value=1) == "closed"
        assert _dc.raw_to_state(0, normal_value=1) == "open"

    def test_anything_not_normal_is_open(self):
        # garde-fou fail-to-alarm : tout sample != normal_value → "open"
        assert _dc.raw_to_state(0, normal_value=1) == "open"
        assert _dc.raw_to_state(1, normal_value=0) == "open"


class TestDecideReport:
    """INV-122 — decision pure 'POSTer un etat OU rester silencieux' cote
    gateway hote. Realise la primitive 'backend voit le silence du uC comme
    intentionnel' qui sous-tend l'agregation OR fail-to-alarm : une gateway
    qui ne pousse rien ne fausse pas la decision (elle est ignoree dans
    `alive_gateways`), tandis qu'un etat 'open' meme isole declenche.

    Avant extraction, cette decision vivait dans
    `HostDryContactMonitorThread._serve` (gateway/modem_gateway.py) et
    n'etait testee qu'indirectement par l'integration tier 2 — un trou
    structurel identifie sur la PR #187.
    """

    LIVENESS = 5.0
    NORMAL = 0  # cablage hote (Arduino INPUT_PULLUP)
    NOW = 1000.0

    def test_fresh_normal_returns_closed(self):
        # (a) uC vient de POSTer raw=normal_value → POST 'closed'
        assert _dc.decide_report(
            latest_raw=0, latest_ts=self.NOW - 1.0,
            now=self.NOW, liveness=self.LIVENESS, normal_value=self.NORMAL,
        ) == "closed"

    def test_fresh_abnormal_returns_open(self):
        # (b) uC vient de POSTer raw != normal_value → POST 'open' (fil coupe)
        assert _dc.decide_report(
            latest_raw=1, latest_ts=self.NOW - 1.0,
            now=self.NOW, liveness=self.LIVENESS, normal_value=self.NORMAL,
        ) == "open"

    def test_stale_returns_none(self):
        # (c) uC muet au-dela de liveness → None (ne pas POSTer)
        # INV-122 : le backend doit voir le silence pour ignorer cette
        # gateway dans l'agregation OR, pas un etat perime.
        assert _dc.decide_report(
            latest_raw=0, latest_ts=self.NOW - (self.LIVENESS + 1.0),
            now=self.NOW, liveness=self.LIVENESS, normal_value=self.NORMAL,
        ) is None

    def test_never_seen_returns_none(self):
        # (d) jamais recu de ligne DC valide depuis le boot → None
        # (uC pas encore prêt / cable USB pas branche au demarrage)
        assert _dc.decide_report(
            latest_raw=None, latest_ts=0.0,
            now=self.NOW, liveness=self.LIVENESS, normal_value=self.NORMAL,
        ) is None

    def test_boundary_exact_liveness_is_fresh(self):
        # Frontiere : now - latest_ts == liveness → encore frais, POST 'closed'.
        # Tue le mutant `<=` -> `<` sur la borne haute : sans cette contrainte
        # explicite, un sample arrive pile en limite serait jete a tort,
        # creant un silence artificiel cote backend (INV-122 frontiere).
        assert _dc.decide_report(
            latest_raw=0, latest_ts=self.NOW - self.LIVENESS,
            now=self.NOW, liveness=self.LIVENESS, normal_value=self.NORMAL,
        ) == "closed"


# --- INV-198 : Arduino pret a brancher (programmation automatique, armement) ---

R4_APP, R4_DFU = (0x2341, 0x0069), (0x2341, 0x0369)


class TestFlashDue:
    """Programmer l'Arduino avec le firmware du depot ? Seulement un UNO R4 muet (ou son bootloader seul),
    au plus un essai par 30 min et par passerelle."""

    NOW = 10_000.0

    def due(self, usb_id=R4_APP, seen_dc=False, open_for=15.0, last_attempt=None):
        return _dc.flash_due(usb_id, seen_dc, open_for, last_attempt, self.NOW)

    def test_silent_uno_r4_is_flashed_after_15_s_not_before(self):
        assert self.due(open_for=15.0)
        assert not self.due(open_for=14.9)

    @pytest.mark.parametrize("usb_id", [(0x1A86, 0x7523), (0x2341, 0x0043), (0x2341, 0x006D), None],
                             ids=["ch340", "uno-r3", "autre-pid-arduino", "sans-identifiant-usb"])
    def test_any_other_serial_device_is_never_flashed(self, usb_id):
        assert not self.due(usb_id=usb_id, open_for=3600.0)

    def test_board_that_sent_a_dc_line_is_never_flashed(self):
        # pas de mise a jour automatique du firmware : un Arduino qui parle n'est jamais reprogramme
        assert not self.due(seen_dc=True, open_for=3600.0)

    def test_bootloader_alone_is_flashed_directly(self):
        # application absente ou corrompue : pas de port serie, rien a attendre
        assert self.due(usb_id=R4_DFU, open_for=0.0)

    @pytest.mark.parametrize("usb_id", [R4_APP, R4_DFU])
    def test_at_most_one_attempt_per_30_min(self, usb_id):
        assert not self.due(usb_id=usb_id, open_for=3600.0, last_attempt=self.NOW - 1799.0)
        assert self.due(usb_id=usb_id, open_for=3600.0, last_attempt=self.NOW - 1800.0)


class TestArmedState:
    """(etat a rapporter, armer maintenant ?) : un Arduino n'alarme qu'apres avoir lu le contact ferme une fois."""

    def test_first_closed_reading_arms(self):
        assert _dc.armed_state("closed", "S1", set()) == ("closed", True)

    def test_open_before_ever_closed_is_no_sensor(self):
        assert _dc.armed_state("open", "S1", set()) == ("no_sensor", False)

    def test_armed_arduino_reports_what_it_reads(self):
        assert _dc.armed_state("open", "S1", {"S1"}) == ("open", False)
        assert _dc.armed_state("closed", "S1", {"S1"}) == ("closed", False)

    def test_arming_is_per_serial_number(self):
        assert _dc.armed_state("open", "S2", {"S1"}) == ("no_sensor", False)

    @pytest.mark.parametrize("state", ["open", "closed"])
    def test_unknown_serial_number_is_reported_as_before(self, state):
        # rien a retenir entre deux relances : fail-to-alarm, comme avant INV-198
        assert _dc.armed_state(state, None, set()) == (state, False)


FIRMWARE = _PATH.parent / "firmware" / "dry_contact_r4"


def test_firmware_bin_is_built_from_the_committed_ino():
    """Le .bin programme par la passerelle vient du .ino versionne. Apres toute modification du .ino :
    arduino-cli compile --fqbn arduino:renesas_uno:minima --output-dir <dossier> gateway/firmware/dry_contact_r4
    puis copier <dossier>/dry_contact_r4.ino.bin ici et : sha256sum dry_contact_r4.ino > dry_contact_r4.ino.sha256"""
    recorded = (FIRMWARE / "dry_contact_r4.ino.sha256").read_text().split()
    assert recorded[1:] == ["dry_contact_r4.ino"]
    assert hashlib.sha256((FIRMWARE / "dry_contact_r4.ino").read_bytes()).hexdigest() == recorded[0], \
        "dry_contact_r4.ino modifie sans recompiler dry_contact_r4.ino.bin (cf docstring)"
