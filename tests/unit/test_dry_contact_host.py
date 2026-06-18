"""Unit test (tier 1) — logique pure du contact sec (`gateway/dry_contact.py`).

Couvre le parsing des lignes du firmware hôte (`DC:<0|1>`) et le mapping
raw→état métier, partagés par les moniteurs modem et hôte de la gateway.

Test pur : charge uniquement `gateway/dry_contact.py` (stdlib only) par chemin,
donc aucune dépendance lourde (pyserial/numpy) tirée dans le tier 1.
"""
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
