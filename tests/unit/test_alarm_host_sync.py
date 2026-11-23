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


def test_cd_state_dir_declared_only_by_alarm_units():
    """INV-141 (2026-11-09) : systemd redonne recursivement un StateDirectory au User de l'unit qui demarre.
    Units root + units alarm sur /var/lib/alarm/cd -> digests.tsv repassait en root 644 et alarm-cd-pull
    echouait (« Permission non accordee », onsite-1 et onsite-2)."""
    repo = SCRIPT.parents[1]
    owners = {}
    for unit in sorted((repo / "infra" / "onsite" / "systemd").glob("*.service")):
        lines = [line.strip() for line in unit.read_text().splitlines()]
        if "StateDirectory=alarm/cd" in lines:
            owners[unit.name] = next((line.split("=", 1)[1] for line in lines if line.startswith("User=")), "root")
    assert "alarm-cd-pull.service" in owners
    assert {u: o for u, o in owners.items() if o != "alarm"} == {}
    for script in ("alarm-host-sync.sh", "alarm-gateway-sync.sh"):  # units root : creent le dossier eux-memes
        assert 'mkdir -p "$STATE_DIR"' in (repo / "scripts" / script).read_text(), script


# --- INV-141 etendu a NODE3 (2026-11-13) : installeur choisi par HOST_INSTALLER, verrou de l'orchestrateur ---

def run_with(r, **extra):
    env = {**ENV, "REPO": str(r["work"]), "STATE_DIR": str(r["tmp"] / "state"), "TARGET_SHA": r["target"], **extra}
    return subprocess.run(["bash", str(r["script"])], env=env, capture_output=True, text=True)


def test_host_installer_replaces_the_site_installer(repos):
    """NODE3 : le drop-in fixe HOST_INSTALLER ; l'installeur du site (pile modem, sudoers) n'est jamais lance."""
    (repos["work"] / "fake-node3.sh").write_text('echo "NODE3-INSTALLER $1"\n')  # non suivi : garde au checkout
    p = run_with(repos, HOST_INSTALLER="fake-node3.sh")
    assert p.returncode == 0, p.stderr
    assert git(repos["work"], "rev-parse", "HEAD") == repos["target"]
    assert "NODE3-INSTALLER --auto" in p.stdout.splitlines()
    assert "INSTALL" not in p.stdout.splitlines()


def test_orchestrator_lock_held_touches_nothing(repos):
    """Passage CD en cours (verrou pris) : ni checkout ni installeur, exit 1, nouvel essai au tick suivant."""
    import fcntl
    state = repos["tmp"] / "state"
    state.mkdir()
    lock = state / "orchestrate.lock"
    lock.write_text("")
    with open(lock) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        p = run_with(repos, LOCK_WAIT="1")
    assert p.returncode == 1
    assert "passage CD en cours" in p.stderr
    assert git(repos["work"], "rev-parse", "HEAD") == repos["base"]
    assert "INSTALL" not in p.stdout.splitlines()


def test_free_orchestrator_lock_is_used_but_never_rewritten(repos):
    """Verrou libre : deploye ; le fichier du verrou (a alarm) n'est ni tronque ni recree par root."""
    state = repos["tmp"] / "state"
    state.mkdir()
    lock = state / "orchestrate.lock"
    lock.write_text("x")
    ino = lock.stat().st_ino
    p = run_with(repos, LOCK_WAIT="1")
    assert p.returncode == 0, p.stderr
    assert git(repos["work"], "rev-parse", "HEAD") == repos["target"]
    assert lock.read_text() == "x" and lock.stat().st_ino == ino
