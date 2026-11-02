"""INV-167 : l'orchestrateur CD deploie chaque site independamment.

Incident 2026-10-29 : node4/node5 (Beaufort, non votants) hors ligne ~2 jours. Le preflight SSH
de `alarm-cd-orchestrate.sh` faisait `exit 1` sur 10.99.0.4 : aucun deploiement de prod, sur
aucun site, pendant 2 jours (4 versions jamais deployees), plus 1 h de cooldown a chaque fois.

Le vrai script tourne (copie dans un dossier temporaire, a cote d'un faux `alarm-cd-canary.sh`
resolu via son SCRIPT_DIR) contre de faux `docker` / `ssh` (PATH) qui journalisent leurs appels.
"""
import fcntl
import os
import pathlib
import subprocess
import time

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alarm-cd-orchestrate.sh"
IP = {1: "10.99.0.2", 2: "10.99.0.3", 3: "10.99.0.1", 4: "10.99.0.4", 5: "10.99.0.5"}
DIGEST = "sha256:" + "b" * 64

DOCKER = '#!/usr/bin/env bash\necho "ghcr.io/claude-murgat/alarm-backend@$(cat "$H/digest")"\n'
SSH = r"""#!/usr/bin/env bash
for a in "$@"; do case "$a" in alarm@*) ip=${a#alarm@} ;; esac; done
echo "$ip" >> "$H/ssh.log"
case " $(cat "$H/down" 2>/dev/null) " in *" $ip "*) exit 255 ;; esac
"""
CANARY = r"""#!/usr/bin/env bash
echo "$2" >> "$H/canary.log"
case " $(cat "$H/fail" 2>/dev/null) " in *" $2 "*) exit 2 ;; esac
"""


@pytest.fixture
def cd(tmp_path):
    for d in ("bin", "cd", "state"):
        (tmp_path / d).mkdir()
    for name, body, where in (("docker", DOCKER, "bin"), ("ssh", SSH, "bin"),
                              ("alarm-cd-canary.sh", CANARY, "cd"),
                              ("alarm-cd-orchestrate.sh", SCRIPT.read_text(), "cd")):
        f = tmp_path / where / name
        f.write_text(body)
        f.chmod(0o755)
    (tmp_path / "digest").write_text(DIGEST)
    return tmp_path


def run(h, down=(), fail=(), default_lock=False):
    """Un tick du timer. Retourne (exit code, noeuds passes au canary, stderr)."""
    (h / "down").write_text(" ".join(IP[n] for n in down))
    (h / "fail").write_text(" ".join(map(str, fail)))
    (h / "canary.log").write_text("")
    env = {**os.environ, "H": str(h), "PATH": f"{h / 'bin'}:{os.environ['PATH']}",
           "STATE_DIR": str(h / "state"), "LOCK_FILE": str(h / "lock"),
           "API_BASE": "http://test", "GATEWAY_KEY": "k", "SOAK": "0"}
    if default_lock:  # chemin par defaut du script, celui que prend aussi alarm-chaos.sh
        del env["LOCK_FILE"]
    p = subprocess.run(["bash", str(h / "cd" / "alarm-cd-orchestrate.sh")], env=env,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, [int(n) for n in (h / "canary.log").read_text().split()], p.stderr


def outcome(h):
    return (h / "state" / "orchestrate.state").read_text().split("\t")[1]


def at_digest(h):
    return sorted(n for n in IP if (h / "state" / f"node{n}.digest").exists()
                  and (h / "state" / f"node{n}.digest").read_text().strip() == DIGEST)


def test_beaufort_down_does_not_block_other_sites_then_catches_up(cd):
    rc, deployed, err = run(cd, down=(4, 5))
    assert rc == 0, err
    assert deployed == [3, 2, 1]
    assert outcome(cd) == "partial"
    assert at_digest(cd) == [1, 2, 3]
    assert "node4 (Beaufort) injoignable : reporte au prochain passage" in err

    # Tick suivant, Beaufort revenu : seuls 4 et 5, tout de suite (pas de cooldown).
    rc, deployed, err = run(cd)
    assert rc == 0, err
    assert deployed == [5, 4]
    assert outcome(cd) == "success"
    assert at_digest(cd) == [1, 2, 3, 4, 5]


def test_voter_down_no_voter_restarted_non_voters_deployed(cd):
    rc, deployed, err = run(cd, down=(2,))
    assert rc == 0, err
    assert deployed == [5, 4]
    assert outcome(cd) == "partial"
    assert "quorum" in err


def test_canary_failure_halts_chain_and_cooldown_holds(cd):
    rc, deployed, _ = run(cd, fail=(3,))
    assert rc == 2
    assert deployed == [3]
    assert outcome(cd) == "failure"
    assert at_digest(cd) == []

    rc, deployed, err = run(cd)
    assert (rc, deployed) == (0, [])
    assert "cooldown" in err

    # Cooldown (1 h) expire : la chaine est retentee.
    (cd / "state" / "orchestrate.state").write_text(f"{DIGEST}\tfailure\t{int(time.time()) - 3601}\n")
    rc, deployed, err = run(cd)
    assert (rc, deployed) == (0, [3, 5, 4, 2, 1]), err
    assert outcome(cd) == "success"


def test_all_nodes_at_digest_nothing_done(cd):
    for n in IP:
        (cd / "state" / f"node{n}.digest").write_text(DIGEST + "\n")
    rc, deployed, err = run(cd)
    assert (rc, deployed, err) == (0, [], "")
    assert not (cd / "ssh.log").exists()


def test_node_behind_caught_up_even_if_digest_unchanged_since_success(cd):
    for n in IP:
        (cd / "state" / f"node{n}.digest").write_text(DIGEST if n != 4 else "sha256:old")
    (cd / "state" / "orchestrate.state").write_text(f"{DIGEST}\tsuccess\t{int(time.time())}\n")
    rc, deployed, err = run(cd)
    assert (rc, deployed) == (0, [4]), err
    assert outcome(cd) == "success"


def test_skips_while_chaos_runner_holds_the_default_lock(cd):
    """INV-178 : alarm-chaos.sh tient ${STATE_DIR}/orchestrate.lock pendant une panne controlee
    (test_alarm_chaos.py prouve qu'il prend ce chemin par defaut) : aucun deploiement."""
    with open(cd / "state" / "orchestrate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, deployed, err = run(cd, default_lock=True)
    assert (rc, deployed) == (0, []), err
    assert "deja pris" in err and "skip ce cycle" in err
    assert not (cd / "ssh.log").exists()
