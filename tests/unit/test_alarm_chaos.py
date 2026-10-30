"""INV-178 : le runner de panne controlee (scripts/alarm-chaos.sh) arme la restauration SUR LA CIBLE
avant d'arreter quoi que ce soit, et partage le verrou de l'orchestrateur CD.

Le vrai script tourne contre de faux `ssh` (journalise la commande distante, repond a `docker ps`)
et `curl` (sert /health, /api/cluster et /internal/chaos/next depuis des fichiers, enregistre les
POST), avec MINUTE_SECONDS=1 pour que la minute de panne dure une seconde.
"""
import fcntl
import json
import os
import pathlib
import subprocess

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alarm-chaos.sh"

SSH = r"""#!/usr/bin/env bash
for a in "$@"; do case "$a" in alarm@*) ip=${a#alarm@} ;; esac; done
cmd="${@: -1}"
echo "$ip $cmd" >> "$H/ssh.log"
[[ -f "$H/ssh_down" ]] && exit 255
case "$cmd" in *"docker ps"*) echo c1; echo c2 ;; esac
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
  esac
  shift
done
echo "$method $url" >> "$H/curl.log"
case "$url" in
  *"/health") cat "$H/health.json" ;;
  *"/api/cluster") cat "$H/cluster.json" ;;
  *"/internal/chaos/next") cat "$H/next.json" ;;
  *"/events") echo "$body" >> "$H/posts.log"; echo '{}' ;;
  *) exit 22 ;;
esac
"""
MEMBERS = [{"name": f"node{i}", "role": "leader" if i == 3 else "replica",
            "state": "running" if i == 3 else "streaming"} for i in (1, 2, 3, 4, 5)]
NEXT = {"id": 7, "nodes": ["node4"], "mode": "stop", "minutes": 1, "status": "claimed"}


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


def run(h):
    env = {**os.environ, "H": str(h), "PATH": f"{h / 'bin'}:{os.environ['PATH']}",
           "STATE_DIR": str(h / "state"), "GATEWAY_KEY": "k", "MINUTE_SECONDS": "1"}
    p = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stderr


def ssh_log(h):
    return (h / "ssh.log").read_text().splitlines() if (h / "ssh.log").exists() else []


def posted(h):
    return [json.loads(l) for l in (h / "posts.log").read_text().splitlines()] if (h / "posts.log").exists() else []


def test_nothing_pending_no_ssh(h):
    rc, err = run(h)
    assert rc == 0, err
    assert "GET http://10.99.0.1:8000/internal/chaos/next" in (h / "curl.log").read_text()
    assert ssh_log(h) == [] and posted(h) == []


def test_stop_node4_arms_restore_before_stop_then_restarts(h):
    (h / "next.json").write_text(json.dumps(NEXT))
    rc, err = run(h)
    assert rc == 0, err
    log = ssh_log(h)
    assert all(l.startswith("10.99.0.4 ") for l in log), log
    cmds = [l.split(" ", 1)[1] for l in log]
    assert cmds[0] == "docker ps -q --filter label=com.docker.compose.project=node4"
    assert cmds[1].startswith("setsid nohup sh -c 'sleep 61; docker start c1 c2") and cmds[1].endswith("&")
    assert cmds[2] == "docker stop c1 c2"
    assert cmds[3] == "docker start c1 c2"
    assert len(cmds) == 4
    kinds = [p["kind"] for p in posted(h)]
    assert kinds == ["started", "restored"]
    assert posted(h)[0]["details"] == {"leader_before": "node3"}
    assert posted(h)[1]["details"]["leader_after"] == "node3"
    assert isinstance(posted(h)[1]["details"]["restore_seconds"], int)
    assert "POST http://10.99.0.1:8000/internal/chaos/7/events" in (h / "curl.log").read_text()


def test_lock_held_by_cd_no_claim(h):
    (h / "next.json").write_text(json.dumps(NEXT))
    with open(h / "state" / "orchestrate.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, err = run(h)
    assert rc == 0, err
    assert "next" not in ((h / "curl.log").read_text() if (h / "curl.log").exists() else "")
    assert ssh_log(h) == [] and posted(h) == []


def test_ssh_failure_reports_failed_and_stops_nothing(h):
    (h / "next.json").write_text(json.dumps(NEXT))
    (h / "ssh_down").write_text("")
    rc, err = run(h)
    assert rc == 1
    assert not any("docker stop" in l for l in ssh_log(h))
    kinds = [p["kind"] for p in posted(h)]
    assert kinds == ["started", "failed"]
    assert "node4" in posted(h)[1]["details"]["reason"]


# INV-177/178 (mode netcut) : le runner ne touche pas a docker, il delegue au helper root de la cible
# (sudo -n alarm-chaos-net cut T*60 : restauration armee sur place), puis attend seulement le retour.
def test_netcut_node4_delegates_to_root_helper_no_docker(h):
    (h / "next.json").write_text(json.dumps({**NEXT, "mode": "netcut", "minutes": 3}))
    rc, err = run(h)
    assert rc == 0, err
    assert ssh_log(h) == ["10.99.0.4 sudo -n /usr/local/bin/alarm-chaos-net cut 180"]
    kinds = [p["kind"] for p in posted(h)]
    assert kinds == ["started", "restored"]
    assert posted(h)[1]["details"]["leader_after"] == "node3"


def test_netcut_site_cuts_each_node(h):
    (h / "next.json").write_text(json.dumps({**NEXT, "mode": "netcut", "nodes": ["node4", "node5"], "minutes": 1}))
    rc, err = run(h)
    assert rc == 0, err
    assert ssh_log(h) == ["10.99.0.4 sudo -n /usr/local/bin/alarm-chaos-net cut 60",
                          "10.99.0.5 sudo -n /usr/local/bin/alarm-chaos-net cut 60"]
    assert [p["kind"] for p in posted(h)] == ["started", "restored"]


def test_netcut_node3_refused_by_runner_without_ssh(h):
    (h / "next.json").write_text(json.dumps({**NEXT, "mode": "netcut", "nodes": ["node3"]}))
    rc, _ = run(h)
    assert rc == 1
    assert ssh_log(h) == []
    assert [p["kind"] for p in posted(h)] == ["started", "failed"]
    assert "node3" in posted(h)[1]["details"]["reason"]
