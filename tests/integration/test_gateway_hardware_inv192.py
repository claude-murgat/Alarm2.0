"""
Tier 2 integration : INV-192 — GET /api/system/gateways (admin) : etat du modem 4G (regles d'INV-129)
et du contact sec (INV-120, vivant au sens d'INV-122) de chaque passerelle. L'onglet Cluster l'affiche
dans la ligne de la machine de la passerelle (gateway_id = nom de la machine, INV-182).

Les passerelles rapportent par les vrais endpoints internes (modem-status, report-state).
"""
from datetime import datetime

import pytest

pytestmark = pytest.mark.integration

GW_KEY = "test-gateway-key-INV-192"
GW = {"X-Gateway-Key": GW_KEY}
URL = "/api/system/gateways"


def _wipe():
    from sqlalchemy import text
    from backend.app.database import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM system_config WHERE key LIKE 'modem_status%'"))
        db.execute(text("DELETE FROM gateway_states"))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def gw(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", GW_KEY)
    monkeypatch.delenv("GATEWAY_LIVENESS_WINDOW_SECONDS", raising=False)  # defaut INV-122 : 15 s
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200
    _wipe()
    yield client
    client.post("/api/test/reset-clock", params={"peer": "false"})
    client.post("/api/test/reset", params={"peer": "false"})  # alarmes nees d'un contact ouvert
    _wipe()


def _modem(client, gateway_id, **status):
    body = {"gateway_id": gateway_id, "site": "izeron", "registered": True, "tech": "LTE", "rsrp": -95.0, "csq": 20,
            **status}
    r = client.post("/internal/alarms/modem-status", headers=GW, json=body)
    assert r.status_code == 200, r.text


def _contact(client, gateway_id, state, site="izeron"):
    r = client.post("/internal/alarms/report-state", headers=GW,
                    json={"gateway_id": gateway_id, "state": state, "site": site})
    assert r.status_code == 200, r.text


def _advance(client, seconds):
    assert client.post("/api/test/advance-clock", params={"seconds": seconds, "peer": "false"}).status_code == 200


def _gateways(client, headers):
    r = client.get(URL, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _by_id(client, headers):
    return {g["gateway_id"]: g for g in _gateways(client, headers)}


def _last_seen_in_db(gateway_id):
    from backend.app.database import SessionLocal
    from backend.app.models import GatewayState
    db = SessionLocal()
    try:
        return db.query(GatewayState).filter(GatewayState.gateway_id == gateway_id).one().last_seen
    finally:
        db.close()


def test_reserved_to_admins(gw, admin_headers):
    r = gw.post("/api/auth/login", json={"name": "user1", "password": "user123"})
    user1 = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert gw.get(URL).status_code in (401, 403)
    assert gw.get(URL, headers=user1).status_code == 403
    assert gw.get(URL, headers=admin_headers).status_code == 200


def test_modem_and_dry_contact_of_each_gateway(gw, admin_headers):
    """onsite-2 : modem seul ; alarm-3 : modem sans reseau + Arduino au repos ; alarm-1 : Arduino seul,
    contact ouvert. Une passerelle d'avant INV-158 (sans gateway_id) ne designe aucune machine."""
    _modem(gw, "onsite-2", rsrp=-95.0)
    _modem(gw, "alarm-3", registered=False, tech=None, rsrp=None, csq=0)
    _contact(gw, "alarm-3", "closed")
    _contact(gw, "alarm-1", "open", site="beaufort")
    r = gw.post("/internal/alarms/modem-status", headers=GW,
                json={"registered": True, "tech": "LTE", "rsrp": -80.0, "csq": 25})
    assert r.status_code == 200, r.text

    body = _gateways(gw, admin_headers)
    assert [g["gateway_id"] for g in body] == ["alarm-1", "alarm-3", "onsite-2"], body
    by_id = {g["gateway_id"]: g for g in body}

    assert by_id["alarm-1"]["modem"] is None
    assert by_id["alarm-1"]["dry_contact"] == {"state": "open", "last_seen": None}

    m = by_id["alarm-3"]["modem"]
    assert (m["state"], m["bars"]) == ("no_network", 0), m
    assert by_id["alarm-3"]["dry_contact"] == {"state": "closed", "last_seen": None}

    m = by_id["onsite-2"]["modem"]
    assert (m["state"], m["bars"], m["tech"], m["rsrp"]) == ("ok", 3, "LTE", -95.0), m  # -95 dBm : 3 barres
    assert m["updated_at"], m
    assert by_id["onsite-2"]["dry_contact"] is None


def test_modem_and_contact_liveness_are_separate(gw, admin_headers):
    """Contact : vivant 15 s apres son dernier report-state (INV-122), meme si le modem rapporte encore
    (Arduino debranche) ; un contact reste ouvert devient `silent` lui aussi. Modem : `unknown` au-dela de
    180 s apres son dernier etat (INV-129), meme si le contact rapporte toutes les 5 s."""
    _modem(gw, "alarm-4")                              # T0
    _contact(gw, "alarm-4", "closed")
    _contact(gw, "alarm-1", "open", site="beaufort")

    _advance(gw, 10)
    _modem(gw, "alarm-4")                              # T10 : keepalive du modem ; l'Arduino ne rapporte plus
    by_id = _by_id(gw, admin_headers)
    assert by_id["alarm-4"]["dry_contact"] == {"state": "closed", "last_seen": None}
    assert by_id["alarm-1"]["dry_contact"] == {"state": "open", "last_seen": None}

    _advance(gw, 10)                                   # T20 : contacts sans rapport depuis 20 s
    by_id = _by_id(gw, admin_headers)
    for gateway_id in ("alarm-4", "alarm-1"):
        dc = by_id[gateway_id]["dry_contact"]
        assert dc["state"] == "silent", dc
        assert datetime.fromisoformat(dc["last_seen"]) == _last_seen_in_db(gateway_id), dc
    assert by_id["alarm-4"]["modem"]["state"] == "ok"  # son dernier etat date de 10 s

    _advance(gw, 160)                                  # T180 : modem sans etat depuis 170 s
    _contact(gw, "alarm-4", "closed")                  # Arduino rebranche
    g = _by_id(gw, admin_headers)["alarm-4"]
    assert g["modem"]["state"] == "ok", g
    assert g["dry_contact"] == {"state": "closed", "last_seen": None}

    _advance(gw, 20)                                   # T200 : modem sans etat depuis 190 s ; le contact rapporte
    _contact(gw, "alarm-4", "closed")
    g = _by_id(gw, admin_headers)["alarm-4"]
    assert (g["modem"]["state"], g["modem"]["bars"]) == ("unknown", 0), g
    assert g["modem"]["updated_at"], g
    assert g["dry_contact"] == {"state": "closed", "last_seen": None}


def test_liveness_window_is_the_aggregation_one(gw, admin_headers, monkeypatch):
    """Meme fenetre que l'agregation OU (INV-122) : `GATEWAY_LIVENESS_WINDOW_SECONDS`."""
    monkeypatch.setenv("GATEWAY_LIVENESS_WINDOW_SECONDS", "60")
    _contact(gw, "alarm-4", "closed")
    _advance(gw, 30)
    assert _by_id(gw, admin_headers)["alarm-4"]["dry_contact"] == {"state": "closed", "last_seen": None}
    _advance(gw, 40)
    assert _by_id(gw, admin_headers)["alarm-4"]["dry_contact"]["state"] == "silent"


def test_response_unchanged_while_the_contact_keeps_reporting(gw, admin_headers):
    """INV-135 : le direct ne notifie qu'un changement. Un rapport toutes les 5 s au meme etat ne change
    pas la reponse ; un contact qui s'ouvre la change."""
    _modem(gw, "alarm-3")
    _contact(gw, "alarm-3", "closed")
    first = gw.get(URL, headers=admin_headers).content
    for _ in range(3):
        _advance(gw, 5)
        _contact(gw, "alarm-3", "closed")
        assert gw.get(URL, headers=admin_headers).content == first
    _contact(gw, "alarm-3", "open")
    assert gw.get(URL, headers=admin_headers).content != first
