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
