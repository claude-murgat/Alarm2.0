"""Unit tests (tier 1) — INV-196 : etcd compacte son historique ; base d'un voteur a 1 Gio ou plus, ou
alarme NOSPACE -> un email par episode et par niveau.

Constat prod du 2026-11-13 : ~1716 Mo par voteur, tout utilise, 13 cles a la revision 4,69 millions
(aucune compaction configuree). A 2 Gio (quota par defaut) etcd leve NOSPACE et refuse les ecritures.
`status` = reponse de POST /v3/maintenance/status (dbSize en chaine, comme etcd la renvoie).
"""
import pathlib

import pytest
import yaml

from backend.app.logic.etcd_space import ETCD_DB_SIZE_ALERT_BYTES, decide_etcd_space, etcd_level

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
GIB = 1073741824
NOSPACE = "memberID:10276657743932975437 alarm:NOSPACE "  # forme reelle (etcd 3.5.17)


def _st(size=70_000_000, errors=None):
    status = {"version": "3.5.17", "dbSize": str(size), "dbSizeInUse": str(size)}
    if errors is not None:
        status["errors"] = errors
    return status


SMALL, BIG, FULL = _st(), _st(1716 * 1024 * 1024), _st(1716 * 1024 * 1024, [NOSPACE])


# --- docker-compose.yml -------------------------------------------------------------------------

def _compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_etcd_compacts_its_history_every_hour_with_literal_values():
    """Litteraux : aucun .env (prod node1-3, dev, CI, node6/7 du demenagement) ne peut les changer."""
    env = _compose()["services"]["etcd"]["environment"]
    assert env["ETCD_AUTO_COMPACTION_MODE"] == "periodic"
    assert env["ETCD_AUTO_COMPACTION_RETENTION"] == "1h"


def test_compose_passes_etcd_hosts_to_the_backend_and_every_node_env_defines_it():
    env = _compose()["services"]["backend"]["environment"]
    assert "ETCD_HOSTS=${ETCD_HOSTS}" in env
    env_files = [*ROOT.glob(".env.prod.node*"), *ROOT.glob(".env.node*"), *ROOT.glob(".env.ci-w*"), ROOT / ".env.dev"]
    assert len(env_files) == 13
    for path in env_files:
        hosts = [line.split("=", 1)[1] for line in path.read_text().splitlines() if line.startswith("ETCD_HOSTS=")]
        assert hosts and hosts[0].strip(), f"{path.name} sans ETCD_HOSTS"


# --- niveau d'un voteur ---------------------------------------------------------------------------

def test_threshold_is_exactly_1_gib():
    assert ETCD_DB_SIZE_ALERT_BYTES == GIB
    assert etcd_level(_st(GIB - 1)) is None
    assert etcd_level(_st(GIB)) == "size"


def test_nospace_alarm_wins_even_on_a_small_db_and_other_errors_are_ignored():
    assert etcd_level(_st(errors=[NOSPACE])) == "nospace"
    assert etcd_level(FULL) == "nospace"
    assert etcd_level(_st(errors=["etcdserver: no leader"])) is None
    assert etcd_level(_st(GIB, errors=["etcdserver: no leader"])) == "size"


# --- episodes ---------------------------------------------------------------------------------------

def test_healthy_cluster_sends_nothing():
    assert decide_etcd_space([SMALL, SMALL, SMALL], None) == (None, None)


def test_one_email_per_episode():
    assert decide_etcd_space([SMALL, BIG, SMALL], None) == ("size", "size")
    assert decide_etcd_space([BIG, BIG, BIG], "size") == (None, "size")


def test_nospace_after_size_email_sends_a_second_email_once():
    assert decide_etcd_space([BIG, FULL, BIG], "size") == ("nospace", "nospace")
    assert decide_etcd_space([FULL, FULL, FULL], "nospace") == (None, "nospace")
    assert decide_etcd_space([FULL, SMALL, SMALL], None) == ("nospace", "nospace")


def test_disarmed_but_still_big_drops_to_size_silently_then_new_nospace_emails():
    assert decide_etcd_space([BIG, BIG, BIG], "nospace") == (None, "size")
    assert decide_etcd_space([BIG, FULL, BIG], "size") == ("nospace", "nospace")


def test_rearmed_when_every_voter_answers_healthy_then_new_alert_emails():
    assert decide_etcd_space([SMALL, SMALL, SMALL], "nospace") == (None, None)
    assert decide_etcd_space([SMALL, SMALL, SMALL], "size") == (None, None)
    assert decide_etcd_space([SMALL, BIG, SMALL], None) == ("size", "size")


def test_unreachable_voter_keeps_the_episode_but_does_not_hide_another_voter():
    assert decide_etcd_space([SMALL, None, SMALL], "size") == (None, "size")
    assert decide_etcd_space([SMALL, None, SMALL], None) == (None, None)
    assert decide_etcd_space([None, BIG, SMALL], None) == ("size", "size")
    assert decide_etcd_space([None, FULL, SMALL], "size") == ("nospace", "nospace")


def test_all_voters_unreachable_changes_nothing():
    assert decide_etcd_space([None, None, None], "size") == (None, "size")
    assert decide_etcd_space([None, None, None], None) == (None, None)
