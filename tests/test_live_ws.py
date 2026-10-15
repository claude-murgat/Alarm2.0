"""WebSocket /api/ws (remplace l'autorefresh 5s de l'UI web).

Contrat : au connect, chaque path surveille est notifie une fois (sync
initiale) ; ensuite seulement quand sa reponse change ; token invalide ->
fermeture 4401 (l'UI deconnecte l'utilisateur).
"""
import json
import os

import pytest
import requests
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

BASE_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
WS_URL = BASE_URL.replace("http", "ws", 1) + "/api/ws"
PATH = "/api/config/system"


def _token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"name": "admin", "password": "admin123"}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


def _changed(ws, timeout):
    return json.loads(ws.recv(timeout=timeout))["changed"]


def test_notifies_initially_then_only_on_change():
    token = _token()
    with connect(WS_URL) as ws:
        ws.send(json.dumps({"token": token, "paths": [PATH]}))
        assert _changed(ws, 10) == PATH
        with pytest.raises(TimeoutError):
            ws.recv(timeout=7)  # 2 ticks sans changement -> silence

        current = requests.get(f"{BASE_URL}{PATH}", headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
        new_email = "live-ws@example.com" if current.get("alert_email") != "live-ws@example.com" else "live-ws2@example.com"
        auth = {"Authorization": f"Bearer {token}"}
        r = requests.post(f"{BASE_URL}{PATH}", json={"key": "alert_email", "value": new_email}, headers=auth, timeout=10)
        r.raise_for_status()
        try:
            assert _changed(ws, 10) == PATH
        finally:
            if current.get("alert_email"):
                requests.post(f"{BASE_URL}{PATH}", json={"key": "alert_email", "value": current["alert_email"]},
                              headers=auth, timeout=10)


def test_invalid_token_closes_4401():
    with connect(WS_URL) as ws:
        ws.send(json.dumps({"token": "invalid", "paths": [PATH]}))
        with pytest.raises(ConnectionClosed) as exc:
            ws.recv(timeout=10)
        assert exc.value.rcvd.code == 4401


def test_non_api_paths_are_ignored():
    with connect(WS_URL) as ws:
        ws.send(json.dumps({"token": _token(), "paths": ["/health", "/api/ws"]}))
        with pytest.raises(TimeoutError):
            ws.recv(timeout=5)
