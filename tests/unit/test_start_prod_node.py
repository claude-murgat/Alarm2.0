"""INV-167 (amende 2026-11-05) : `start-prod-node.sh` accepte les noeuds de prod 3 a 7 ; les non votants
(node4, node5) ne lancent que Patroni + backend, jamais de conteneur etcd. Etat final du jour du demenagement (INV-090) : node1/node2 retires, voteurs etcd 3, 6, 7.

node6/node7 = alarm-3/alarm-4 (futurs noeuds d'Izeron). Un etcd lance par erreur sur un non votant
tenterait de demarrer un membre inconnu du cluster etcd. Le vrai script tourne depuis un dossier qui
tient lieu de /opt/alarm (vrais `.env.prod.nodeN` du depot), contre de faux `ip` / `docker` (PATH).
"""
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "start-prod-node.sh"
IP = r"""#!/usr/bin/env bash
case "$*" in
  "-br a show wg0") echo "wg0 UNKNOWN 10.99.0.9/24" ;;
  *) echo "1: lo    inet 127.0.0.1/8 scope host lo"; echo "3: wg0    inet 10.99.0.9/24 scope global wg0" ;;
esac
"""
DOCKER = '#!/usr/bin/env bash\necho "$*" >> "$H/docker.log"\n'


@pytest.fixture
def h(tmp_path):
    (tmp_path / "bin").mkdir()
    for name, body in (("ip", IP), ("docker", DOCKER),
                       ("python3", f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')):
        f = tmp_path / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    (tmp_path / "opt").mkdir()
    for env in REPO.glob(".env.prod.node*"):
        shutil.copy(env, tmp_path / "opt" / env.name)
    (tmp_path / "opt" / ".env.prod.secrets").write_text("SECRET_KEY=une-cle-de-test-assez-longue\n")
    return tmp_path


def run(h, node):
    # Sans les COV_CORE_* de pytest-cov : le python3 lance par le script mesurerait sa couverture sans les
    # branches, et la fusion des mesures (-n auto en CI) echouerait (« Can't combine statement ... with branch »).
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    env.update(H=str(h), PATH=f"{h / 'bin'}:{os.environ['PATH']}")
    env.pop("ADVERTISE_HOST", None)
    p = subprocess.run(["bash", str(SCRIPT), "--pull", str(node)], cwd=h / "opt", env=env,
                       capture_output=True, text=True, timeout=30)
    calls = (h / "docker.log").read_text().splitlines() if (h / "docker.log").exists() else []
    return p.returncode, calls, p.stdout + p.stderr


@pytest.mark.parametrize("node", [3, 4, 5, 6, 7])
def test_each_prod_node_starts_non_voters_without_etcd(h, node):
    rc, calls, out = run(h, node)
    assert rc == 0, out
    services = " --no-deps patroni backend" if node in (4, 5) else ""  # voteurs 3, 6, 7 : etcd compris
    assert calls == [f"compose --env-file .env.prod.node{node} -p node{node} pull{services.replace(' --no-deps', '')}",
                     f"compose --env-file .env.prod.node{node} -p node{node} up -d{services}"], calls


@pytest.mark.parametrize("node", [6, 7])
def test_node6_node7_env_files_advertise_their_wireguard_ip(node):
    env = (REPO / f".env.prod.node{node}").read_text().splitlines()
    assert f"NODE_NAME=node{node}" in env and f"ADVERTISE_HOST=10.99.0.{node}" in env
    peers = next(l for l in env if l.startswith("PEER_TEST_URLS=")).split("=", 1)[1].split(",")
    others = [f"http://10.99.0.{i}:8000" for i in (1, 4, 5, 6, 7) if i != node]
    assert sorted(peers) == sorted(others)  # les 4 autres backends, une fois chacun (plus onsite-1/2)
    assert peers[0] == "http://10.99.0.1:8000"  # node3 cloud d'abord


@pytest.mark.parametrize("node", [3, 4, 5])
def test_existing_nodes_peer_with_node6_and_node7_last(node):
    env = (REPO / f".env.prod.node{node}").read_text().splitlines()
    peers = next(l for l in env if l.startswith("PEER_TEST_URLS=")).split("=", 1)[1].split(",")
    assert peers[-2:] == ["http://10.99.0.6:8000", "http://10.99.0.7:8000"]
    assert len(peers) == 4 and len(set(peers)) == 4  # plus onsite-1/2 (10.99.0.2/.3)


ETCD_HOSTS = "10.99.0.1:2379,10.99.0.6:2379,10.99.0.7:2379"
ETCD_INITIAL_CLUSTER = "node3=http://10.99.0.1:2380,node6=http://10.99.0.6:2380,node7=http://10.99.0.7:2380"


@pytest.mark.parametrize("node", [3, 4, 5, 6, 7])
def test_every_prod_env_lists_the_3_final_etcd_voters(node):
    """INV-090, jour du demenagement : voteurs node3, node6, node7 (Patroni de chaque noeud ;
    ETCD_INITIAL_CLUSTER ne sert qu'au premier demarrage d'un voteur)."""
    env = (REPO / f".env.prod.node{node}").read_text().splitlines()
    assert f"ETCD_HOSTS={ETCD_HOSTS}" in env
    assert f"ETCD_INITIAL_CLUSTER={ETCD_INITIAL_CLUSTER}" in env


@pytest.mark.parametrize("node", [1, 2])
def test_onsite_nodes_are_no_longer_deployed(h, node):
    """onsite-1/2 retires : plus de .env.prod.node1/2, `start-prod-node.sh 1` refuse sans rien lancer."""
    assert not (REPO / f".env.prod.node{node}").exists()
    rc, calls, out = run(h, node)
    assert (rc, calls) == (1, []), out
