"""INV-178 (mode netcut) : le helper root scripts/alarm-chaos-net.sh verifie que wg-quick@wg0 est actif,
arme la restauration de wg0 (timer systemd sur la cible, boucle bornee de tentatives) et la prouve AVANT
d'armer la coupure, refuse les arguments invalides sans rien faire, et ne coupe rien si l'armement echoue.

Le vrai script tourne contre de faux `systemd-run` / `systemctl` qui journalisent leurs arguments ; la
commande de restauration armee est rejouee telle quelle contre un faux `systemctl` qui echoue N fois.
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
[[ -f "$H/wg_inactive" && "$*" == "is-active --quiet wg-quick@wg0" ]] && exit 3
[[ -f "$H/timer_inactive" && "$*" == "is-active --quiet alarm-chaos-restore.timer" ]] && exit 3
[[ "$(basename "$0")" == systemd-run && "$*" == *"--unit=alarm-chaos-restore"* ]] && printf '%s\0' "$@" > "$H/restore_argv"
exit 0
"""
RESTORE_OPTS = "systemd-run --on-active=180 --timer-property=AccuracySec=1s --unit=alarm-chaos-restore /bin/sh -c "
CUT = "systemd-run --on-active=5 --timer-property=AccuracySec=1s --unit=alarm-chaos-cut --collect systemctl stop wg-quick@wg0"


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
    return p.returncode, p.stdout, p.stderr


def calls(h):
    return (h / "calls.log").read_text().splitlines() if (h / "calls.log").exists() else []


def test_cut_checks_wg0_arms_and_proves_restore_then_cut_five_seconds_later(h):
    rc, out, err = run(h, "cut", "180")
    assert rc == 0, err
    assert out == "ARMED\n"  # seule sortie standard : la preuve lue par le runner
    c = calls(h)
    assert c[0] == "systemctl is-active --quiet wg-quick@wg0"  # rien n'est touche si wg0 n'est pas l'unit active
    assert c[1].startswith("systemctl stop ") and "alarm-chaos-cut.timer" in c[1] and "alarm-chaos-restore.timer" in c[1]
    assert c[2].startswith("systemctl reset-failed ") and "alarm-chaos-restore.service" in c[2]
    assert c[3].startswith(RESTORE_OPTS), c[3]  # AccuracySec=1s, sans --collect (echec final visible)
    assert c[4] == "systemctl is-active --quiet alarm-chaos-restore.timer"  # preuve de l'armement, avant la coupure
    assert c[5] == CUT
    assert len(c) == 6


def replay_restore(h, ok_at):
    """Rejoue la commande armee pour alarm-chaos-restore : faux systemctl qui reussit a la tentative ok_at."""
    argv = [a.decode() for a in (h / "restore_argv").read_bytes().split(b"\0")[:-1]]
    cmd = argv[[a.startswith("--") for a in argv].index(False):]  # apres les options de systemd-run
    assert cmd[:2] == ["/bin/sh", "-c"], cmd
    rb = h / "replay"
    rb.mkdir()
    (rb / "systemctl").write_text('#!/usr/bin/env bash\necho "systemctl $*" >> "$H/replay.log"\n'
                                  'n=$(grep -c "^systemctl start" "$H/replay.log")\n(( n >= OK_AT ))\n')
    (rb / "sleep").write_text('#!/usr/bin/env bash\necho "sleep $*" >> "$H/replay.log"\n')
    for f in rb.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "H": str(h), "OK_AT": str(ok_at), "PATH": f"{rb}:{os.environ['PATH']}"}
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout + p.stderr, (h / "replay.log").read_text().splitlines()


def test_restore_timer_retries_until_wg0_is_back(h):
    assert run(h, "cut", "180")[0] == 0
    rc, out, log = replay_restore(h, ok_at=3)
    assert rc == 0, out
    assert log == ["systemctl start wg-quick@wg0", "sleep 30", "systemctl start wg-quick@wg0", "sleep 30",
                   "systemctl start wg-quick@wg0"]
    assert "tentative 3" in out  # journalise (stdout du service transitoire -> journal)


def test_restore_timer_gives_up_after_ten_attempts_and_fails_visibly(h):
    assert run(h, "cut", "180")[0] == 0
    rc, out, log = replay_restore(h, ok_at=99)
    assert rc != 0  # unit en echec, gardee (pas de --collect) : systemctl --failed
    assert log.count("systemctl start wg-quick@wg0") == 10
    assert log.count("sleep 30") == 9  # pas d'attente inutile apres la derniere
    assert "ECHEC" in out


@pytest.mark.parametrize("args", [("cut", "59"), ("cut", "721"), ("cut", "abc"), ("cut",), ("cut", "180", "x"),
                                  ("flood",), (), ("restore", "x"),
                                  ("cut", "0777"), ("cut", "01320"), ("cut", "060"), ("cut", "18446744073709551716"),
                                  ("cut", "+120"), ("cut", " 120")])
def test_invalid_args_exit_2_and_touch_nothing(h, args):
    rc, out, _ = run(h, *args)
    assert rc == 2
    assert calls(h) == [] and out == ""


def test_wg0_unit_inactive_refuses_without_touching_anything(h):
    (h / "wg_inactive").write_text("")
    rc, out, err = run(h, "cut", "180")
    assert rc == 1
    assert out == "" and "inactif" in err
    assert calls(h) == ["systemctl is-active --quiet wg-quick@wg0"]  # aucun timer arrete, arme ou coupe


@pytest.mark.parametrize("flag", ["arm_fails", "timer_inactive"])
def test_arming_failure_or_unproven_exits_nonzero_without_cut(h, flag):
    (h / flag).write_text("")
    rc, out, err = run(h, "cut", "60")
    assert rc not in (0, 2), err
    assert "ARMED" not in out
    c = calls(h)
    assert not any("alarm-chaos-cut" in x and x.startswith("systemd-run") for x in c), c
    assert "systemctl stop wg-quick@wg0" not in c


def test_restore_starts_wg0_and_cancels_pending_cut(h):
    rc, _, err = run(h, "restore")
    assert rc == 0, err
    c = calls(h)
    assert c[-1] == "systemctl start wg-quick@wg0"
    assert any(x.startswith("systemctl stop") and "alarm-chaos-cut.timer" in x for x in c[:-1])
