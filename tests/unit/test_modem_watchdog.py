"""Unit test (tier 1) — INV-146 : le watchdog modem detecte un modem present sur l'USB
mais muet, via les redemarrages en boucle de la gateway (`scripts/modem-watchdog.py`).

Alarme 886 du 2026-10-21 : SIM7600 visible sur l'USB mais port inutilisable
(`BrokenPipeError`), gateway relancee toutes les ~90 s par systemd. Le watchdog ne
reagissait qu'a un modem absent de l'USB : aucune recuperation, ni SMS ni appel.
"""
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "modem-watchdog.py"
_spec = importlib.util.spec_from_file_location("modem_watchdog", _PATH)
_wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wd)

W = 600  # fenetre de 10 min


class TestMuteDetected:
    def test_three_restarts_in_window_is_mute(self):
        assert _wd.mute_detected([1000, 1090, 1180], now=1200) is True

    def test_two_restarts_is_not_mute(self):
        assert _wd.mute_detected([1000, 1090], now=1100) is False

    def test_old_restarts_are_ignored(self):
        assert _wd.mute_detected([1000, 1090, 1180], now=1000 + W + 1) is False

    def test_today_incident_timeline(self):
        # sorties 16:03:05, 16:04:47, 16:06:30 (en secondes depuis 16:00)
        assert _wd.mute_detected([185, 287, 390], now=400) is True
