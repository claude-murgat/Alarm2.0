"""INV-187 : le canary ne redemarre jamais le leader Patroni sans l'avoir d'abord fait basculer.

Prod 2026-11-05/06 : chaque etape canary relancait Patroni + backend avec `docker compose up -d`.
Sur le leader, c'etait une perte brutale : ~15-35 s sans ecriture, une nouvelle timeline (285 -> 290
en un jour), et les replicas hors ligne a ce moment revenaient sur l'ancienne timeline, WAL deja
supprimes (re-clonage, INV-168). Decision du proprietaire : bascule planifiee (POST /switchover)
vers un replica sain d'abord ; pas de bascule terminee -> le noeud n'est pas redemarre (exit 3).

Le vrai script tourne contre de faux `ssh` et `curl` (PATH) qui journalisent leurs appels dans le
meme fichier (ordre). Le faux Patroni sert `cluster.json`, puis `after.json` une fois la bascule
demandee (sans `after.json`, la bascule ne se termine jamais).
"""
import json
import os
import pathlib
import re
import subprocess

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alarm-cd-canary.sh"
SSH = r"""#!/usr/bin/env bash
echo "ssh ${!#}" >> "$H/calls.log"
case "${!#}" in
  *"imagetools inspect"*) echo "sha256:remote" ;;
  *"image inspect"*) echo "ghcr.io/claude-murgat/alarm-backend@sha256:local" ;;
esac
exit 0
"""
CURL = r"""#!/usr/bin/env bash
prev=; for a in "$@"; do [[ $prev == -d ]] && body=$a; [[ $a == http* ]] && url=$a; prev=$a; done
echo "curl $url ${body//$'\n'/ }" >> "$H/calls.log"
case "$url" in
  *:8008/cluster) if [[ -f "$H/switched" ]]; then cat "$H/after.json"; else cat "$H/cluster.json"; fi ;;
  *:8008/switchover) [[ -f "$H/after.json" ]] && touch "$H/switched"; echo 'Successfully switched over' ;;
esac
exit 0
"""


def member(name, role="replica", state="streaming", tl=290, lag=0, **extra):
    m = {"name": name, "role": role, "state": state, "timeline": tl, **extra}
    if role != "leader":
        m["lag"] = lag
    return m


def leader(name):
    return member(name, "leader", "running")


@pytest.fixture
def h(tmp_path):
    (tmp_path / "bin").mkdir()
    for name, body in (("ssh", SSH), ("curl", CURL)):
        f = tmp_path / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    return tmp_path


def run(h, node, cluster, after=None, pending=None):
    """Une etape canary. `cluster=None` : etat Patroni illisible. Retourne (exit, appels, stderr)."""
    (h / "cluster.json").write_text(json.dumps({"members": cluster}) if cluster else "")
    if after:
        (h / "after.json").write_text(json.dumps({"members": after}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    env.update(H=str(h), PATH=f"{h / 'bin'}:{os.environ['PATH']}", SWITCHOVER_TIMEOUT="2")
    args = ["bash", str(SCRIPT), "--node", str(node), "--soak", "0", "--api-base", "http://test",
            "--gateway-key", "k"]
    if pending is not None:
        args += ["--pending", pending]
    p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, (h / "calls.log").read_text().splitlines(), p.stderr


def idx(calls, needle):
    return [i for i, c in enumerate(calls) if needle in c]


def events(calls):
    """(kind, status) des POST /api/deployments/events, dans l'ordre."""
    return [(re.search(r'"kind": "(\w+)"', c)[1], re.search(r'"status": "(\w+)"', c)[1])
            for c in calls if "/api/deployments/events" in c]


def switchovers(calls):
    return [json.loads(c.split(" ", 2)[2]) for c in calls if ":8008/switchover" in c]


def test_leader_target_switches_over_then_restarts(h):
    rc, calls, err = run(h, 1, [leader("node1"), member("node2"), member("node3", lag=10), member("node4")],
                         after=[member("node1"), leader("node2"), member("node3"), member("node4")])
    assert rc == 0, err
    assert switchovers(calls) == [{"leader": "node1", "candidate": "node2"}]
    assert idx(calls, "http://10.99.0.2:8008/switchover")  # l'API Patroni du leader (node1)
    restart = idx(calls, "start-prod-node.sh --pull 1")
    assert restart and idx(calls, ":8008/switchover")[0] < restart[0]
    assert events(calls) == [("switchover", "success"), ("canary_start", "in_progress"),
                             ("canary_promoted", "success")]


def test_replica_target_restarts_without_switchover(h):
    rc, calls, err = run(h, 4, [leader("node1"), member("node2"), member("node4")])
    assert rc == 0, err
    assert switchovers(calls) == []
    assert idx(calls, "start-prod-node.sh --pull 4")
    assert [k for k, _ in events(calls)] == ["canary_start", "canary_promoted"]


@pytest.mark.parametrize("case, cluster, posted, msg", [
    # Aucun replica sain : pas streaming, ancienne timeline, `nofailover`.
    ("no_candidate", [leader("node1"), member("node2", state="running"), member("node3", tl=289),
                      member("node4", tags={"nofailover": True})], 0, "aucun replica sain"),
    # Bascule demandee, mais node1 reste leader (pas d'after.json).
    ("timeout", [leader("node1"), member("node2")], 1, "non terminee"),
    ("unreadable", None, 0, "illisible"),
])
def test_leader_without_completed_switchover_is_not_restarted(h, case, cluster, posted, msg):
    rc, calls, err = run(h, 1, cluster)
    assert rc == 3, err  # pas pret : reporte au prochain passage (INV-167)
    assert msg in err
    assert len(switchovers(calls)) == posted
    assert not idx(calls, "start-prod-node") and not idx(calls, "compose"), calls
    assert ("canary_start", "in_progress") not in events(calls)
    if case != "unreadable":
        assert events(calls) == [("switchover", "failure")]


@pytest.mark.parametrize("replicas, pending, chosen", [
    # Un votant etcd (1, 2, 3) avant un non votant, meme mieux synchronise.
    ([member("node2", lag=5_000_000), member("node4", lag=0)], None, "node2"),
    # Entre votants : le plus petit lag.
    ([member("node2", lag=900), member("node3", lag=10)], None, "node3"),
    # Jamais un replica en retard d'une timeline.
    ([member("node2", tl=289), member("node3", lag=50)], None, "node3"),
    # Pas un noeud encore a redemarrer dans ce passage, s'il y a mieux (meme un non votant).
    ([member("node2"), member("node4")], "2", "node4"),
    # Tous a redemarrer : on en prend un quand meme (le plus petit lag).
    ([member("node2", lag=10), member("node3", lag=0)], "2 3", "node3"),
], ids=["voter-first", "lag", "timeline", "not-pending", "all-pending"])
def test_candidate_choice(h, replicas, pending, chosen):
    rc, calls, err = run(h, 1, [leader("node1"), *replicas], pending=pending,
                         after=[member("node1"), leader(chosen)])
    assert rc == 0, err
    assert switchovers(calls) == [{"leader": "node1", "candidate": chosen}]
