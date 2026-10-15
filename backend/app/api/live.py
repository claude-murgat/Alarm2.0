"""WebSocket /api/ws : remplace l'autorefresh 5s de l'UI web.

Le client envoie {"token": ..., "paths": [GET /api/... a surveiller]}. Le
serveur rejoue ces GET en interne (memes routes, meme auth, DB locale du
noeud) et n'envoie {"changed": path} que si la reponse a change. Le
navigateur ne recharge donc que les panneaux dont les donnees ont bouge.
Marche sur n'importe quel noeud (replica compris) sans bus inter-noeuds.
"""
import asyncio
import hashlib
import logging

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

TICK_SECONDS = 3
MAX_PATHS = 20
ME_PATH = "/api/auth/me"  # certains GET surveilles sont publics : l'auth est verifiee ici

# Sinon httpx loggue chaque GET interne (paths x clients toutes les 3s).
logging.getLogger("httpx").addFilter(lambda r: "http://live/" not in r.getMessage())


@router.websocket("/api/ws")
async def live(ws: WebSocket):
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        token = str(hello.get("token") or "")
        paths = [p for p in hello.get("paths") or [] if isinstance(p, str)
                 and p.startswith("/api/") and not p.startswith("/api/ws")][:MAX_PATHS]
    except (asyncio.TimeoutError, ValueError, AttributeError):
        await ws.close(code=4400)
        return

    seen: dict[str, str] = {}
    transport = httpx.ASGITransport(app=ws.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://live",
                                 headers={"Authorization": f"Bearer {token}"}) as client:
        receiver = asyncio.create_task(_drain(ws))
        try:
            while not receiver.done():
                for path in [ME_PATH, *paths]:
                    try:
                        r = await client.get(path)
                    except Exception:
                        continue  # erreur transitoire : on retente au tick suivant
                    if path == ME_PATH and r.status_code in (401, 403):
                        await ws.close(code=4401)
                        return
                    if r.status_code != 200 or path == ME_PATH:
                        continue  # 503 & co : pas de notif, l'UI garde ses donnees
                    digest = hashlib.sha256(r.content).hexdigest()
                    if seen.get(path) != digest:
                        seen[path] = digest
                        await ws.send_json({"changed": path})
                await asyncio.wait([receiver], timeout=TICK_SECONDS)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            receiver.cancel()


async def _drain(ws: WebSocket):
    """Lit (et ignore) les messages client ; se termine a la deconnexion."""
    try:
        while True:
            await ws.receive_text()
    except Exception:
        pass
