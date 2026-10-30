"""INV-177/178 : le runner de panne controlee (scripts/alarm-chaos.sh) sonde etcd, arme la restauration
SUR LA CIBLE (et la prouve) avant d'arreter quoi que ce soit, et partage le verrou de l'orchestrateur CD.

Le vrai script tourne contre de faux `ssh` (journalise la commande distante et ses options, repond a
`docker ps` et a l'armement) et `curl` (sert /health, :2379/health, /api/cluster, /internal/chaos/next
et /abort depuis des fichiers, enregistre les POST), avec MINUTE_SECONDS=1 pour que la minute de
panne dure une seconde.
"""
import fcntl
import json
import os
import pathlib
import re
import subprocess
import time

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alarm-chaos.sh"

SSH = r"""#!/usr/bin/env bash
for a in "$@"; do case "$a" in alarm@*) ip=${a#alarm@} ;; esac; done
cmd="${@: -1}"
echo "$ip $cmd" >> "$H/ssh.log"
echo "$*" >> "$H/ssh_args.log"
[[ -f "$H/ssh_down" ]] && exit 255
case "$cmd" in
  *"docker ps"*) echo c1; echo c2 ;;
  *setsid*) [[ -f "$H/arm_down" ]] || echo ARMED ;;
  *"docker stop"*) [[ -f "$H/stop_fail_$ip" ]] && exit 1
    [[ -f "$H/abort_on_stop" ]] && cp "$H/abort_on_stop" "$H/abort.json" ;;  # annulation ou alarme apres cet arret
  *"alarm-chaos-net cut"*) [[ -f "$H/cut_drop_$ip" ]] && exit 255; [[ -f "$H/cut_noarm_$ip" ]] && exit 0
    # /health de la cible muet jusqu'a son timer (S/60 s, MINUTE_SECONDS=1) ; cut_noop : coupure sans effet
    [[ -f "$H/cut_noop_$ip" || -f "$H/down_until_$ip" ]] || echo $(( $(date +%s) + ${cmd##* } / 60 )) > "$H/down_until_$ip"
    [[ -f "$H/abort_on_stop" ]] && cp "$H/abort_on_stop" "$H/abort.json"  # annulation ou alarme apres cette coupure
    echo ARMED ;;
  *"alarm-chaos-net restore"*) [[ -f "$H/restore_down" ]] && exit 255; rm -f "$H/down_until_$ip" ;;
esac
exit 0
"""
CURL = r"""#!/usr/bin/env bash
url=""; body=""; method=GET
while (( $# )); do
  case "$1" in
    -X) method=$2; shift ;;
    -d) body=$2; shift ;;
    -H|--max-time) shift ;;
    http*) url=$1 ;;
    -*f*) fail=1 ;;  # -sf, -fsS : une reponse 503 est alors un echec
  esac
  shift
done
echo "$method $url" >> "$H/curl.log"
case "$url" in
  *":2379/health") grep -qxF "$url" "$H/etcd_down" 2>/dev/null && exit 22; echo '{"health":"true"}' ;;
  *":8000/health") ip=${url#http://}; ip=${ip%%:*}
    [[ -f "$H/down_until_$ip" ]] && (( $(date +%s) < $(cat "$H/down_until_$ip") )) && exit 28  # coupe : aucune reponse
    [[ -f "$H/health_503_$ip" ]] && { [[ -n "${fail:-}" ]] && exit 22; echo '{"status":"degraded"}'; exit 0; }
    cat "$H/health.json" ;;
  *"/api/cluster") cat "$H/cluster.json" ;;
  *"/internal/chaos/next") cat "$H/next.json" ;;
  *"/abort") cat "$H/abort.json" 2>/dev/null || echo '{"abort": null}' ;;
  *"/events") echo "$body" >> "$H/posts.log"; echo '{}' ;;
  *) exit 22 ;;
esac
"""
MEMBERS = [{"name": f"node{i}", "role": "leader" if i == 3 else "replica",
            "state": "running" if i == 3 else "streaming"} for i in (1, 2, 3, 4, 5)]
NEXT = {"id": 7, "nodes": ["node4"], "mode": "stop", "minutes": 1, "status": "claimed",
        "voters": ["node1", "node2", "node3"]}
# relance, politique de compose remise, puis desarmement du filet de CE test (id 7) seulement
RESTORE = "docker start c1 c2 && docker update --restart=unless-stopped c1 c2 >/dev/null && { pkill -f '[a]larm-chaos-7$' || true; }"


@pytest.fixture
def h(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "state").mkdir()
    for name, body in (("ssh", SSH), ("curl", CURL)):
        f = tmp_path / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    (tmp_path / "health.json").write_text(json.dumps({"status": "ok", "role": "primary"}))
    (tmp_path / "cluster.json").write_text(json.dumps({"members": MEMBERS}))
    (tmp_path / "next.json").write_text("null")
    return tmp_path


def run(h, **env):
    env = {**os.environ, "H": str(h), "PATH": f"{h / 'bin'}:{os.environ['PATH']}",
           "STATE_DIR": str(h / "state"), "GATEWAY_KEY": "k", "MINUTE_SECONDS": "1", **env}
    p = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stderr


def ssh_log(h):
    return (h / "ssh.log").read_text().splitlines() if (h / "ssh.log").exists() else []


def posted(h):
    return [json.loads(l) for l in (h / "posts.log").read_text().splitlines()] if (h / "posts.log").exists() else []


def claim(h, **fields):
    (h / "next.json").write_text(json.dumps({**NEXT, **fields}))


def test_nothing_pending_no_ssh(h):
    rc, err = run(h)
    assert rc == 0, err
    assert "GET http://10.99.0.1:8000/internal/chaos/next" in (h / "curl.log").read_text()
    assert ssh_log(h) == [] and posted(h) == []


def test_stop_node4_probes_etcd_pins_restart_proves_arming_then_stops_and_restores(h):
    claim(h)
    rc, err = run(h)
    assert rc == 0, err
    curl = (h / "curl.log").read_text()
    for ip in ("10.99.0.2", "10.99.0.3", "10.99.0.1"):  # etcd de node1, node2, node3 (voters de /next)
        assert f"GET http://{ip}:2379/health" in curl
    log = ssh_log(h)
    assert all(l.startswith("10.99.0.4 ") for l in log), log
    cmds = [l.split(" ", 1)[1] for l in log]
    assert cmds[0] == "docker ps -q --filter label=com.docker.compose.project=node4"
    # restart=always (un reboot de l'hote relance les conteneurs) puis filet arme, prouve par ARMED
    assert cmds[1].startswith("docker update --restart=always c1 c2 >/dev/null || exit 1; setsid nohup sh -c ")
    assert re.search(r"sh -c 'sleep 6[01]; docker start c1 c2; docker update --restart=unless-stopped c1 c2' "
                     r"alarm-chaos-7 >/dev/null 2>&1 </dev/null & sleep 1; kill -0 \$! && echo ARMED$", cmds[1]), cmds[1]
    assert cmds[2] == "pgrep -f '[a]larm-chaos-7$' >/dev/null"  # le filet a survecu a la fin de la session
    assert cmds[3] == "docker stop c1 c2"
    assert cmds[4] == RESTORE
    assert len(cmds) == 5
    for line in (h / "ssh_args.log").read_text().splitlines():
        for opt in ("BatchMode=yes", "ConnectTimeout=10", "ServerAliveInterval=5", "ServerAliveCountMax=3"):
            assert opt in line, (opt, line)
    kinds = [p["kind"] for p in posted(h)]
    assert kinds == ["started", "restored"]
    assert posted(h)[0]["details"] == {"leader_before": "node3"}
    assert posted(h)[1]["details"]["leader_after"] == "node3"
    assert isinstance(posted(h)[1]["details"]["restore_seconds"], int)
    assert "POST http://10.99.0.1:8000/internal/chaos/7/events" in (h / "curl.log").read_text()


def test_lock_held_by_cd_no_claim(h):
    claim(h)
    with open(h / "state" / "orchestrate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, err = run(h)
    assert rc == 0, err
    assert "next" not in ((h / "curl.log").read_text() if (h / "curl.log").exists() else "")
    assert ssh_log(h) == [] and posted(h) == []


def test_ssh_failure_reports_failed_and_stops_nothing(h):
    claim(h)
    (h / "ssh_down").write_text("")
    rc, err = run(h)
    assert rc == 1
    assert not any("docker stop" in l for l in ssh_log(h))
    kinds = [p["kind"] for p in posted(h)]
    assert kinds == ["started", "failed"]
    assert "node4" in posted(h)[1]["details"]["reason"]


def test_arming_not_proven_stops_nothing_and_resets_restart_policy(h):
    claim(h)
    (h / "arm_down").write_text("")  # ssh renvoie 0 mais le filet n'a pas demarre : pas de ARMED
    rc, err = run(h)
    assert rc == 1, err
    cmds = [l.split(" ", 1)[1] for l in ssh_log(h)]
    assert not any(c.startswith("docker stop") for c in cmds), cmds
    assert cmds[-1] == RESTORE  # unless-stopped remis (docker start sans effet sur un conteneur en marche)
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    assert "armement" in posted(h)[1]["details"]["reason"] and "node4" in posted(h)[1]["details"]["reason"]


def test_partial_stop_failure_restarts_every_listed_node_at_once(h):
    claim(h, nodes=["node4", "node5"])
    (h / "stop_fail_10.99.0.5").write_text("")
    rc, err = run(h)
    assert rc == 1, err
    log = ssh_log(h)
    after = log[log.index("10.99.0.5 docker stop c1 c2") + 1:]
    assert sorted(after) == [f"10.99.0.4 {RESTORE}", f"10.99.0.5 {RESTORE}"]
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    assert "docker stop KO sur node5" in posted(h)[1]["details"]["reason"]


@pytest.mark.parametrize("mode", ["stop", "netcut"])
def test_etcd_voter_unhealthy_stops_nothing(h, mode):
    claim(h, mode=mode)
    (h / "etcd_down").write_text("http://10.99.0.3:2379/health\n")  # etcd de node2 tombe, Patroni sain
    rc, err = run(h)
    assert rc == 1, err
    assert ssh_log(h) == []
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    reason = posted(h)[1]["details"]["reason"]
    assert "etcd" in reason and "node2" in reason


def test_cancel_or_alarm_during_window_restores_early(h):
    claim(h, minutes=10)  # fenetre de 10 s (MINUTE_SECONDS=1)
    (h / "abort_on_stop").write_text(json.dumps({"abort": "annulé par l'admin"}))  # pendant la fenetre
    t0 = time.monotonic()
    rc, err = run(h)
    assert rc == 0, err
    assert time.monotonic() - t0 < 8, "restauration attendue avant l'echeance"
    assert [p["kind"] for p in posted(h)] == ["started", "early_restore", "restored"]
    assert posted(h)[1]["details"] == {"reason": "annulé par l'admin"}
    assert [l.split(" ", 1)[1] for l in ssh_log(h)][-1] == RESTORE  # relance puis desarmement du filet


@pytest.mark.parametrize("abort_file, stopped", [("abort.json", []), ("abort_on_stop", ["node4"])])
def test_cancel_or_alarm_before_a_docker_stop_stops_nothing_more(h, abort_file, stopped):
    """Abort relu juste avant CHAQUE docker stop : avant le premier (rien d'arrete) ou entre node4 et node5."""
    claim(h, nodes=["node4", "node5"])
    (h / abort_file).write_text(json.dumps({"abort": "alarme en cours"}))
    rc, err = run(h)
    assert rc == 1, err
    name = {"10.99.0.4": "node4", "10.99.0.5": "node5"}
    assert [name[l.split(" ", 1)[0]] for l in ssh_log(h) if l.endswith(" docker stop c1 c2")] == stopped
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    victim = "node5" if stopped else "node4"
    assert posted(h)[1]["details"]["reason"] == f"alarme en cours : rien n'est arrêté sur {victim}"
    listed = ["10.99.0.4", "10.99.0.5"] if stopped else ["10.99.0.4"]
    assert sorted(ssh_log(h)[-len(listed):]) == [f"{ip} {RESTORE}" for ip in listed]  # relances et desarmes


def test_disarm_kills_only_this_tests_armed_restore(h):
    """Le desarmement joue pour de vrai (pkill, faux docker) : le filet de ce test meurt, celui de l'id <id>0 survit."""
    cid = int(f"7{os.getpid()}")  # propre a ce pytest : jamais le filet d'un autre run en parallele
    claim(h, id=cid)
    rc, err = run(h)
    assert rc == 0, err
    restore = ssh_log(h)[-1].split(" ", 1)[1]
    (h / "bin" / "docker").write_text("#!/bin/sh\nexit 0\n")
    (h / "bin" / "docker").chmod(0o755)
    # meme forme que le filet arme (plusieurs commandes : sh garde son argv au lieu d'exec sleep)
    nets = {n: subprocess.Popen(["sh", "-c", "sleep 30; true", n]) for n in (f"alarm-chaos-{cid}", f"alarm-chaos-{cid}0")}
    mine, other = nets.values()
    try:
        time.sleep(0.2)
        assert subprocess.run(["bash", "-c", restore], env={**os.environ, "PATH": f"{h / 'bin'}:{os.environ['PATH']}"}).returncode == 0
        mine.wait(timeout=5)  # desarme
        assert other.poll() is None  # filet d'un autre test intact
    finally:
        for proc in nets.values():
            proc.kill()
            proc.wait()


def test_replica_running_but_not_streaming_is_not_restored(h):
    claim(h)
    stuck = [{**m, "state": "running"} if m["name"] == "node4" else m for m in MEMBERS]  # INV-168
    (h / "cluster.json").write_text(json.dumps({"members": stuck}))
    rc, err = run(h, HEALTHY_TIMEOUT="1")
    assert rc == 1, err
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    assert "node4 pas sain" in posted(h)[1]["details"]["reason"]


# INV-177/178 (mode netcut) : le runner ne touche pas a docker, il delegue au helper root de la cible
# (sudo -n alarm-chaos-net cut T*60 : restauration armee sur place), puis attend seulement le retour.
def test_netcut_node4_delegates_to_root_helper_no_docker(h):
    (h / "next.json").write_text(json.dumps({**NEXT, "mode": "netcut", "minutes": 3}))
    rc, err = run(h)
    assert rc == 0, err
    # cut, puis, la cible revenue, `restore` : desarme son timer s'il attend encore (INV-178)
    assert ssh_log(h) == ["10.99.0.4 sudo -n /usr/local/bin/alarm-chaos-net cut 180",
                          "10.99.0.4 sudo -n /usr/local/bin/alarm-chaos-net restore"]
    for opt in ("BatchMode=yes", "ConnectTimeout=10", "ServerAliveInterval=5", "ServerAliveCountMax=3"):
        assert opt in (h / "ssh_args.log").read_text(), opt
    kinds = [p["kind"] for p in posted(h)]
    assert kinds == ["started", "restored"]
    assert posted(h)[1]["details"]["leader_after"] == "node3"


def test_netcut_site_cuts_each_node(h):
    (h / "next.json").write_text(json.dumps({**NEXT, "mode": "netcut", "nodes": ["node4", "node5"], "minutes": 1}))
    rc, err = run(h)
    assert rc == 0, err
    assert ssh_log(h) == ["10.99.0.4 sudo -n /usr/local/bin/alarm-chaos-net cut 60",
                          "10.99.0.5 sudo -n /usr/local/bin/alarm-chaos-net cut 60",
                          "10.99.0.4 sudo -n /usr/local/bin/alarm-chaos-net restore",
                          "10.99.0.5 sudo -n /usr/local/bin/alarm-chaos-net restore"]
    assert [p["kind"] for p in posted(h)] == ["started", "restored"]


def test_netcut_node3_refused_by_runner_without_ssh(h):
    (h / "next.json").write_text(json.dumps({**NEXT, "mode": "netcut", "nodes": ["node3"]}))
    rc, _ = run(h)
    assert rc == 1
    assert ssh_log(h) == []
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    assert "node3" in posted(h)[1]["details"]["reason"]


CUT60 = "sudo -n /usr/local/bin/alarm-chaos-net cut 60"
NET_RESTORE = "sudo -n /usr/local/bin/alarm-chaos-net restore"


@pytest.mark.parametrize("flag", ["cut_drop", "cut_noarm"])  # SSH coupe apres l'armement / sortie sans ARMED
def test_netcut_cut_not_proven_on_second_node_restores_both_and_fails(h, flag):
    claim(h, mode="netcut", nodes=["node4", "node5"])
    (h / f"{flag}_10.99.0.5").write_text("")
    rc, err = run(h)
    assert rc == 1, err
    assert ssh_log(h) == [f"10.99.0.4 {CUT60}", f"10.99.0.5 {CUT60}", f"10.99.0.4 {NET_RESTORE}", f"10.99.0.5 {NET_RESTORE}"]
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    reason = posted(h)[1]["details"]["reason"]
    assert "node5" in reason and "SSH interrompu" in reason


def test_netcut_early_restore_reaches_target(h):
    claim(h, mode="netcut", minutes=10)  # fenetre de 10 s (MINUTE_SECONDS=1)
    (h / "abort_on_stop").write_text(json.dumps({"abort": "alarme en cours"}))  # pendant la fenetre
    t0 = time.monotonic()
    rc, err = run(h)
    assert rc == 0, err
    assert time.monotonic() - t0 < 8, "restauration attendue avant l'echeance"
    # restore tout de suite, puis de nouveau une fois la cible saine (desarmement, sans effet sur wg0 actif)
    assert ssh_log(h) == ["10.99.0.4 sudo -n /usr/local/bin/alarm-chaos-net cut 600", f"10.99.0.4 {NET_RESTORE}",
                          f"10.99.0.4 {NET_RESTORE}"]
    assert [p["kind"] for p in posted(h)] == ["started", "early_restore", "restored"]


def test_netcut_early_restore_unreachable_waits_for_target_timer_until_deadline(h):
    # Cible injoignable (le runner passe par wg0) : son timer remet wg0 a l'echeance ; l'attente de sante
    # doit courir jusqu'a l'echeance + HEALTHY_TIMEOUT, pas seulement HEALTHY_TIMEOUT apres l'abandon.
    claim(h, mode="netcut", minutes=10)
    (h / "abort_on_stop").write_text(json.dumps({"abort": "annulé par l'admin"}))
    (h / "restore_down").write_text("")
    (h / "down_until_10.99.0.4").write_text(str(int(time.time()) + 5))  # timer de node4 : de retour 5 s apres le lancement
    rc, err = run(h, HEALTHY_TIMEOUT="2")
    assert rc == 0, err
    assert f"10.99.0.4 {NET_RESTORE}" in ssh_log(h)
    assert "timer" in err
    assert "désarmement" in err  # restore final KO : journalise, le resultat reste restored
    assert [p["kind"] for p in posted(h)] == ["started", "early_restore", "restored"]
    assert posted(h)[2]["details"]["restore_seconds"] >= 0


def test_netcut_node_never_healthy_fails_and_restores(h):
    claim(h, mode="netcut")
    (h / "cluster.json").write_text(json.dumps({"members": [m for m in MEMBERS if m["name"] != "node4"]}))
    rc, err = run(h, HEALTHY_TIMEOUT="1")
    assert rc == 1, err
    assert ssh_log(h) == [f"10.99.0.4 {CUT60}", f"10.99.0.4 {NET_RESTORE}"]
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    assert "node4 pas sain" in posted(h)[1]["details"]["reason"]


@pytest.mark.parametrize("abort_file, cut", [("abort.json", []), ("abort_on_stop", ["node4"])])
def test_netcut_cancel_or_alarm_before_a_cut_cuts_nothing_more(h, abort_file, cut):
    """Abort relu juste avant CHAQUE cut (comme avant chaque docker stop) : avant le premier, ou entre node4 et node5."""
    claim(h, mode="netcut", nodes=["node4", "node5"])
    (h / abort_file).write_text(json.dumps({"abort": "alarme en cours"}))
    rc, err = run(h)
    assert rc == 1, err
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    victim = "node5" if cut else "node4"
    assert posted(h)[1]["details"]["reason"] == f"alarme en cours : rien n'est coupé sur {victim}"
    assert ssh_log(h) == ([f"10.99.0.4 {CUT60}", f"10.99.0.4 {NET_RESTORE}"] if cut else [])  # node5 jamais coupe


@pytest.mark.parametrize("answer", ["200", "503"])  # toute reponse HTTP, meme 503, prouve que le tunnel passe
def test_netcut_cut_without_effect_fails_and_restores(h, answer):
    claim(h, mode="netcut", minutes=3)
    (h / "cut_noop_10.99.0.4").write_text("")  # systemctl stop sans effet : /health repond encore
    if answer == "503":
        (h / "health_503_10.99.0.4").write_text("")
    rc, err = run(h, HEALTHY_TIMEOUT="2")
    assert rc == 1, err
    assert ssh_log(h) == ["10.99.0.4 sudo -n /usr/local/bin/alarm-chaos-net cut 180", f"10.99.0.4 {NET_RESTORE}"]
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    reason = posted(h)[1]["details"]["reason"]
    assert "coupure non effective" in reason and "node4" in reason
