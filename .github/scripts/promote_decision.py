"""Promouvoir `:stable` maintenant ? Decision de `cd-promote-stable.yml` (INV-190).

Usage : promote_decision.py <workflow declencheur : "CI" | "CD Build"> <sha master> <owner/repo>
Ecrit `promote=true|false` dans $GITHUB_OUTPUT et la raison en `::notice::`.

- Jamais de retour en arriere : sha egal ou ancetre de la revision de `:stable` -> rien.
- "CI" (CI verte sur master) -> promotion, comme avant.
- "CD Build" (images construites, CI master pas encore finie) -> promotion tout de suite seulement si
  le commit a le meme arbre que la tete de la PR qui l'a introduit, et que tous les checks requis de
  cette tete sont verts. Le moindre doute -> rien, la CI master promouvra.
"""
import json
import os
import subprocess
import sys

# Noms des jobs de .github/workflows/pr.yml (workflow "CI"), verifies par tests/unit/test_promote_decision.py.
REQUIRED_CHECKS = (
    "Tier 1 — Unit (logique pure)",
    "Tier 1.5 — Mutation testing (logic pure, blocking)",
    "Tier 2 — Integration (FastAPI TestClient + SQLite)",
    "Tier 3 — E2E cluster (worker 1/2)",
    "Tier 3 — E2E cluster (worker 2/2)",
    "Prod config check (INV-076 — /api/test/* disabled)",
)
STABLE = "ghcr.io/claude-murgat/alarm-backend:stable"
WAIT = " : attente de la CI master"


def decide(trigger, sha, stable_rev, api, is_ancestor):
    """api(path) = JSON de GET /repos/<repo>/<path> ; is_ancestor(a, b) = git merge-base --is-ancestor a b.
    Retourne (promouvoir, raison)."""
    if stable_rev and is_ancestor(sha, stable_rev):
        return False, f":stable est deja sur {stable_rev[:7]}, egal ou plus recent que {sha[:7]}"
    if trigger == "CI":
        return True, f"CI master verte sur {sha[:7]}"
    if not stable_rev:
        return False, "revision de :stable illisible" + WAIT
    prs = [p for p in api(f"commits/{sha}/pulls") if p.get("merged_at") and p.get("merge_commit_sha") == sha]
    if len(prs) != 1:
        return False, f"{len(prs)} PR mergee(s) en {sha[:7]}" + WAIT
    number, head = prs[0]["number"], prs[0]["head"]["sha"]
    # Arbre du commit = `git rev-parse <sha>^{tree}`, lu par l'API (la tete de PR n'est pas dans le checkout).
    if api(f"commits/{sha}")["commit"]["tree"]["sha"] != api(f"commits/{head}")["commit"]["tree"]["sha"]:
        return False, f"{sha[:7]} n'a pas le meme arbre que la tete {head[:7]} de la PR #{number}" + WAIT
    runs = api(f"commits/{head}/check-runs?per_page=100")["check_runs"]
    for name in REQUIRED_CHECKS:
        states = [(r["status"], r["conclusion"]) for r in runs if r["name"] == name]
        if not states:
            return False, f"PR #{number} : check « {name} » absent" + WAIT
        if any(s != ("completed", "success") for s in states):
            return False, f"PR #{number} : check « {name} » {states}" + WAIT
    return True, f"PR #{number} : meme arbre que sa tete {head[:7]}, checks requis verts"


def main(trigger, sha, repo):
    def run(*cmd):
        return subprocess.run(cmd, capture_output=True, text=True)

    def api(path):
        p = run("gh", "api", f"repos/{repo}/{path}")
        if p.returncode:
            raise RuntimeError(f"gh api {path} : {p.stderr.strip()}")
        return json.loads(p.stdout)

    # Revision du commit promu, label OCI pose par cd-build.yml (le meme que lit alarm-host-sync, INV-141).
    stable = run("docker", "buildx", "imagetools", "inspect", STABLE, "--format",
                 '{{index .Image.Config.Labels "org.opencontainers.image.revision"}}').stdout.strip()
    try:
        promote, reason = decide(trigger, sha, stable,
                                 api, lambda a, b: run("git", "merge-base", "--is-ancestor", a, b).returncode == 0)
    except Exception as e:  # API GitHub en erreur : doute
        promote, reason = False, f"{e}" + WAIT
    print(f"::notice::{'Promotion' if promote else 'Pas de promotion'} : {reason}")
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as out:
        out.write(f"promote={'true' if promote else 'false'}\n")


if __name__ == "__main__":
    main(*sys.argv[1:4])
