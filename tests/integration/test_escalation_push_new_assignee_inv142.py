"""
Tier 2 integration : INV-142 — la boucle d'escalade envoie un push au nouvel assigne au
tick meme de l'escalade.

Bug prod du 2026-10-20 (alarme 817) : escalade timo -> admin a 08:33:31, push envoye a timo
seulement (`notified_user_ids: [4]`). La session est en autoflush=False : la notification du
nouvel assigne, ajoutee sans flush, etait invisible a la relecture qui choisit les
destinataires du push. admin n'etait reveille qu'au palier suivant ou par le SMS (+2 min).
Les tests FCM existants passaient par /api/test/trigger-escalation (qui flush) et ne
voyaient pas la vraie boucle.
"""
import time

import pytest

pytestmark = pytest.mark.integration


def _login(client, name, password):
    r = client.post("/api/auth/login", json={"name": name, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]["id"]


@pytest.fixture
def setup(client):
    assert client.post("/api/test/reset", params={"peer": "false"}).status_code == 200
    assert client.post("/api/test/reset-clock", params={"peer": "false"}).status_code == 200
    admin_h, _ = _login(client, "admin", "admin123")
    users = {}
    for name in ("user1", "user2"):
        h, uid = _login(client, name, "user123")
        r = client.post("/api/devices/fcm-token", headers=h,
                        json={"token": f"tok-{name}-inv142", "device_id": f"dev-{name}-inv142"})
        assert r.status_code == 200, r.text
        users[name] = uid
    # Tick d'escalade a 1 s (INV-084) pour ne pas attendre 10 s.
    r = client.post("/api/config/system", headers=admin_h,
                    json={"key": "escalation_tick_seconds", "value": "1"})
    assert r.status_code == 200, r.text
    yield admin_h, users
    client.post("/api/config/system", headers=admin_h, json={"key": "escalation_tick_seconds", "value": "10"})
    client.post("/api/test/reset-clock", params={"peer": "false"})
    client.post("/api/test/reset", params={"peer": "false"})


def test_loop_escalation_pushes_the_new_assignee(client, setup):
    admin_h, users = setup
    sent = client.post("/api/alarms/send", headers=admin_h, json={
        "site": "izeron", "title": "INV-142", "message": "push au nouvel assigne", "severity": "critical",
        "assigned_user_id": None,
    })
    assert sent.status_code == 200, sent.text
    assert sent.json()["assigned_user_id"] == users["user1"]
    client.post("/api/test/reset-fcm")

    # Delai d'escalade par defaut 15 min : la vraie boucle doit escalader user1 -> user2.
    client.post("/api/test/advance-clock", params={"seconds": 16 * 60, "peer": "false"})
    deadline = time.time() + 15
    while time.time() < deadline:
        alarm = client.get("/api/alarms/active", headers=admin_h).json()[0]
        if alarm["escalation_count"] >= 1:
            break
        time.sleep(0.5)
    assert alarm["assigned_user_id"] == users["user2"], "la boucle d'escalade n'a pas escalade"

    pushed = {f["user_id"] for f in client.get("/api/test/last-fcm").json()}
    assert users["user2"] in pushed, f"le nouvel assigne n'a pas recu de push au tick de l'escalade : {pushed}"
    assert users["user1"] in pushed, "escalade cumulative : l'ancien assigne doit aussi etre relance"
