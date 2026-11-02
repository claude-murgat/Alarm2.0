"""INV-180 : le retour arriere CD charge .env.prod.secrets avant `docker compose up -d`.

Constat NODE3 2026-10-30 : `alarm-cd-rollback.sh` relancait les conteneurs sans charger les
secrets ; docker-compose.yml exige SECRET_KEY (`${SECRET_KEY:?...}`) -> le rollback echouait
apres le re-tag :stable et le noeud restait sur la version cassee.

Le vrai script tourne contre de faux `ssh` / `docker` / `curl` (PATH). Le faux `ssh` execute la
commande distante en local (bash -c) : un depot factice tient lieu de /opt/alarm, le faux
`docker` journalise ses arguments et la SECRET_KEY presente dans son environnement.
"""
import os
import pathlib
import subprocess

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alarm-cd-rollback.sh"
SECRET = "s3cret-du-fichier-env-prod-secrets"

SSH = r"""#!/usr/bin/env bash
cmd=${!#}
echo "$cmd" >> "$H/ssh.log"
exec bash -c "$cmd"
"""
# `image inspect` repond un digest (le tag :stable-prev existe sur la cible).
DOCKER = r"""#!/usr/bin/env bash
printf '%s\t%s\n' "$*" "${SECRET_KEY:-}" >> "$H/docker.log"
[[ "$1 $2" == "image inspect" ]] && echo "ghcr.io/claude-murgat/alarm-backend@sha256:prev"
exit 0
"""
CURL = "#!/usr/bin/env bash\nexit 0\n"


@pytest.fixture
def cd(tmp_path):
    (tmp_path / "bin").mkdir()
    for name, body in (("ssh", SSH), ("docker", DOCKER), ("curl", CURL)):
        f = tmp_path / "bin" / name
        f.write_text(body)
        f.chmod(0o755)
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / ".env.prod.secrets").write_text(f"SECRET_KEY={SECRET}\n")
    return tmp_path


def run(h, node):
    """Rollback du noeud. Retourne (exit code, appels docker [(args, SECRET_KEY recue)], stderr)."""
    env = {**os.environ, "H": str(h), "PATH": f"{h / 'bin'}:{os.environ['PATH']}",
           "REMOTE_REPO": str(h / "remote"), "API_BASE": "http://test", "GATEWAY_KEY": "k"}
    env.pop("SECRET_KEY", None)  # seule source possible : le .env.prod.secrets factice
    p = subprocess.run(["bash", str(SCRIPT), "--node", str(node)], env=env,
                       capture_output=True, text=True, timeout=30)
    calls = [tuple(line.split("\t")) for line in (h / "docker.log").read_text().splitlines()]
    return p.returncode, calls, p.stderr


@pytest.mark.parametrize("node, services", [(3, ""), (4, " --no-deps patroni backend")])
def test_compose_up_receives_secret_key_from_env_prod_secrets(cd, node, services):
    rc, calls, err = run(cd, node)
    assert rc == 0, err
    ups = [call for call in calls if " up -d" in call[0]]
    assert ups == [(f"compose --env-file .env.prod.node{node} -p node{node} up -d{services}", SECRET)]
    # Semantique du rollback conservee : l'image re-taguee :stable, ni pull ni build.
    assert not any(" pull" in args or "--build" in args for args, _ in calls)
