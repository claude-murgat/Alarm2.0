"""INV-167 : l'orchestrateur CD deploie chaque site independamment.

Incident 2026-10-29 : node4/node5 (Beaufort, non votants) hors ligne ~2 jours. Le preflight SSH
de `alarm-cd-orchestrate.sh` faisait `exit 1` sur 10.99.0.4 : aucun deploiement de prod, sur
aucun site, pendant 2 jours (4 versions jamais deployees), plus 1 h de cooldown a chaque fois.

Le vrai script tourne (copie dans un dossier temporaire, a cote d'un faux `alarm-cd-canary.sh`
resolu via son SCRIPT_DIR) contre de faux `docker` / `ssh` (PATH) qui journalisent leurs appels.

7 noeuds depuis le 2026-11-05 (node6/node7 = alarm-3/alarm-4, non votants) : ordre `3 7 6 5 4 2 1`.
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
IP = {1: "10.99.0.2", 2: "10.99.0.3", 3: "10.99.0.1", 4: "10.99.0.4", 5: "10.99.0.5", 6: "10.99.0.6", 7: "10.99.0.7"}
DIGEST = "sha256:" + "b" * 64

DOCKER = '#!/usr/bin/env bash\necho "ghcr.io/claude-murgat/alarm-backend@$(cat "$H/digest")"\n'
SSH = r"""#!/usr/bin/env bash
for a in "$@"; do case "$a" in alarm@*) ip=${a#alarm@} ;; esac; done
echo "$ip" >> "$H/ssh.log"
case " $(cat "$H/down" 2>/dev/null) " in *" $ip "*) exit 255 ;; esac
"""
# Decouverte du leader Patroni (GET /health des 7 noeuds) : journalisee ; seule l'IP de `primary` repond.
CURL = r"""#!/usr/bin/env bash
echo "$*" >> "$H/curl.log"
p=$(cat "$H/primary" 2>/dev/null)
[[ -n "$p" && "$*" == *"//$p:8000/health"* ]] && echo '{"role": "primary", "cluster_writable": true}' && exit 0
exit 7
"""
CANARY = r"""#!/usr/bin/env bash
echo "$2" >> "$H/canary.log"
printf '%s\n' "$@" > "$H/args.$2"
case " $(cat "$H/fail" 2>/dev/null) " in *" $2 "*) exit 2 ;; esac
case " $(cat "$H/unready" 2>/dev/null) " in *" $2 "*) exit 3 ;; esac  # prerequis KO, rien touche
# :stable promu pendant ce canary (nouveau digest unique, cache de NODE3 mis a jour) ; une ligne = une promotion
if grep -qx "$2" "$H/promote" 2>/dev/null; then
  printf 'sha256:%064d\n' "$(wc -l < "$H/canary.log")" > "$H/digest"
  sed -i "0,/^$2\$/{//d}" "$H/promote"
fi
"""


@pytest.fixture
def cd(tmp_path):
    for d in ("bin", "cd", "state"):
        (tmp_path / d).mkdir()
    for name, body, where in (("docker", DOCKER, "bin"), ("ssh", SSH, "bin"), ("curl", CURL, "bin"),
                              ("alarm-cd-canary.sh", CANARY, "cd"),
                              ("alarm-cd-orchestrate.sh", SCRIPT.read_text(), "cd")):
        f = tmp_path / where / name
        f.write_text(body)
        f.chmod(0o755)
    (tmp_path / "digest").write_text(DIGEST)
    return tmp_path


def run(h, down=(), fail=(), default_lock=False, unready=(), promote=(), api_base="http://test", primary=None):
    """Un tick du timer. Retourne (exit code, noeuds passes au canary, stderr)."""
    (h / "primary").write_text(IP[primary] if primary else "")
    (h / "down").write_text(" ".join(IP[n] for n in down))
    (h / "fail").write_text(" ".join(map(str, fail)))
    (h / "unready").write_text(" ".join(map(str, unready)))
    (h / "promote").write_text("".join(f"{n}\n" for n in promote))
    (h / "canary.log").write_text("")
    env = {**os.environ, "H": str(h), "PATH": f"{h / 'bin'}:{os.environ['PATH']}",
           "STATE_DIR": str(h / "state"), "LOCK_FILE": str(h / "lock"),
           "API_BASE": api_base, "GATEWAY_KEY": "k", "SOAK": "0"}
    if api_base is None:  # cas de prod sans API_BASE dans /etc/alarm/cd.env : decouverte du leader
        del env["API_BASE"]
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
    assert deployed == [3, 7, 6, 2, 1]
    assert outcome(cd) == "partial"
    assert at_digest(cd) == [1, 2, 3, 6, 7]
    assert "node4 (Beaufort) injoignable : reporte au prochain passage" in err

    # Tick suivant, Beaufort revenu : seuls 4 et 5, tout de suite (pas de cooldown).
    rc, deployed, err = run(cd)
    assert rc == 0, err
    assert deployed == [5, 4]
    assert outcome(cd) == "success"
    assert at_digest(cd) == [1, 2, 3, 4, 5, 6, 7]


def test_node6_node7_not_yet_installed_do_not_block_the_others(cd):
    """2026-11-05 : alarm-3/alarm-4 annonces avant leur installation : reportes a chaque passage."""
    rc, deployed, err = run(cd, down=(6, 7))
    assert rc == 0, err
    assert deployed == [3, 5, 4, 2, 1]
    assert outcome(cd) == "partial"
    assert "node7 (Izeron) injoignable : reporte au prochain passage" in err
    assert "node6 (Izeron) injoignable : reporte au prochain passage" in err

    rc, deployed, err = run(cd)  # installes : deployes au tick suivant, les autres sautes
    assert (rc, deployed) == (0, [7, 6]), err
    assert outcome(cd) == "success"


def test_voter_down_no_voter_restarted_non_voters_deployed(cd):
    rc, deployed, err = run(cd, down=(2,))
    assert rc == 0, err
    assert deployed == [7, 6, 5, 4]
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
    assert (rc, deployed) == (0, [3, 7, 6, 5, 4, 2, 1]), err
    assert outcome(cd) == "success"


def test_all_nodes_at_digest_nothing_done(cd):
    for n in IP:
        (cd / "state" / f"node{n}.digest").write_text(DIGEST + "\n")
    rc, deployed, err = run(cd)
    assert (rc, deployed, err) == (0, [], "")
    assert not (cd / "ssh.log").exists()


def at_current_digest(h):
    for n in IP:
        (h / "state" / f"node{n}.digest").write_text(DIGEST + "\n")


def test_no_op_tick_is_silent_and_cheap_without_api_base(cd):
    """Tick toutes les 2 min (2026-11-09) : rien a faire -> aucun log, ni SSH, ni sonde /health."""
    at_current_digest(cd)
    rc, deployed, err = run(cd, api_base=None)
    assert (rc, deployed, err) == (0, [], "")
    assert not (cd / "ssh.log").exists()
    assert not (cd / "curl.log").exists()


def test_no_op_tick_leaves_the_lock_to_the_chaos_runner(cd):
    """INV-178 : rien a deployer -> le tick ne touche pas au verrou partage, donc rien a dire si
    alarm-chaos.sh le tient (avant : « Lock ... deja pris » toutes les 2 min pendant une panne)."""
    at_current_digest(cd)
    with open(cd / "state" / "orchestrate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, deployed, err = run(cd, default_lock=True)
    assert (rc, deployed, err) == (0, [], "")


def test_deploy_tick_discovers_the_leader_only_when_there_is_work(cd):
    rc, deployed, err = run(cd, api_base=None)
    assert (rc, deployed) == (0, ORDER), err
    assert "aucun leader Patroni joignable" in err  # faux curl : aucun /health ne repond


def test_orchestrator_timer_ticks_every_2_min():
    """INV-167 : tick 2 min (15 min jusqu'au 2026-11-09), gigue 30 s au plus."""
    timer = (SCRIPT.parents[1] / "infra" / "onsite" / "systemd" / "alarm-cd-orchestrate.timer").read_text()
    lines = {line.strip() for line in timer.splitlines()}
    assert {"OnUnitActiveSec=2min", "RandomizedDelaySec=30s"} <= lines


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


def test_unready_beaufort_node_is_deferred_without_halting_izeron(cd):
    """2026-11-02 : alarm-1 sur sa 4G de secours, DNS mort, registre illisible -> canary exit 3."""
    rc, deployed, err = run(cd, unready=(4,))
    assert rc == 0, err
    assert deployed == [3, 7, 6, 5, 4, 2, 1]  # 4 tente, reporte ; 2 et 1 deployes quand meme
    assert outcome(cd) == "partial"
    assert at_digest(cd) == [1, 2, 3, 5, 6, 7]
    assert "node4 (Beaufort) pas pret : reporte au prochain passage" in err

    rc, deployed, err = run(cd)  # pas de cooldown : node4 repris au tick suivant
    assert (rc, deployed) == (0, [4]), err
    assert outcome(cd) == "success"


def test_unready_voter_defers_the_following_voters(cd):
    """Un votant pas pret est peut-etre tombe : redemarrer un autre votant perdrait le quorum."""
    rc, deployed, err = run(cd, unready=(3,))
    assert rc == 0, err
    assert deployed == [3, 7, 6, 5, 4]  # 2 et 1 (votants) reportes sans etre touches
    assert outcome(cd) == "partial"
    assert at_digest(cd) == [4, 5, 6, 7]
    assert "node2 (Izeron, votant) reporte" in err

    rc, deployed, err = run(cd)
    assert (rc, deployed) == (0, [3, 2, 1]), err
    assert outcome(cd) == "success"


def test_canary_told_the_nodes_still_to_restart_and_the_voters(cd):
    """INV-187 : cible leader Patroni -> le canary bascule d'abord vers un replica sain, de preference
    pas un noeud encore a redemarrer dans ce passage, puis un votant etcd."""
    rc, deployed, err = run(cd)
    assert rc == 0, err
    args = {n: (cd / f"args.{n}").read_text().split("\n") for n in deployed}

    def opt(n, name):
        return args[n][args[n].index(name) + 1]

    assert [opt(n, "--pending") for n in deployed] == [
        "7 6 5 4 2 1", "6 5 4 2 1", "5 4 2 1", "4 2 1", "2 1", "1", ""]
    assert {opt(n, "--voters") for n in deployed} == {"1 2 3"}


ORDER = [3, 7, 6, 5, 4, 2, 1]


def test_image_promoted_during_a_pass_triggers_a_new_pass_right_away(cd):
    """2026-11-09 : 1.9.3 promue a 08:25Z pendant un passage commence a 08:20Z ; node3 (OVH), node7 et
    node6 sont restes un passage de plus sur l'ancienne version. Promotion au canary node6 (3e appel)."""
    rc, deployed, err = run(cd, promote=(6,))
    assert rc == 0, err
    assert deployed == ORDER + ORDER  # 2e passage : le vrai canary sort aussitot sur un noeud deja a jour
    new = (cd / "digest").read_text().strip()
    assert new != DIGEST
    assert all((cd / "state" / f"node{n}.digest").read_text().strip() == new for n in IP)
    assert (cd / "state" / "orchestrate.state").read_text().split("\t")[:2] == [new, "success"]
    assert f"nouvelle image promue pendant le passage ({new}) : nouveau passage tout de suite" in err.lower()


def test_new_pass_bounded_to_three_per_tick(cd):
    """Garde-fou : une image promue a chaque passage (node1 = dernier canary) -> 3 passages, puis le tick suivant."""
    rc, deployed, err = run(cd, promote=(1, 1, 1, 1))
    assert rc == 0, err
    assert deployed == ORDER * 3
    assert "3 passages faits, repris au prochain tick" in err


def pending_of(h, node):
    args = (h / f"args.{node}").read_text().split("\n")
    return args[args.index("--pending") + 1]


@pytest.mark.parametrize("primary, order", [(3, [7, 6, 5, 4, 2, 3, 1]), (2, ORDER), (1, ORDER), (None, ORDER)])
def test_patroni_leader_deployed_next_to_last(cd, primary, order):
    """INV-197 : 2026-11-13, node3 leader et premier du passage -> sa bascule (INV-187) ne pouvait aller que
    vers un noeud encore sur l'ancienne image (node1), dont le backend n'est jamais devenu primary. Le leader
    passe avant-dernier : node1 reste la fin de chaine, sa bascule va vers un noeud deja a jour."""
    rc, deployed, err = run(cd, primary=primary)
    assert (rc, deployed) == (0, order), err
    if primary == 3:
        assert pending_of(cd, 3) == "1"


def test_deferred_node_still_counts_as_old_image_for_the_next_canaries(cd):
    """INV-197 : un noeud reporte (pas pret, injoignable) tourne encore l'ancienne image : pas un candidat prefere."""
    rc, deployed, err = run(cd, unready=(7,))
    assert rc == 0, err
    assert pending_of(cd, 6) == "7 5 4 2 1"
    assert pending_of(cd, 1) == "7"
