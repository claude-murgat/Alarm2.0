"""Unit test (tier 1) — INV-182 : `scripts/modem-set-rat.py` (alarm-modem-rat, au boot) sur une
machine sans modem ne laisse pas d'unit `failed` : sans /dev/ttyUSB*, rien a faire, code 0.

Avant : « aucun port AT libre+repondant » puis exit 1 a chaque boot de chaque machine sans modem.
Le vrai script tourne (runpy) ; pyserial (absent du tier 1), glob et attentes sont remplaces.
"""
import glob
import pathlib
import runpy
import sys
import time
import types

import pytest

# no_mutation : ne touche pas backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "modem-set-rat.py"


def test_no_modem_is_success(monkeypatch):
    monkeypatch.setitem(sys.modules, "serial", sys.modules.get("serial") or types.ModuleType("serial"))
    monkeypatch.setattr(glob, "glob", lambda pattern: [])
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(SystemExit) as exit_:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exit_.value.code in (0, None)
