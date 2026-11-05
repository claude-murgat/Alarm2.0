"""INV-167 (amende 2026-11-05) : `start-prod-node.sh` accepte les 7 noeuds de prod ; les non votants
(node4 a node7) ne lancent que Patroni + backend, jamais de conteneur etcd.

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
    env = {**os.environ, "H": str(h), "PATH": f"{h / 'bin'}:{os.environ['PATH']}"}
    env.pop("ADVERTISE_HOST", None)
    p = subprocess.run(["bash", str(SCRIPT), "--pull", str(node)], cwd=h / "opt", env=env,
                       capture_output=True, text=True, timeout=30)
    calls = (h / "docker.log").read_text().splitlines() if (h / "docker.log").exists() else []
    return p.returncode, calls, p.stdout + p.stderr


@pytest.mark.parametrize("node", [1, 2, 3, 4, 5, 6, 7])
def test_each_prod_node_starts_non_voters_without_etcd(h, node):
    rc, calls, out = run(h, node)
    assert rc == 0, out
    services = " --no-deps patroni backend" if node >= 4 else ""
    assert calls == [f"compose --env-file .env.prod.node{node} -p node{node} pull{services.replace(' --no-deps', '')}",
                     f"compose --env-file .env.prod.node{node} -p node{node} up -d{services}"], calls


@pytest.mark.parametrize("node", [6, 7])
def test_node6_node7_env_files_advertise_their_wireguard_ip(node):
    env = (REPO / f".env.prod.node{node}").read_text().splitlines()
    assert f"NODE_NAME=node{node}" in env and f"ADVERTISE_HOST=10.99.0.{node}" in env
    peers = next(l for l in env if l.startswith("PEER_TEST_URLS=")).split("=", 1)[1].split(",")
    others = [f"http://10.99.0.{i}:8000" for i in range(1, 8) if i != node]
    assert sorted(peers) == sorted(others)  # les 6 autres backends, une fois chacun
    assert peers[:3] == ["http://10.99.0.1:8000", "http://10.99.0.2:8000", "http://10.99.0.3:8000"]  # votants d'abord


@pytest.mark.parametrize("node", [1, 2, 3, 4, 5])
def test_existing_nodes_peer_with_node6_and_node7_last(node):
    env = (REPO / f".env.prod.node{node}").read_text().splitlines()
    peers = next(l for l in env if l.startswith("PEER_TEST_URLS=")).split("=", 1)[1].split(",")
    assert peers[-2:] == ["http://10.99.0.6:8000", "http://10.99.0.7:8000"]
    assert len(peers) == 6 and len(set(peers)) == 6
