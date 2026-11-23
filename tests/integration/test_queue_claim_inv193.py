"""
Tier 2 integration : INV-193 — plusieurs modems sur un site : chaque SMS et chaque appel part
d'une seule passerelle, qui le reserve sur le serveur avant d'agir.

Incident du 2026-11-12, alarme 1231 (Izeron) : l'appel 949 compose par alarm-3 a 17:11:52 etait
encore dans /internal/calls/pending (marque seulement au compte rendu, 17:12:32) ; alarm-4 l'a lu
et recompose a 17:12:49, onsite-2 tres probablement aussi : 3 appels pour 1 appel en file.

Les passerelles sont jouees ici (gateway_id alarm-3, alarm-4, onsite-2) ; horloge avancee pour
les baux.
"""
import logging
import tempfile
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

GW_KEY = "test-gateway-key-INV-193"
GW = {"X-Gateway-Key": GW_KEY}
PHONE = "+33600000193"
LEASE = {"calls": 180, "sms": 90}  # appel 3 min, SMS 1 min 30
KINDS = ["calls", "sms"]


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]["id"]


@pytest.fixture
def setup(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", GW_KEY)  # lu a chaque requete /internal/*
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200
    admin_h, uid = _login(client, "admin", "admin123")
    r = client.post("/api/alarms/send", headers=admin_h, json={
        "site": "izeron", "title": "INV-193", "message": "m", "severity": "critical"})
    assert r.status_code == 200, r.text
    yield client, admin_h, r.json()["id"], uid
    client.post("/api/test/reset-clock", params={"peer": "false"})
    client.post("/api/test/reset", params={"peer": "false"})


def _queue(client, kind, alarm_id, uid):
    """Un appel (de l'alarme) ou un SMS en file pour Izeron (site vide -> default_site)."""
    if kind == "calls":
        r = client.post("/api/test/insert-call", json={
            "to_number": PHONE, "alarm_id": alarm_id, "user_id": uid, "tts_message": "Alarme."})
    else:
        r = client.post("/api/test/insert-sms", json={"to_number": PHONE, "body": "ALARME Izeron: INV-193"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _pending(client, kind):
    r = client.get(f"/internal/{kind}/pending", headers=GW)
    assert r.status_code == 200, r.text
    return [x["id"] for x in r.json()]


def _claim(client, kind, item, gateway_id):
    return client.post(f"/internal/{kind}/{item}/claim", headers=GW, json={"gateway_id": gateway_id})


def _report(client, kind, item, ok, gateway_id=None):
    """Compte rendu : ok -> no_answer (appel) / envoye (SMS) ; sinon error. gateway_id None =
    passerelle d'avant INV-193 (`/sent` sans corps)."""
    who = {} if gateway_id is None else {"gateway_id": gateway_id}
    if kind == "calls":
        r = client.post(f"/internal/calls/{item}/result", headers=GW,
                        json={"result": "no_answer" if ok else "error", **who})
    elif ok:
        r = client.post(f"/internal/sms/{item}/sent", headers=GW, **({"json": who} if who else {}))
    else:
        r = client.post(f"/internal/sms/{item}/error", headers=GW, json={"error": "AT_ERROR: ", **who})
    assert r.status_code == 200, r.text
    return r.json()


def _row(kind, item):
    """(retries, fait a, claimed_by) en base."""
    from backend.app.database import SessionLocal
    from backend.app.models import CallQueue, SmsQueue
    db = SessionLocal()
    try:
        row = db.get(CallQueue if kind == "calls" else SmsQueue, item)
        return row.retries, row.called_at if kind == "calls" else row.sent_at, row.claimed_by
    finally:
        db.close()


def _advance(client, seconds):
    assert client.post("/api/test/advance-clock", params={"seconds": seconds, "peer": "false"}).status_code == 200


@pytest.mark.parametrize("kind", KINDS)
def test_two_gateways_read_the_same_queue_only_one_claim_wins(setup, kind):
    """Rejeu de l'incident : alarm-3 et alarm-4 lisent la meme file ; alarm-3 reserve, alarm-4 ne
    voit plus l'element et sa reservation (liste lue avant) est refusee : il ne compose pas."""
    client, _, alarm_id, uid = setup
    item = _queue(client, kind, alarm_id, uid)
    assert item in _pending(client, kind)  # alarm-3 et alarm-4 lisent la file
    won = _claim(client, kind, item, "alarm-3")

    assert item not in _pending(client, kind), (
        "alarm-4 voit encore l'element reserve par alarm-3 : il le composerait aussi (incident 2026-11-12)")
    assert won.status_code == 200, won.text
    assert won.json()["claimed"] is True and won.json()["claimed_by"] == "alarm-3"
    lost = _claim(client, kind, item, "alarm-4")
    assert lost.status_code == 200, lost.text
    assert lost.json()["claimed"] is False and lost.json()["claimed_by"] == "alarm-3"

    _report(client, kind, item, ok=True, gateway_id="alarm-3")
    retries, done, claimed_by = _row(kind, item)
    assert done is not None and claimed_by is None, "compte rendu du titulaire : fait, reservation levee"
    assert item not in _pending(client, kind)
    assert _claim(client, kind, item, "onsite-2").json()["claimed"] is False, "deja fait : liste perimee refusee"


@pytest.mark.parametrize("kind", KINDS)
def test_lease_expires_then_another_gateway_takes_the_item(setup, kind):
    """Passerelle morte en plein appel / envoi : apres le bail, l'element revient en file."""
    from backend.app.clock import now as clock_now
    client, _, alarm_id, uid = setup
    item = _queue(client, kind, alarm_id, uid)
    t0 = clock_now()
    won = _claim(client, kind, item, "alarm-3")
    assert won.status_code == 200, won.text
    until = datetime.fromisoformat(won.json()["claimed_until"])
    assert abs((until - t0).total_seconds() - LEASE[kind]) < 5, until

    _advance(client, LEASE[kind] - 10)
    assert item not in _pending(client, kind), "bail encore valide"
    assert _claim(client, kind, item, "alarm-4").json()["claimed"] is False
    _advance(client, 15)
    assert item in _pending(client, kind), "bail expire : l'element doit revenir en file"
    again = _claim(client, kind, item, "alarm-4").json()
    assert again["claimed"] is True and again["claimed_by"] == "alarm-4"


@pytest.mark.parametrize("kind", KINDS)
def test_error_is_replayed_until_3_tries_success_never(setup, kind):
    """INV-148 / INV-064 inchanges : `error` (SMS en echec) repart aussitot, au plus 3 essais ;
    `no_answer` (SMS envoye) ne repart jamais, meme apres le bail."""
    client, _, alarm_id, uid = setup
    failing, done = _queue(client, kind, alarm_id, uid), _queue(client, kind, alarm_id, uid)

    for n, gateway_id in enumerate(("alarm-3", "alarm-4", "onsite-2"), 1):
        assert _claim(client, kind, failing, gateway_id).json()["claimed"] is True, f"essai {n}"
        assert _report(client, kind, failing, ok=False, gateway_id=gateway_id)["retries"] == n
        assert (failing in _pending(client, kind)) is (n < 3), f"apres {n} echec(s)"
    assert _claim(client, kind, failing, "alarm-3").json()["claimed"] is False, "3 essais : abandonne"

    assert _claim(client, kind, done, "alarm-3").json()["claimed"] is True
    _report(client, kind, done, ok=True, gateway_id="alarm-3")
    _advance(client, LEASE[kind] + 10)
    assert done not in _pending(client, kind)
    assert _claim(client, kind, done, "alarm-4").json()["claimed"] is False


@pytest.mark.parametrize("kind", KINDS)
def test_report_from_another_gateway_is_accepted_and_keeps_the_claim(setup, kind, caplog):
    client, _, alarm_id, uid = setup
    item = _queue(client, kind, alarm_id, uid)
    assert _claim(client, kind, item, "alarm-3").json()["claimed"] is True

    with caplog.at_level(logging.WARNING):
        assert _report(client, kind, item, ok=False, gateway_id="onsite-2")["retries"] == 1  # accepte
    assert item not in _pending(client, kind), "reservation d'alarm-3 gardee : pas de nouvel essai pendant son appel"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("alarm-3" in m and "onsite-2" in m for m in warnings), warnings

    _report(client, kind, item, ok=True, gateway_id="alarm-3")
    _, done, claimed_by = _row(kind, item)
    assert done is not None and claimed_by is None, "compte rendu du titulaire : fait, reservation levee"


@pytest.mark.parametrize("kind", KINDS)
def test_gateway_not_yet_updated_works_as_before(setup, kind):
    """Versions melangees : ni reservation ni gateway_id (`/sent` sans corps), formats inchanges."""
    client, _, alarm_id, uid = setup
    item = _queue(client, kind, alarm_id, uid)
    assert item in _pending(client, kind)
    _report(client, kind, item, ok=False)
    assert item in _pending(client, kind)
    _report(client, kind, item, ok=True)
    assert item not in _pending(client, kind)
    assert _row(kind, item)[1] is not None


def test_claim_refused_once_the_alarm_is_resolved(setup):
    """INV-154 : une liste lue avant l'acquittement / la resolution ne fait plus composer."""
    client, admin_h, alarm_id, uid = setup
    item = _queue(client, "calls", alarm_id, uid)
    assert client.post(f"/api/alarms/{alarm_id}/resolve", headers=admin_h).status_code == 200
    assert _claim(client, "calls", item, "alarm-3").json()["claimed"] is False


@pytest.mark.parametrize("kind", KINDS)
def test_claim_needs_gateway_id_key_and_known_item(setup, kind):
    client, _, alarm_id, uid = setup
    item = _queue(client, kind, alarm_id, uid)
    url = f"/internal/{kind}/{item}/claim"
    assert client.post(url, headers=GW, json={}).status_code == 400
    assert client.post(url, headers=GW, json={"gateway_id": "  "}).status_code == 400
    assert client.post(url, json={"gateway_id": "alarm-3"}).status_code == 401
    assert client.post(f"/internal/{kind}/999999/claim", headers=GW, json={"gateway_id": "alarm-3"}).status_code == 404
    assert item in _pending(client, kind), "aucune reservation posee par ces refus"


def test_migration_adds_claim_columns_without_touching_rows(client):
    """Base d'avant INV-193 : colonnes ajoutees, lignes intactes et non reservees."""
    from backend.app.database import Base, run_migrations

    f = tempfile.NamedTemporaryFile(delete=False, suffix=".db", prefix="alarm-inv193-")
    f.close()
    engine = create_engine(f"sqlite:///{f.name}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for table in ("sms_queue", "call_queue"):
            cols = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))]
            for col in ("claimed_by", "claimed_until"):
                if col in cols:
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))
        conn.execute(text("INSERT INTO sms_queue (to_number, body, retries) VALUES ('+33600000193', 'avant', 1)"))
        conn.execute(text("INSERT INTO call_queue (to_number, alarm_id, user_id, tts_message, retries) "
                          "VALUES ('+33600000193', 1, 1, 'avant', 2)"))

    run_migrations(engine)
    run_migrations(engine)  # idempotente

    with engine.connect() as conn:
        for table in ("sms_queue", "call_queue"):
            cols = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))]
            assert {"claimed_by", "claimed_until"} <= set(cols), table
        assert conn.execute(text("SELECT body, retries, claimed_by, claimed_until FROM sms_queue")).one() == \
            ("avant", 1, None, None)
        assert conn.execute(text("SELECT tts_message, retries, claimed_by, claimed_until FROM call_queue")).one() == \
            ("avant", 2, None, None)
