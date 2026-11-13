"""INV-196 : apres chaque bascule planifiee et chaque redemarrage, le canary attend un backend primary.

Incident 2026-11-13 : node3 (leader) bascule vers node1, qui tournait encore l'ancienne image (dernier du
passage) ; aucun backend ne s'est dit primary pendant 18 min, et le passage a continue. Decision du
proprietaire : un backend primary en 60 s, sinon seconde bascule vers un autre replica sain, arret du
passage et email, quoi qu'il arrive.

Le vrai canary tourne contre de faux `ssh`, `curl`, `docker` et mailer (PATH, MAILER). Le faux Patroni tient
l'etat du cluster dans `cluster.json` et applique les bascules demandees. Le backend du leader Patroni se dit
primary sur /health un tour de sondes apres sa promotion (boucle de 5 s du backend), sauf s'il est dans
`stuck` (backend qui ne devient jamais primary). Le faux `ssh` journalise `ssh <ip> <commande>` et execute la
sauvegarde des logs (`docker logs`, faux) dans un dossier temporaire.
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
IP = {"node1": "10.99.0.2", "node2": "10.99.0.3", "node3": "10.99.0.1", "node4": "10.99.0.4",
      "node5": "10.99.0.5", "node6": "10.99.0.6", "node7": "10.99.0.7"}

SSH = r"""#!/usr/bin/env bash
for a in "$@"; do case "$a" in alarm@*) ip=${a#alarm@} ;; esac; done
cmd=${!#}
echo "ssh $ip $cmd" >> "$H/calls.log"
case "$cmd" in
  *"imagetools inspect"*) echo "sha256:remote" ;;
  *"image inspect"*) echo "ghcr.io/claude-murgat/alarm-backend@sha256:local" ;;
  *"docker logs"*) bash -c "$cmd" ;;
  *start-prod-node*) if [[ -f "$H/breaks" ]]; then cat "$H/breaks" >> "$H/stuck"; fi ;;
esac
exit 0
"""
DOCKER = '#!/usr/bin/env bash\necho "log du backend ($*)"\n'
MAILER = '#!/usr/bin/env bash\necho "mail $1" >> "$H/calls.log"\n{ echo "$1"; cat; } > "$H/mail"\n'
CURL = r"""#!/usr/bin/env python3
import json, os, sys
H = os.environ["H"]
NAMES = {ip: name for name, ip in json.loads(os.environ["IPS"]).items()}
args = sys.argv[1:]
url = next(a for a in args if a.startswith("http"))
body = args[args.index("-d") + 1] if "-d" in args else ""
with open(H + "/calls.log", "a") as f:
    f.write("curl %s %s\n" % (url, " ".join(body.split())))
state = json.load(open(H + "/cluster.json"))
members = state["members"]
leader = next(m["name"] for m in members if m["role"] == "leader")
if url.endswith(":8008/cluster"):
    print(json.dumps(state))
elif url.endswith(":8008/switchover"):
    req = json.loads(body)
    for m in members:
        if m["name"] == req["leader"]:
            m.update(role="replica", state="streaming", lag=0)
        if m["name"] == req["candidate"]:
            m.update(role="leader", state="running")
    state["rounds"] = 0
    json.dump(state, open(H + "/cluster.json", "w"))
    print('Successfully switched over to "%s"' % req["candidate"])
elif url.endswith(":8000/health"):
    name = NAMES[url.split("//")[1].split(":")[0]]
    stuck = open(H + "/stuck").read().split() if os.path.exists(H + "/stuck") else []
    primary = name == leader and name not in stuck and state.get("rounds", 1) >= 1
    if name == leader:
        state["rounds"] = state.get("rounds", 1) + 1
        json.dump(state, open(H + "/cluster.json", "w"))
    print(json.dumps({"role": "primary" if primary else "secondary", "cluster_writable": True}))
"""


def member(name, role="replica", lag=0):
    return {"name": name, "role": role, "state": "running" if role == "leader" else "streaming",
            "timeline": 324, "lag": lag}


def leader(name):
    return member(name, "leader")


@pytest.fixture
def h(tmp_path):
    (tmp_path / "bin").mkdir()
    for name, body in (("ssh", SSH), ("curl", CURL), ("docker", DOCKER), ("mailer", MAILER)):
        f = tmp_path / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    return tmp_path


def run(h, node, cluster, pending="", stuck=(), breaks=()):
    """Une etape canary. `stuck` : backends jamais primary ; `breaks` : le deviennent au redemarrage."""
    (h / "cluster.json").write_text(json.dumps({"members": cluster}))
    (h / "stuck").write_text(" ".join(stuck) + " ")
    if breaks:
        (h / "breaks").write_text(" ".join(breaks) + " ")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    env.update(H=str(h), PATH=f"{h / 'bin'}:{os.environ['PATH']}", IPS=json.dumps(IP),
               SWITCHOVER_TIMEOUT="2", PRIMARY_TIMEOUT="2", MAILER=str(h / "bin" / "mailer"),
               CD_LOG_DIR=str(h / "logs"))
    p = subprocess.run(["bash", str(SCRIPT), "--node", str(node), "--soak", "0", "--api-base", "http://test",
                        "--gateway-key", "k", "--pending", pending],
                       env=env, capture_output=True, text=True, timeout=90)
    return p.returncode, (h / "calls.log").read_text().splitlines(), p.stderr


def idx(calls, needle):
    return [i for i, c in enumerate(calls) if needle in c]


def events(calls):
    return [(re.search(r'"kind": "(\w+)"', c)[1], re.search(r'"status": "(\w+)"', c)[1])
            for c in calls if "/api/deployments/events" in c]


def switchovers(calls):
    """(API Patroni appelee, corps) des POST /switchover, dans l'ordre."""
    return [(c.split()[1].split("//")[1].split(":")[0], json.loads(c.split(" ", 2)[2]))
            for c in calls if ":8008/switchover" in c]


def mail(h):
    return (h / "mail").read_text() if (h / "mail").exists() else ""


def test_primary_soon_after_switchover_continues_as_before(h):
    """Cas normal : le backend du nouveau leader se dit primary au tour suivant -> etape inchangee."""
    rc, calls, err = run(h, 3, [leader("node3"), member("node1", lag=10), member("node2"), member("node4")])
    assert rc == 0, err
    assert switchovers(calls) == [("10.99.0.1", {"leader": "node3", "candidate": "node2"})]
    assert idx(calls, "start-prod-node.sh --pull 3")
    assert events(calls) == [("switchover", "success"), ("canary_start", "in_progress"),
                             ("canary_promoted", "success")]
    # Les events vont au nouveau primary (node2), pas a l'ancien leader.
    assert all(c.startswith("curl http://10.99.0.3:8000/") for c in calls if "/api/deployments/events" in c)
    assert mail(h) == ""


def test_no_primary_after_switchover_second_switchover_to_new_image_then_stop(h):
    """Rejeu du 2026-11-13 : le nouveau leader (node2) ne devient jamais primary. Seconde bascule vers un
    noeud deja sur la nouvelle image (node4, non votant) plutot que l'ancien leader node3 (pas redemarre)
    ou node1 (encore a redemarrer) ; primary retrouve, mais le passage s'arrete quand meme."""
    rc, calls, err = run(h, 3, [leader("node3"), member("node1"), member("node2"), member("node4")],
                         pending="1", stuck=("node2",))
    assert rc == 2, err
    assert switchovers(calls) == [("10.99.0.1", {"leader": "node3", "candidate": "node2"}),
                                  ("10.99.0.3", {"leader": "node2", "candidate": "node4"})]
    assert not idx(calls, "start-prod-node"), calls
    # Logs du backend bloque sauves sur sa machine avant la seconde bascule.
    snap = idx(calls, "ssh 10.99.0.3 mkdir")
    assert snap and "docker logs --timestamps --tail 300 node2-backend-1" in calls[snap[0]]
    assert snap[0] < idx(calls, ":8008/switchover")[1]
    m = mail(h)
    assert m.startswith("[ALARME-MURGAT] CD arrete : aucun backend primary apres la bascule de node3 vers node2")
    assert "node2 -> node4" in m and "backend primary retrouve : http://10.99.0.4:8000" in m
    assert events(calls) == [("switchover", "failure")]
    assert idx(calls, "mail ")[0] < idx(calls, "/api/deployments/events")[0]


def test_no_primary_even_after_second_switchover_stop_and_email(h):
    rc, calls, err = run(h, 3, [leader("node3"), member("node1"), member("node2"), member("node4")],
                         pending="1", stuck=("node2", "node4"))
    assert rc == 2, err
    assert len(switchovers(calls)) == 2  # jamais de boucle
    assert not idx(calls, "start-prod-node") and not idx(calls, "compose"), calls
    assert "TOUJOURS AUCUN backend primary" in mail(h)
    assert events(calls) == []  # aucune base inscriptible : seul l'email part


def test_no_primary_after_restart_second_switchover_then_stop(h):
    """Cible replica : le redemarrage fait perdre son primary au leader node1 -> bascule vers node2, arret."""
    rc, calls, err = run(h, 4, [leader("node1"), member("node2"), member("node3", lag=10), member("node4")],
                         breaks=("node1",))
    assert rc == 2, err
    assert len(idx(calls, "start-prod-node")) == 1
    assert switchovers(calls) == [("10.99.0.2", {"leader": "node1", "candidate": "node2"})]
    assert idx(calls, "start-prod-node")[0] < idx(calls, ":8008/switchover")[0]
    m = mail(h)
    assert "aucun backend primary apres le redemarrage de node4" in m
    assert "backend primary retrouve : http://10.99.0.3:8000" in m
    assert ("canary_promoted", "success") not in events(calls)


def test_no_primary_before_restart_target_untouched(h):
    """Jamais de redemarrage tant qu'aucun backend n'ecrit : pas pret (exit 3), rien n'est touche."""
    rc, calls, err = run(h, 4, [leader("node1"), member("node2"), member("node4")], stuck=("node1",))
    assert rc == 3, err
    assert "aucun backend primary" in err
    assert not idx(calls, "start-prod-node") and not idx(calls, "docker logs") and not switchovers(calls)
    assert mail(h) == ""


def test_backend_logs_saved_before_recreate_last_10_kept(h):
    logs = h / "logs"
    logs.mkdir()
    for i in range(12):  # sauvegardes plus anciennes
        f = logs / f"node4-202610{i + 1:02d}T000000Z.log"
        f.write_text("ancien")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    rc, calls, err = run(h, 4, [leader("node1"), member("node2"), member("node4")])
    assert rc == 0, err
    snap = idx(calls, "ssh 10.99.0.4 mkdir")
    assert snap and "docker logs --timestamps --tail 300 node4-backend-1" in calls[snap[0]]
    assert snap[0] < idx(calls, "start-prod-node.sh --pull 4")[0]
    assert len(list(logs.iterdir())) == 10
    new = [p for p in logs.iterdir() if p.read_text() != "ancien"]
    assert len(new) == 1 and re.fullmatch(r"node4-\d{8}T\d{6}Z\.log", new[0].name)
    assert "log du backend (logs --timestamps --tail 300 node4-backend-1)" in new[0].read_text()
