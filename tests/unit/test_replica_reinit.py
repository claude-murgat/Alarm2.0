"""Unit tests (tier 1) — backend/app/logic/replica_reinit.py (INV-168).

Incident prod 2026-10-29 : node4/node5 (Beaufort) eteints 2 jours. Au retour, Patroni les
laisse en state « running » (pas « streaming ») sur la timeline 253 alors que le cluster est
en 256 : le leader a recycle les WAL dont ils avaient besoin. Ils ont servi des donnees
vieilles de 2 jours jusqu'au `patronictl reinit` manuel.

Forme reelle de GET /cluster (patroni/api.py, cluster_as_json) : par membre name, role,
state, api_url, host, port, timeline (optionnel), lag ; `pause: true` en maintenance.
"""
from datetime import datetime, timedelta

import pytest

from backend.app.logic.replica_reinit import decide_reinit, reinit_after, reinit_url

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 29, 12, 0, 0)
AFTER = timedelta(minutes=10)
INCIDENT = {"state": "running", "timeline": 253}  # node4/node5 au retour de Beaufort


def _m(name, role="replica", state="streaming", timeline=256):
    n = name[-1]
    m = {"name": name, "role": role, "state": state, "api_url": f"http://10.99.0.{n}:8008/patroni",
         "host": f"10.99.0.{n}", "port": 5432, "lag": 0}
    if timeline is not None:
        m["timeline"] = timeline
    return m


def _prod(leader_state="running", leader_timeline=256, **replicas):
    """Cluster de prod : node3 leader (OVH), 4 replicas streaming sauf ceux donnes."""
    members = [_m("node3", role="leader", state=leader_state, timeline=leader_timeline)]
    members += [replicas.get(n, _m(n)) for n in ("node1", "node2", "node4", "node5")]
    return {"members": members}


def _decide(cluster, stuck_since=None, last_handled=None):
    return decide_reinit(cluster, stuck_since or {}, last_handled or {}, NOW, AFTER)


def test_replica_stuck_10_min_is_reinitialized():
    d = _decide(_prod(node4=_m("node4", **INCIDENT)), {"node4": NOW - AFTER})
    assert d.target["name"] == "node4"
    assert d.reason == "state=running timeline=253 leader_timeline=256"
    assert d.blocked == ()
    assert d.stuck_since == {"node4": NOW - AFTER}


def test_stuck_less_than_10_min_waits():
    since = {"node4": NOW - AFTER + timedelta(seconds=1)}
    d = _decide(_prod(node4=_m("node4", **INCIDENT)), since)
    assert (d.target, d.reason, d.blocked) == (None, None, ())
    assert d.stuck_since == since


def test_first_stuck_observation_starts_the_count():
    d = _decide(_prod(node4=_m("node4", **INCIDENT)))
    assert d.stuck_since == {"node4": NOW}
    assert d.target is None


def test_streaming_replica_catching_up_is_not_stuck():
    """Rattrapage normal : streaming, meme timeline, lag (en baisse) non nul."""
    lagging = _m("node4")
    lagging["lag"] = 50_000_000
    d = _decide(_prod(node4=lagging), {"node4": NOW - timedelta(hours=1)})
    assert d.stuck_since == {}
    assert d.target is None


def test_streaming_on_an_old_timeline_is_stuck():
    d = _decide(_prod(node4=_m("node4", timeline=253)), {"node4": NOW - AFTER})
    assert d.target["name"] == "node4"
    assert d.reason == "state=streaming timeline=253 leader_timeline=256"


@pytest.mark.parametrize("state", ["running", "stopped", "in archive recovery", "starting"])
def test_any_state_but_streaming_is_stuck(state):
    d = _decide(_prod(node4=_m("node4", state=state)), {"node4": NOW - AFTER})
    assert d.target["name"] == "node4"


def test_missing_timeline_is_not_compared():
    """`timeline` est optionnel dans /cluster : seul le state compte alors."""
    since = {"node4": NOW - AFTER}
    assert _decide(_prod(node4=_m("node4", timeline=None)), since).target is None
    assert _decide(_prod(leader_timeline=None, node4=_m("node4", timeline=253)), since).target is None


def test_recovered_replica_resets_the_count():
    d = _decide(_prod(), {"node4": NOW - timedelta(hours=1)})
    assert d.stuck_since == {}


def test_absent_member_is_forgotten_and_not_handled():
    cluster = _prod()
    cluster["members"] = [m for m in cluster["members"] if m["name"] != "node4"]
    d = _decide(cluster, {"node4": NOW - timedelta(hours=1)})
    assert d.stuck_since == {}
    assert (d.target, d.blocked) == (None, ())


def test_leader_is_never_reinitialized():
    """Le leader n'est jamais « streaming » (rien a suivre) : il ne doit jamais etre candidat."""
    d = _decide(_prod(), {"node3": NOW - timedelta(hours=1)})
    assert d.target is None
    assert d.stuck_since == {}


def test_sync_standby_is_handled():
    d = _decide(_prod(node4=_m("node4", role="sync_standby", **INCIDENT)), {"node4": NOW - AFTER})
    assert d.target["name"] == "node4"


@pytest.mark.parametrize("leader_state", ["stopped", "starting", "crashed"])
def test_nothing_when_leader_not_running(leader_state):
    d = _decide(_prod(leader_state=leader_state, node4=_m("node4", **INCIDENT)), {"node4": NOW - AFTER})
    assert (d.target, d.blocked, d.stuck_since) == (None, (), {})


def test_nothing_without_leader():
    cluster = {"members": [_m("node1"), _m("node4", **INCIDENT)]}
    d = _decide(cluster, {"node4": NOW - AFTER})
    assert (d.target, d.blocked, d.stuck_since) == (None, (), {})


def test_nothing_when_cluster_paused():
    """`patronictl pause` = maintenance manuelle en cours : on ne touche a rien."""
    cluster = _prod(node4=_m("node4", **INCIDENT))
    cluster["pause"] = True
    d = _decide(cluster, {"node4": NOW - AFTER})
    assert (d.target, d.blocked, d.stuck_since) == (None, (), {})


def test_one_reinit_per_member_per_hour():
    cluster = _prod(node4=_m("node4", **INCIDENT))
    since = {"node4": NOW - timedelta(hours=2)}
    assert _decide(cluster, since, {"node4": NOW - timedelta(minutes=59, seconds=59)}).target is None
    assert _decide(cluster, since, {"node4": NOW - timedelta(minutes=60)}).target["name"] == "node4"


def test_one_reinit_per_tick_then_the_next():
    """Incident : node4 ET node5 bloques (2 replicas sur 4, pas plus de la moitie)."""
    cluster = _prod(node4=_m("node4", **INCIDENT), node5=_m("node5", **INCIDENT))
    since = {"node4": NOW - AFTER, "node5": NOW - AFTER}
    assert _decide(cluster, since).target["name"] == "node4"
    assert _decide(cluster, since, {"node4": NOW}).target["name"] == "node5"


def test_more_than_half_stuck_blocks_reinit():
    """3 replicas sur 4 bloques : probleme cote leader ou reseau, email seul."""
    stuck = _m("node1", **INCIDENT), _m("node4", **INCIDENT), _m("node5", **INCIDENT)
    cluster = _prod(node1=stuck[0], node4=stuck[1], node5=stuck[2])
    d = _decide(cluster, {n: NOW - AFTER for n in ("node1", "node4", "node5")})
    assert d.target is None
    assert d.blocked == ("node1", "node4", "node5")


def test_guard_counts_replicas_stuck_since_this_tick():
    """2 bloques depuis 10 min + 1 depuis ce tick = 3 sur 4 : garde-fou quand meme."""
    cluster = _prod(node1=_m("node1", **INCIDENT), node4=_m("node4", **INCIDENT), node5=_m("node5", **INCIDENT))
    d = _decide(cluster, {"node4": NOW - AFTER, "node5": NOW - AFTER})
    assert d.target is None
    assert d.blocked == ("node4", "node5")


def test_guard_silent_while_nothing_is_due():
    """Glitch general (ex. juste apres un failover) : rien de du, donc ni reinit ni email."""
    cluster = _prod(node1=_m("node1", state="starting"), node4=_m("node4", **INCIDENT),
                    node5=_m("node5", **INCIDENT))
    d = _decide(cluster)
    assert (d.target, d.blocked) == (None, ())


def test_guard_respects_the_hourly_cooldown():
    cluster = _prod(node1=_m("node1", **INCIDENT), node4=_m("node4", **INCIDENT), node5=_m("node5", **INCIDENT))
    since = {n: NOW - AFTER for n in ("node1", "node4", "node5")}
    d = _decide(cluster, since, {"node1": NOW, "node4": NOW})
    assert d.blocked == ("node5",)


@pytest.mark.parametrize("raw, expected", [
    (None, timedelta(minutes=10)),
    ("15", timedelta(minutes=15)),
    ("0.5", timedelta(seconds=30)),
    ("abc", timedelta(minutes=10)),
    ("0", timedelta(minutes=10)),
    ("-5", timedelta(minutes=10)),
])
def test_reinit_after_reads_system_config(raw, expected):
    assert reinit_after(raw) == expected


def test_reinit_url_targets_the_member_patroni_api():
    assert reinit_url("http://10.99.0.4:8008/patroni") == "http://10.99.0.4:8008/reinitialize"
