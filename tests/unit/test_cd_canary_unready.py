"""INV-167 (amende 2026-11-02) : le canary sort en 3 quand ses prerequis echouent, sans toucher au noeud.

alarm-1 passe sur sa 4G de secours (VPN joignable, DNS mort) : `docker buildx imagetools inspect` ne
joint pas le registre. Avant : exit 1 = canary en echec -> chaine arretee, 1 h de cooldown, Izeron bloque.
Le vrai script tourne contre un faux `ssh` (PATH) qui journalise les commandes distantes.
"""
import os
import pathlib
import subprocess

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alarm-cd-canary.sh"
SSH = r"""#!/usr/bin/env bash
echo "${!#}" >> "$H/ssh.log"
[[ -f "$H/ssh_down" ]] && exit 255
case "${!#}" in
  *"imagetools inspect"*) [[ -f "$H/registry_ko" ]] && exit 1; echo "sha256:remote" ;;
  *"image inspect"*) echo "ghcr.io/claude-murgat/alarm-backend@sha256:local" ;;
esac
exit 0
"""


@pytest.fixture
def h(tmp_path):
    (tmp_path / "bin").mkdir()
    f = tmp_path / "bin" / "ssh"
    f.write_text(SSH)
    f.chmod(0o755)
    return tmp_path


def run(h):
    env = {**os.environ, "H": str(h), "PATH": f"{h / 'bin'}:{os.environ['PATH']}"}
    p = subprocess.run(["bash", str(SCRIPT), "--node", "4", "--soak", "0", "--api-base", "http://test",
                        "--gateway-key", "k"], env=env, capture_output=True, text=True, timeout=30)
    log = (h / "ssh.log").read_text().splitlines() if (h / "ssh.log").exists() else []
    return p.returncode, log, p.stderr


@pytest.mark.parametrize("flag, msg", [("ssh_down", "SSH vers alarm@10.99.0.4 echoue"),
                                       ("registry_ko", "impossible de lire le digest distant")])
def test_unready_target_exits_3_without_touching_the_node(h, flag, msg):
    (h / flag).write_text("")
    rc, log, err = run(h)
    assert rc == 3, err
    assert msg in err
    assert not any("start-prod-node" in c or "compose" in c or " tag " in c for c in log), log
