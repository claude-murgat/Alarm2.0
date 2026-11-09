"""INV-190 : promotion `:stable` des le build quand la PR a deja ete testee sur le meme arbre.

Release 1.9.5 (2026-11-09) : PR #321 (VERSION seul, CI verte sur sa tete, a jour de master) mergee a
14:37:03Z, CD Build fini a 14:37:32Z, mais `:stable` attendait la CI master (~35 min) : ~1 h avant que
node3 annonce la version. La decision (`.github/scripts/promote_decision.py`) tourne ici contre de
fausses reponses de l'API GitHub et de git.
"""
import importlib.util
import pathlib
import re

import pytest

# no_mutation : script CI, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("promote_decision", ROOT / ".github" / "scripts" / "promote_decision.py")
pd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pd)

MERGE = "25aabf4bb62470f17dfe0af1e4b260136309cdab"  # 1.9.5 sur master
HEAD = "ec9e4d721f5bf6609cffd559ba6c394051b84e6c"   # tete de la PR #321
OLD = "5cd3fde" + "0" * 33                          # :stable avant la release
TREE = "916f254e65b7c784355e6374207637d69da67b17"
REQUIRED = [
    "Tier 1 — Unit (logique pure)",
    "Tier 1.5 — Mutation testing (logic pure, blocking)",
    "Tier 2 — Integration (FastAPI TestClient + SQLite)",
    "Tier 3 — E2E cluster (worker 1/2)",
    "Tier 3 — E2E cluster (worker 2/2)",
    "Prod config check (INV-076 — /api/test/* disabled)",
]


def green():
    return [{"name": n, "status": "completed", "conclusion": "success"} for n in REQUIRED] + [
        {"name": "AI review (Sonnet 5.5)", "status": "completed", "conclusion": "skipped"}]


def fake_api(pulls=None, head_tree=TREE, runs=None):
    pulls = [{"number": 321, "merged_at": "2026-11-09T14:37:03Z", "merge_commit_sha": MERGE,
              "head": {"sha": HEAD}}] if pulls is None else pulls
    answers = {
        f"commits/{MERGE}/pulls": pulls,
        f"commits/{MERGE}": {"commit": {"tree": {"sha": TREE}}},
        f"commits/{HEAD}": {"commit": {"tree": {"sha": head_tree}}},
        f"commits/{HEAD}/check-runs?per_page=100": {"check_runs": green() if runs is None else runs},
    }
    return answers.__getitem__


def ancestry(*pairs):
    """is_ancestor(a, b) : vrai pour (a, b) dans pairs et pour a == b (comme git merge-base --is-ancestor)."""
    return lambda a, b: a == b or (a, b) in pairs


def build(api, stable=OLD, is_ancestor=ancestry((OLD, MERGE))):
    return pd.decide("CD Build", MERGE, stable, api, is_ancestor)


def test_same_tree_and_all_checks_green_promotes_now():
    promote, reason = build(fake_api())
    assert promote, reason
    assert "#321" in reason


def test_tree_differs_waits_for_master_ci():
    promote, reason = build(fake_api(head_tree="0" * 40))
    assert not promote
    assert "arbre" in reason


@pytest.mark.parametrize("name", REQUIRED)
def test_one_required_check_failed_waits(name):
    runs = [dict(r, conclusion="failure") if r["name"] == name else r for r in green()]
    promote, reason = build(fake_api(runs=runs))
    assert not promote
    assert name in reason


def test_required_check_still_running_waits():
    runs = [dict(r, status="in_progress", conclusion=None) if r["name"] == REQUIRED[3] else r for r in green()]
    promote, reason = build(fake_api(runs=runs))
    assert not promote
    assert REQUIRED[3] in reason


def test_required_check_missing_waits():
    runs = [r for r in green() if r["name"] != REQUIRED[4]]
    promote, reason = build(fake_api(runs=runs))
    assert not promote
    assert REQUIRED[4] in reason


def test_no_pr_waits():
    promote, reason = build(fake_api(pulls=[]))
    assert not promote


def test_pr_whose_merge_commit_is_another_sha_is_ignored():
    """Ex. PR ouverte dont la branche contient deja ce commit : ce n'est pas elle qui l'a introduit."""
    promote, _ = build(fake_api(pulls=[{"number": 322, "merged_at": None, "merge_commit_sha": "f" * 40,
                                        "head": {"sha": HEAD}}]))
    assert not promote


def test_unreadable_stable_waits_on_build_trigger():
    promote, _ = build(fake_api(), stable="")
    assert not promote


def test_master_ci_trigger_promotes_as_before():
    promote, reason = pd.decide("CI", MERGE, OLD, fake_api(pulls=[]), ancestry((OLD, MERGE)))
    assert promote, reason


def test_master_ci_trigger_after_fast_promotion_does_not_promote_again():
    promote, reason = pd.decide("CI", MERGE, MERGE, fake_api(), ancestry())
    assert not promote
    assert "deja" in reason


def test_older_sha_never_replaces_a_newer_stable():
    """Course reelle : 5cd3fde (CI master ~35 min) finit APRES la promotion rapide de 1.9.5."""
    promote, reason = pd.decide("CI", OLD, MERGE, fake_api(), ancestry((OLD, MERGE)))
    assert not promote
    assert "deja" in reason


def test_required_checks_match_ci_job_names():
    """Garde-fou : un job renomme dans pr.yml ne doit pas rendre la promotion rapide impossible en silence."""
    assert list(pd.REQUIRED_CHECKS) == REQUIRED
    ci = (ROOT / ".github" / "workflows" / "pr.yml").read_text(encoding="utf-8")
    for name in REQUIRED:
        assert "name: " + re.sub(r"worker \d/2", "worker ${{ matrix.worker }}/2", name) in ci, name
