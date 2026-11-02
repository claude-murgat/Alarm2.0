"""Unit tests (tier 1) — backend/app/logic/write_route.py (INV-173).

Un replica qui relaie une ecriture (INV-044) ou un heartbeat (INV-043) essaie d'abord le peer
dont l'hote est celui du leader Patroni ; sinon l'ordre configure (PEER_TEST_URLS) est garde.
Hotes de prod : node3 (OVH) 10.99.0.1, node1 10.99.0.2, node2 10.99.0.3 (cf .env.prod.node*).
"""
import pytest

from backend.app.logic.write_route import leader_first

pytestmark = pytest.mark.unit

OVH, IZ1, IZ2 = "http://10.99.0.1:8000", "http://10.99.0.2:8000", "http://10.99.0.3:8000"


def _cluster(leader_host, *replica_hosts):
    members = [{"name": "leader", "role": "leader", "state": "running", "host": leader_host}]
    members += [{"name": f"r{i}", "role": "replica", "state": "streaming", "host": h}
                for i, h in enumerate(replica_hosts)]
    return {"members": members}


def test_leader_in_second_position_comes_first_others_keep_their_order():
    assert leader_first([IZ1, OVH, IZ2], _cluster("10.99.0.1", "10.99.0.2", "10.99.0.3")) == [OVH, IZ1, IZ2]


def test_leader_already_first_is_unchanged():
    assert leader_first([OVH, IZ1, IZ2], _cluster("10.99.0.1", "10.99.0.2")) == [OVH, IZ1, IZ2]


def test_leader_host_unknown_to_peers_keeps_configured_order():
    assert leader_first([IZ1, OVH], _cluster("10.99.0.9")) == [IZ1, OVH]


def test_no_leader_keeps_configured_order():
    cluster = {"members": [{"name": "r", "role": "replica", "state": "streaming", "host": "10.99.0.1"}]}
    assert leader_first([IZ1, OVH], cluster) == [IZ1, OVH]


def test_no_cluster_keeps_configured_order():
    assert leader_first([IZ1, OVH], None) == [IZ1, OVH]


def test_ci_same_host_for_everyone_keeps_configured_order():
    peers = ["http://host.docker.internal:8001", "http://host.docker.internal:8002"]
    assert leader_first(peers, _cluster("host.docker.internal", "host.docker.internal")) == peers
