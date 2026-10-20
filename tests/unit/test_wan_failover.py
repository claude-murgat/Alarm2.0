"""Unit test (tier 1) — bascule automatique box -> 4G (`scripts/wan-failover.py`, INV-140).

Constat 2026-10-19 sur onsite-2 : `default via 192.168.1.1 dev enp1s0 onlink` (statique,
metrique 0) ne disparait que si le cable est coupe. Box sans internet -> le trafic
continuait vers elle et la route 4G (metrique 300) ne servait jamais.

Charge `scripts/wan-failover.py` par chemin (nom avec tiret, aucune dependance) :
seules les fonctions pures sont testees ; la boucle (ping/ip) est validee sur alarm-1.
"""
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "wan-failover.py"
_spec = importlib.util.spec_from_file_location("wan_failover", _PATH)
_wf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wf)

ROUTES = (
    "default via 192.168.1.1 dev enp1s0 onlink \n"
    "default via 10.150.124.89 dev wwp0s20f0u3i5 metric 300 onlink \n"
)
DEMOTED = (
    "default via 10.150.124.89 dev wwp0s20f0u3i5 metric 300 onlink \n"
    "default via 192.168.1.1 dev enp1s0 metric 500 onlink \n"
)


class TestParseRoutes:
    def test_box_and_cell_routes(self):
        box, cell = _wf.box_and_cell(_wf.parse_default_routes(ROUTES))
        assert box == {"gw": "192.168.1.1", "dev": "enp1s0", "metric": 0}
        assert cell == {"gw": "10.150.124.89", "dev": "wwp0s20f0u3i5", "metric": 300}

    def test_demoted_box_is_still_found(self):
        box, cell = _wf.box_and_cell(_wf.parse_default_routes(DEMOTED))
        assert box["metric"] == _wf.DEMOTED_METRIC and cell["dev"].startswith("ww")

    def test_node_without_4g(self):
        box, cell = _wf.box_and_cell(_wf.parse_default_routes("default via 192.168.1.1 dev enp0s31f6 onlink\n"))
        assert box["dev"] == "enp0s31f6" and cell is None


class TestDecide:
    """3 echecs box + 4G OK -> demote ; 3 succes box -> restore ; jamais vers un lien mort."""

    def test_three_box_failures_with_4g_up_demotes(self):
        assert _wf.decide(demoted=False, box_fails=3, box_oks=0, cell_ok=True) == "demote"

    def test_two_failures_is_not_enough(self):
        assert _wf.decide(demoted=False, box_fails=2, box_oks=0, cell_ok=True) is None

    def test_never_switch_to_dead_4g(self):
        assert _wf.decide(demoted=False, box_fails=10, box_oks=0, cell_ok=False) is None

    def test_three_box_successes_restores(self):
        assert _wf.decide(demoted=True, box_fails=0, box_oks=3, cell_ok=True) == "restore"

    def test_restore_even_if_4g_is_down(self):
        assert _wf.decide(demoted=True, box_fails=0, box_oks=3, cell_ok=False) == "restore"

    def test_two_successes_is_not_enough(self):
        assert _wf.decide(demoted=True, box_fails=0, box_oks=2, cell_ok=True) is None

    def test_nothing_to_do_when_box_is_fine(self):
        assert _wf.decide(demoted=False, box_fails=0, box_oks=5, cell_ok=True) is None


class TestCounters:
    def test_success_resets_failures(self):
        assert _wf.count(True, fails=2, oks=0) == (0, 1)

    def test_failure_resets_successes(self):
        assert _wf.count(False, fails=0, oks=2) == (1, 0)


class TestRouteCommands:
    BOX = {"gw": "192.168.1.1", "dev": "enp1s0", "metric": 0}

    def test_demote_adds_metric_500_then_removes_metric_0(self):
        assert _wf.route_commands("demote", self.BOX) == [
            ["ip", "route", "replace", "default", "via", "192.168.1.1", "dev", "enp1s0", "metric", "500", "onlink"],
            ["ip", "route", "del", "default", "via", "192.168.1.1", "dev", "enp1s0", "metric", "0"],
        ]

    def test_restore_adds_metric_0_then_removes_metric_500(self):
        assert _wf.route_commands("restore", {**self.BOX, "metric": 500}) == [
            ["ip", "route", "replace", "default", "via", "192.168.1.1", "dev", "enp1s0", "metric", "0", "onlink"],
            ["ip", "route", "del", "default", "via", "192.168.1.1", "dev", "enp1s0", "metric", "500"],
        ]


class TestConstants:
    def test_spec_values(self):
        assert _wf.INTERVAL_S == 10
        assert _wf.FAILS_TO_SWITCH == 3 and _wf.OKS_TO_RESTORE == 3
        assert _wf.DEMOTED_METRIC == 500
        assert _wf.TARGETS == ("51.210.105.102", "1.1.1.1", "9.9.9.9")
