"""INV-141 : alarm-host-sync n'ecrase une modification locale de /opt/alarm que si le commit
cible la contient deja (fusion a 3 voies = cible). Sinon refus, rien n'est touche.

Cas reel 2026-10-20 (onsite-1/2) : docker-compose.yml avec le correctif de bind ADVERTISE_HOST
pose a la main (deja dans master) + .env.prod.nodeN avec un bloc SMTP ajoute en fin de fichier.
Le script tourne sur un vrai depot git temporaire ; seuls la lecture du commit promu (docker),
runuser et install.sh sont remplaces.
"""
import os
import pathlib
import subprocess

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]  # subprocess + git, hors backend/app/logic

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alarm-host-sync.sh"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}
SMTP = "\n# SMTP relay\nSMTP_HOST=smtp.gmail.com\n"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], env=ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """origin (bare) avec base A puis cible B ; clone /opt/alarm reste sur A."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "master")
    (src / "compose.yml").write_text("ports:\n  - \"2379:2379\"\nrestart: no\n")
    (src / "node.env").write_text("REGISTRY=ghcr\nIMAGE_TAG=stable\n")
    git(src, "add", "."); git(src, "commit", "-qm", "A")
    base = git(src, "rev-parse", "HEAD")
    # Cible : le correctif de bind + une autre evolution, et le bloc SMTP remonte.
    (src / "compose.yml").write_text("ports:\n  - \"10.99.0.2:2379:2379\"\nrestart: unless-stopped\n")
    (src / "node.env").write_text("REGISTRY=ghcr\nIMAGE_TAG=stable\n" + SMTP)
    git(src, "commit", "-qam", "B")
    target = git(src, "rev-parse", "HEAD")
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(tmp_path / "origin.git")], env=ENV, check=True)
    work = tmp_path / "opt-alarm"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(work)], env=ENV, check=True)
    git(work, "checkout", "-q", "--detach", base)

    src_script = SCRIPT.read_text()
    src_script = src_script.replace(
        "SHA=$(docker image inspect", 'SHA="$TARGET_SHA" # $(docker image inspect', 1)
    src_script = src_script.replace('runuser -u "$OWNER" -- git', "git")
    src_script = src_script.replace('bash "$REPO/infra/onsite/install.sh" --auto', "echo INSTALL")
    script = tmp_path / "host-sync.sh"
    script.write_text(src_script)
    return {"work": work, "base": base, "target": target, "script": script, "tmp": tmp_path}


def run(r):
    env = {**ENV, "REPO": str(r["work"]), "STATE_DIR": str(r["tmp"] / "state"), "TARGET_SHA": r["target"]}
    return subprocess.run(["bash", str(r["script"])], env=env, capture_output=True, text=True)


def test_local_changes_already_in_target_are_deployed(repos):
    w = repos["work"]
    (w / "compose.yml").write_text("ports:\n  - \"10.99.0.2:2379:2379\"\nrestart: no\n")  # correctif a la main
    (w / "node.env").write_text("REGISTRY=ghcr\nIMAGE_TAG=stable\n" + SMTP)              # bloc SMTP local
    p = run(repos)
    assert p.returncode == 0, p.stderr
    assert git(w, "rev-parse", "HEAD") == repos["target"]
    assert git(w, "status", "--porcelain", "--untracked-files=no") == ""
    assert "INSTALL" in p.stdout


def test_local_change_missing_from_target_is_never_overwritten(repos):
    w = repos["work"]
    local = "REGISTRY=ghcr\nIMAGE_TAG=stable\nSMTP_PASS=local-only\n"
    (w / "node.env").write_text(local)
    p = run(repos)
    assert p.returncode == 1
    assert "node.env" in p.stderr
    assert git(w, "rev-parse", "HEAD") == repos["base"]
    assert (w / "node.env").read_text() == local
    assert "INSTALL" not in p.stdout


def test_clean_clone_is_deployed(repos):
    p = run(repos)
    assert p.returncode == 0, p.stderr
    assert git(repos["work"], "rev-parse", "HEAD") == repos["target"]
