"""
Tier 2 integration : INV-081 — POST /api/config/escalation/bulk repond des que la chaine est
enregistree ; les push `escalation_update` partent ensuite, hors de la requete.

Bug prod du 2026-11-13 (node3) : enregistrer la chaine depuis l'app affichait « Serveur
injoignable » alors que la chaine etait enregistree. Le handler envoyait les push avant de
repondre : oauth2.googleapis.com injoignable ~15 s (jeton OAuth), puis ~10 envois a des jetons
morts. L'app (OkHttp, lecture 5 s) avait coupe la connexion bien avant la reponse.
"""
import threading
import time

import pytest

pytestmark = pytest.mark.integration

SEND_BLOCK_S = 3  # chaque envoi FCM bloque (Google injoignable)


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]["id"]


@pytest.fixture
def setup(client):
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    uids = {}
    for name, pwd in (("admin", "admin123"), ("user1", "user123"), ("user2", "user123")):
        h, uids[name] = _login(client, name, pwd)
        r = client.post("/api/devices/fcm-token", headers=h,
                        json={"token": f"tok-{name}-inv081", "device_id": f"dev-{name}-inv081"})
        assert r.status_code == 200, r.text
        if name == "admin":
            admin_h = h
    yield admin_h, uids
    client.post("/api/test/reset", params={"peer": "false"})


def test_bulk_answers_without_waiting_for_slow_pushes(client, setup, monkeypatch):
    admin_h, uids = setup
    release = threading.Event()
    sent = []

    def blocking_send(token, title, body, data):
        sent.append((token, title, data.get("escalation_position")))
        release.wait(SEND_BLOCK_S)

    monkeypatch.setattr("backend.app.fcm_service._send_fcm_http", blocking_send)
    chain = [uids["user2"], uids["admin"], uids["user1"]]
    try:
        t0 = time.monotonic()
        r = client.post("/api/config/escalation/bulk", headers=admin_h, json={"user_ids": chain})
        elapsed = time.monotonic() - t0
    finally:
        release.set()

    assert r.status_code == 200, r.text
    assert elapsed < 1, f"reponse apres {elapsed:.1f} s : elle attend les push (l'app coupe a 5 s)"
    assert [e["user_id"] for e in r.json()] == chain
    assert [e["user_id"] for e in client.get("/api/config/escalation").json()] == chain

    client.get("/api/test/last-fcm")  # attend la fin des push en cours (INV-081)
    assert {tok: (title, pos) for tok, title, pos in sent if tok.endswith("-inv081")} == {
        "tok-user2-inv081": ("Vous etes de garde", "1"),
        "tok-admin-inv081": ("Chaine d'escalade modifiee", "2"),
        "tok-user1-inv081": ("Chaine d'escalade modifiee", "3"),
    }


def test_push_failure_is_logged_and_chain_kept(client, setup, monkeypatch, caplog):
    admin_h, uids = setup

    def broken(db, user_ids):
        raise RuntimeError("panne push inv081")

    monkeypatch.setattr("backend.app.api.config._notify_escalation_change", broken)
    chain = [uids["user2"], uids["user1"], uids["admin"]]
    r = client.post("/api/config/escalation/bulk", headers=admin_h, json={"user_ids": chain})
    client.get("/api/test/last-fcm")  # attend la fin des push en cours (INV-081)

    assert r.status_code == 200, r.text
    assert [e["user_id"] for e in client.get("/api/config/escalation").json()] == chain
    assert "panne push inv081" in caplog.text, "echec des push perdu sans trace dans les logs"
