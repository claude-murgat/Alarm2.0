"""INV-178 (mode netcut) : le helper root scripts/alarm-chaos-net.sh arme la restauration de wg0
(timer systemd sur la cible) AVANT d'armer la coupure, refuse les arguments invalides sans rien
faire, et ne coupe rien si l'armement echoue.

Le vrai script tourne contre de faux `systemd-run` / `systemctl` qui journalisent leurs arguments.
"""
import os
import pathlib
import subprocess

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alarm-chaos-net.sh"

FAKE = r"""#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$H/calls.log"
[[ -f "$H/arm_fails" && "$*" == *"--unit=alarm-chaos-restore"* ]] && exit 1
exit 0
"""


@pytest.fixture
def h(tmp_path):
    (tmp_path / "bin").mkdir()
    for name in ("systemd-run", "systemctl"):
        f = tmp_path / "bin" / name
        f.write_text(FAKE)
        f.chmod(0o755)
    return tmp_path


def run(h, *args):
    env = {**os.environ, "H": str(h), "PATH": f"{h / 'bin'}:{os.environ['PATH']}"}
    p = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stderr


def calls(h):
    return (h / "calls.log").read_text().splitlines() if (h / "calls.log").exists() else []


def test_cut_arms_restore_then_cut_five_seconds_later(h):
    rc, err = run(h, "cut", "180")
    assert rc == 0, err
    c = calls(h)
    assert c[0].startswith("systemctl stop ") and "alarm-chaos-cut.timer" in c[0] and "alarm-chaos-restore.timer" in c[0]
    assert c[1] == "systemd-run --on-active=180 --unit=alarm-chaos-restore --collect systemctl start wg-quick@wg0"
    assert c[2] == "systemd-run --on-active=5 --unit=alarm-chaos-cut --collect systemctl stop wg-quick@wg0"
    assert len(c) == 3


@pytest.mark.parametrize("args", [("cut", "59"), ("cut", "721"), ("cut", "abc"), ("cut",), ("cut", "180", "x"),
                                  ("flood",), (), ("restore", "x")])
def test_invalid_args_exit_2_and_touch_nothing(h, args):
    rc, _ = run(h, *args)
    assert rc == 2
    assert calls(h) == []


def test_arming_failure_exits_nonzero_without_cut(h):
    (h / "arm_fails").write_text("")
    rc, err = run(h, "cut", "60")
    assert rc not in (0, 2), err
    c = calls(h)
    assert not any("alarm-chaos-cut" in x and x.startswith("systemd-run") for x in c), c
    assert "systemctl stop wg-quick@wg0" not in c


def test_restore_starts_wg0_and_cancels_pending_cut(h):
    rc, err = run(h, "restore")
    assert rc == 0, err
    c = calls(h)
    assert c[-1] == "systemctl start wg-quick@wg0"
    assert any(x.startswith("systemctl stop") and "alarm-chaos-cut.timer" in x for x in c[:-1])
