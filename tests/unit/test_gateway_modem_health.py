"""Unit test (tier 1) — sante du lien serie gateway <-> modem (`gateway/modem_health.py`).

Regression de l'incident onsite-2 2026-09-12 07:11 → 2026-10-13 : le modem
SIM7600 s'est re-enumere sur l'USB (ttyUSB4 disparu, AT passe sur ttyUSB3).
La gateway a garde l'ancien port : `Input/output error` chaque seconde pendant
un mois, 45 212 logs "tentative de reconnexion" sans aucune reconnexion.
`send_at_command` avale l'erreur et renvoie "" :
  - SMS → CMGF_FAILED (abandonnes apres 3 retries) ;
  - appels → "no_answer" alors qu'aucun appel n'etait parti.

Charge uniquement `gateway/modem_health.py` (aucun import) → pas de dependance
pyserial/numpy en tier 1, meme pattern que test_gateway_locking.py.
"""
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_PATH = pathlib.Path(__file__).resolve().parents[2] / "gateway" / "modem_health.py"
_spec = importlib.util.spec_from_file_location("gateway_modem_health", _PATH)
_mh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mh)


class TestCallWasDialed:
    """Un appel n'est compte "passe" que si le modem a accepte ATD (OK)."""

    def test_ok_means_dialed(self):
        assert _mh.call_was_dialed("OK") is True

    @pytest.mark.parametrize("response", [
        "",               # port serie mort : send_at_command renvoie ""
        "ERROR",
        "+CME ERROR: 3",
        "NO CARRIER",
    ])
    def test_anything_else_is_not_dialed(self, response):
        assert _mh.call_was_dialed(response) is False


class TestKeepaliveExit:
    """Le keepalive (AT toutes les 60s) doit faire sortir la gateway quand le
    modem ne repond plus, pour que systemd (Restart=always) la relance et
    re-detecte le port. Un echec isole ne suffit pas (modem occupe)."""

    def test_ok_resets_failures(self):
        assert _mh.at_failures_after("OK", 1) == 0

    def test_no_answer_increments_failures(self):
        assert _mh.at_failures_after("", 0) == 1
        assert _mh.at_failures_after("", 1) == 2

    def test_single_failure_does_not_exit(self):
        assert _mh.at_failures_after("", 0) < _mh.MAX_AT_FAILURES

    def test_consecutive_failures_reach_exit_threshold(self):
        failures = 0
        for _ in range(_mh.MAX_AT_FAILURES):
            failures = _mh.at_failures_after("", failures)
        assert failures >= _mh.MAX_AT_FAILURES
