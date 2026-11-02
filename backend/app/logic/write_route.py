"""Logique pure : ordre des peers quand un replica relaie une ecriture au leader (INV-173).

Avant, les peers (PEER_TEST_URLS) etaient essayes dans l'ordre du fichier d'env : un noeud mort en
tete coutait un timeout complet a chaque login ou heartbeat relaye. Le leader vu par le Patroni
local passe en tete ; les autres gardent leur ordre.
"""
from urllib.parse import urlsplit


def leader_first(peers: list[str], cluster: dict | None) -> list[str]:
    """`cluster` = JSON de GET {PATRONI_URL}/cluster (None si injoignable). Le peer dont l'hote est
    celui du membre `leader` passe en tete ; pas de cluster, pas de leader ou hote inconnu des
    peers -> ordre inchange (CI : meme hote host.docker.internal pour tous -> inchange)."""
    leaders = [m.get("host") for m in (cluster or {}).get("members", []) if m.get("role") == "leader"]
    return sorted(peers, key=lambda p: urlsplit(p).hostname not in leaders)
