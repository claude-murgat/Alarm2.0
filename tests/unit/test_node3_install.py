"""INV-141 etendu a NODE3 (2026-11-13) : infra/node3/install.sh installe seulement la partie hote de NODE3
(CD, sauvegarde, email, alarm-host-sync), jamais la pile modem du site. --auto ne demarre ni n'active
aucun timer (une maintenance garde le sien arrete) et ne relance qu'un timer actif dont l'unit a change.

Le vrai script tourne dans une racine temporaire (ROOT) ; seul systemctl est remplace (journal des appels).
"""
import os
import pathlib
import subprocess

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]  # subprocess, hors backend/app/logic

REPO = pathlib.Path(__file__).resolve().parents[2]
INSTALL = REPO / "infra" / "node3" / "install.sh"
TIMERS = ["alarm-cd-orchestrate", "alarm-cd-pull", "alarm-db-backup", "alarm-db-restore-test", "alarm-host-sync"]
FILES = {  # destination -> (source dans le depot, mode)
    "/usr/local/bin/alarm-cd-orchestrate": ("scripts/alarm-cd-orchestrate.sh", 0o755),
    "/usr/local/bin/alarm-cd-canary.sh": ("scripts/alarm-cd-canary.sh", 0o755),
    "/usr/local/bin/alarm-cd-rollback.sh": ("scripts/alarm-cd-rollback.sh", 0o755),
    "/usr/local/bin/alarm-cd-pull": ("scripts/alarm-cd-pull.sh", 0o755),
    "/usr/local/bin/alarm-db-backup": ("scripts/alarm-db-backup.sh", 0o755),
    "/usr/local/bin/alarm-host-sync": ("scripts/alarm-host-sync.sh", 0o755),
    "/opt/alarm/send-alert-email.py": ("scripts/send-alert-email.py", 0o755),
    "/etc/systemd/system/alarm-host-sync.service.d/node3.conf": (
        "infra/node3/alarm-host-sync.service.d/node3.conf", 0o644),
    **{f"/etc/systemd/system/{u}.{k}": (f"infra/onsite/systemd/{u}.{k}", 0o644)
       for u in TIMERS for k in ("service", "timer")},
}
FAKE_SYSTEMCTL = """#!/bin/bash
echo "$*" >> "$SYSTEMCTL_LOG"
if [ "$1" = is-active ]; then
  u="${@: -1}"; [[ " $ACTIVE_UNITS " == *" $u "* ]] && exit 0 || exit 3
fi
exit 0
"""


@pytest.fixture
def box(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
    (bin_dir / "systemctl").chmod(0o755)
    log = tmp_path / "systemctl.log"

    def run(*args, active=""):
        log.write_text("")
        env = {**os.environ, "ROOT": str(tmp_path / "root"), "PATH": f"{bin_dir}:{os.environ['PATH']}",
               "SYSTEMCTL_LOG": str(log), "ACTIVE_UNITS": active}
        p = subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True, text=True)
        return p, [line for line in log.read_text().splitlines() if not line.startswith("is-active")]

    return {"root": tmp_path / "root", "run": run}


def dest(box, path):
    return box["root"] / path.lstrip("/")


def mtimes(box):
    return {d: dest(box, d).stat().st_mtime_ns for d in FILES}


def test_bootstrap_installs_node3_files_and_enables_the_timers(box):
    p, calls = box["run"]()
    assert p.returncode == 0, p.stderr
    for d, (src, mode) in FILES.items():
        f = dest(box, d)
        assert f.read_bytes() == (REPO / src).read_bytes(), d
        assert f.stat().st_mode & 0o777 == mode, d
    conf = dest(box, "/etc/systemd/system/alarm-host-sync.service.d/node3.conf").read_text()
    assert "Environment=HOST_INSTALLER=infra/node3/install.sh" in conf.splitlines()
    assert "daemon-reload" in calls
    for u in TIMERS:
        assert f"enable --now {u}.timer" in calls, u
    # Jamais la pile du site (passerelle, modem, sudoers).
    assert not (box["root"] / "etc" / "sudoers.d").exists()
    assert not any("gateway" in str(f) or "modem" in str(f) for f in box["root"].rglob("*"))


def test_auto_without_change_rewrites_nothing_and_starts_nothing(box):
    box["run"]()
    before = mtimes(box)
    p, calls = box["run"]("--auto", active=" ".join(f"{u}.timer" for u in TIMERS))
    assert p.returncode == 0, p.stderr
    assert mtimes(box) == before
    assert calls == []


def test_auto_reinstalls_changes_and_restarts_only_active_changed_timers(box):
    box["run"]()
    for d in ("/usr/local/bin/alarm-cd-orchestrate", "/etc/systemd/system/alarm-cd-orchestrate.timer",
              "/etc/systemd/system/alarm-db-backup.timer"):
        dest(box, d).write_text("ancienne version\n")
    # alarm-db-backup.timer arrete (maintenance) : doit le rester.
    p, calls = box["run"]("--auto", active="alarm-cd-orchestrate.timer alarm-cd-pull.timer")
    assert p.returncode == 0, p.stderr
    for d in ("/usr/local/bin/alarm-cd-orchestrate", "/etc/systemd/system/alarm-cd-orchestrate.timer",
              "/etc/systemd/system/alarm-db-backup.timer"):
        assert dest(box, d).read_bytes() == (REPO / FILES[d][0]).read_bytes(), d
    assert calls == ["daemon-reload", "restart alarm-cd-orchestrate.timer"]
