"""
INV-044 — Proxy des écritures `/api/*` reçues par un replica vers le leader.

Généralise INV-043 (heartbeat, cf api/devices.py) à TOUTES les écritures
applicatives. Voir tests/INVARIANTS.md §INV-044.

Une requête mutante (POST/PUT/PATCH/DELETE) sur `/api/*` arrivant sur un nœud
replica est forwardée au leader (peers WireGuard via PEER_TEST_URLS, les mêmes
que le relai heartbeat) qui exécute l'écriture ; la réponse du leader est
relayée telle quelle. Anti-boucle via le header `X-Write-Proxied`. Kill-switch
`WRITE_PROXY_ON_REPLICA` (défaut true).

Pourquoi : l'UI web est servie par un nœud FIXE (souvent le cloud OVH, seul
joignable publiquement) et ne peut pas rotater les URLs comme l'app mobile
(INV-ANDROID-304). Si ce nœud est replica, `POST /api/auth/login` (qui écrit :
refresh token persistant + log_event) échoue en read-only → connexion
impossible. Le relai backend rend l'UI web utilisable quel que soit le primary,
ce qui permet de garder le leader Patroni sur un onsite (bon pour la gateway SMS).

Exclusions (endpoints qui gèrent EUX-MÊMES leur comportement replica) :
- `/api/devices/heartbeat` : relai dédié INV-043 (devices.py), garde son propre
  header anti-boucle `X-Heartbeat-Proxied`.
- `/api/deployments/*`     : plumbing CD (auth gateway-key). `POST /events` a son
  propre garde replica→503 "replica" ; les scripts CD découvrent le leader
  eux-mêmes (`discover_leader()` / GET /health) — ne pas relayer.
- `/api/test/*`            : fan-out propre (test_api.py) + désactivé en prod (INV-076).
- `/internal/*`            : endpoints node-to-node (gateway SMS/voix, alarms_internal).
  Ils ne commencent pas par `/api/` → déjà hors périmètre, jamais relayés.
"""
import os

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .leader_election import is_leader

# Peers WireGuard (mêmes que le relai heartbeat) : les 2 autres backends.
_PEER_URLS = [
    u.strip().rstrip("/")
    for u in os.getenv("PEER_TEST_URLS", "").split(",")
    if u.strip()
]

# Kill-switch (cf INV-044). Défaut true sur tous les nœuds. false = ancien
# comportement (l'écriture atteint l'endpoint local et échoue en read-only).
_PROXY_ON_REPLICA = os.getenv("WRITE_PROXY_ON_REPLICA", "true").lower() == "true"

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Anti-boucle : header posé par le proxy. Header de refus renvoyé par un replica
# qui reçoit une écriture déjà proxifiée (signal "tente le peer suivant").
_PROXIED_HEADER = "X-Write-Proxied"
_DECLINED_HEADER = "X-Write-Proxy-Declined"

# Chemins `/api/*` explicitement exclus du relai générique (cf docstring).
_EXCLUDED_EXACT = frozenset({"/api/devices/heartbeat"})
_EXCLUDED_PREFIXES = ("/api/test", "/api/deployments")


def _is_proxyable_write(request: Request) -> bool:
    """True si la requête est une écriture `/api/*` relayable (hors exclusions)."""
    if request.method not in _WRITE_METHODS:
        return False
    path = request.url.path
    if not path.startswith("/api/"):
        return False  # /internal/* (node-to-node) et le reste : jamais relayés
    if path in _EXCLUDED_EXACT:
        return False
    if any(path.startswith(p) for p in _EXCLUDED_PREFIXES):
        return False
    return True


async def _proxy_write_to_primary(request: Request) -> Response:
    """Forwarde l'écriture vers un peer leader (best-effort, itère les peers).

    Le header `X-Write-Proxied` est l'anti-boucle : un peer replica qui reçoit
    une écriture déjà proxifiée renvoie 503 + `X-Write-Proxy-Declined` au lieu
    de la ré-exécuter — on tente alors le peer suivant. La réponse d'un leader
    (200/401/429/…) est relayée telle quelle. 503 final si aucun leader joignable.
    """
    body = await request.body()
    # Recopie des headers client (Authorization, Content-Type, …) sauf Host /
    # Content-Length (httpx les recalcule à partir du body forwardé).
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length")
    }
    fwd_headers[_PROXIED_HEADER] = "1"

    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"

    async with httpx.AsyncClient(timeout=5.0) as cli:
        for peer in _PEER_URLS:
            try:
                r = await cli.request(
                    request.method,
                    f"{peer}{target}",
                    headers=fwd_headers,
                    content=body,
                )
            except Exception:
                continue  # peer injoignable → suivant
            # Ce peer est aussi un replica (il a refusé) → suivant.
            if r.status_code == 503 and r.headers.get(_DECLINED_HEADER) == "1":
                continue
            # Réponse autoritative du leader → relayer telle quelle.
            return Response(
                content=r.content,
                status_code=r.status_code,
                media_type=r.headers.get("content-type"),
            )
    # Aucun leader joignable (panne cluster / pas de quorum).
    return JSONResponse(
        status_code=503,
        content={"detail": "no primary available for write"},
    )


class ReplicaWriteProxyMiddleware(BaseHTTPMiddleware):
    """INV-044 : relaie les écritures `/api/*` au leader quand ce nœud est replica."""

    async def dispatch(self, request: Request, call_next):
        if not _is_proxyable_write(request):
            return await call_next(request)

        # Leader : exécute localement (cas nominal + dev/CI single-node où le
        # nœud est toujours leader → ce middleware est transparent).
        if is_leader.is_set():
            return await call_next(request)

        # --- Replica ---
        if not _PROXY_ON_REPLICA:
            # Kill-switch : ancien comportement. L'écriture atteint l'endpoint
            # local et échouera en read-only (Patroni replica). Pas de relai.
            return await call_next(request)

        if request.headers.get(_PROXIED_HEADER) == "1":
            # Écriture déjà proxifiée et je ne suis pas leader → refuser (sans
            # ré-exécuter ni re-proxifier) pour que le nœud d'entrée tente le
            # peer suivant. Anti-boucle replica→replica.
            return JSONResponse(
                status_code=503,
                content={"detail": "replica"},
                headers={_DECLINED_HEADER: "1"},
            )

        # Replica nominal : relayer au leader.
        return await _proxy_write_to_primary(request)
