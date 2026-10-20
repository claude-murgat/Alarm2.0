"""Unit test (tier 1) — selection des tests et modules du test de mutation (`mutmut_config.py`).

Garde-fou de l'acceleration du job Tier 1.5 : si un module de logique n'etait associe a
aucun test, ses mutants ne seraient tues par rien (ou retomberaient sur la suite complete).
"""
import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.unit

_PATH = pathlib.Path(__file__).resolve().parents[2] / "mutmut_config.py"
_spec = importlib.util.spec_from_file_location("mutmut_config", _PATH)
_mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mc)


def test_every_logic_module_has_tests():
    for mod in _mc.logic_modules():
        assert _mc.tests_for(mod), f"aucun test unitaire ne cite backend/app/logic/{mod}.py"


def test_module_maps_to_its_own_tests():
    assert "tests/unit/test_sms_call_timers.py" in _mc.tests_for("sms_timer")
    assert "tests/unit/test_quorum_detection.py" in _mc.tests_for("quorum_detection")


def test_models_mutants_also_run_tests_of_importing_modules():
    assert {"escalation", "sms_timer"} <= _mc.importers("models")
    assert "tests/unit/test_escalation.py" in _mc.tests_for("models")


def test_changed_logic_module_is_mutated():
    assert _mc.modules_to_mutate(["backend/app/logic/sms_timer.py"]) == ["backend/app/logic/sms_timer.py"]


def test_changed_test_mutates_the_modules_it_cites():
    mutated = _mc.modules_to_mutate(["tests/unit/test_sms_call_timers.py"])
    assert "backend/app/logic/sms_timer.py" in mutated
    assert "backend/app/logic/quorum_detection.py" not in mutated


def test_this_config_test_is_not_a_logic_test():
    assert _mc.SELF_TEST not in _mc.tests_for("sms_timer")
    assert _mc.modules_to_mutate([_mc.SELF_TEST]) == []


def test_unrelated_changes_mutate_nothing():
    assert _mc.modules_to_mutate(["scripts/4g-standby.sh", "tests/unit/test_4g_standby.py", "docs/X.md"]) == []


@pytest.mark.parametrize("changed", ["pyproject.toml", "backend/app/logic/models.py", "mutmut_config.py"])
def test_shared_files_mutate_everything(changed):
    assert len(_mc.modules_to_mutate([changed])) == len(_mc.logic_modules())
