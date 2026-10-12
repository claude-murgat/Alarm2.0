"""Garde-fou d'exposition reseau du cluster — logique pure (INV-125).

Les regles iptables de Docker court-circuitent UFW : un port publie sur
`0.0.0.0` reste joignable depuis Internet alors que `ufw status` l'affiche
ferme. Le `docker-compose.yml` publie etcd (2379/2380), PostgreSQL (5432) et
Patroni REST (8008) sur `${ADVERTISE_HOST:-0.0.0.0}` — la seule chose qui ferme
ces trois surfaces non authentifiees en prod est la presence de
`ADVERTISE_HOST` dans un fichier d'env hors repo. Un `.env` qui l'oublie
retombe sur le defaut et republie tout en silence.

Ce module repond a "ce bind est-il sur ?" pour que le bring-up echoue
bruyamment au lieu de republier sans bruit.

Fonction pure : aucun effet de bord (pas de DNS, pas de subprocess).
L'enumeration des adresses de la machine est la responsabilite de l'appelant
(`scripts/check-bind-exposure.py`), qui n'enumere que l'IPv4 — le defaut
`0.0.0.0` du compose ne publie que sur IPv4, donc une IPv6 publique sur la
machine n'est pas une exposition et ne doit pas bloquer le bring-up.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

# Les litteraux de `message=` portent `# pragma: no mutate`. mutmut emballe un
# litteral entier (`"texte"` -> `"XXtexteXX"`), donc une assertion par sous-chaine
# ne peut pas tuer ces mutants : il faudrait comparer la prose au caractere, ce qui
# rendrait les tests cassants a chaque reformulation. Ce qui est teste est le
# contenu OPERATOIRE du message — l'adresse qui expose la machine, via `exposed`
# (cf tests/unit/test_bind_exposure.py). Convention INV-125.

# Valeurs non-IP qui font publier Docker sur toutes les interfaces. Les formes
# litterales (`0.0.0.0`, `::`, `::0`, `[::]`, `0.0.0.0/0`) sont reconnues par
# `is_unspecified` plutot qu'enumerees ici.
NON_IP_WILDCARD_HOSTS = frozenset({"", "*"})


@dataclass(frozen=True)  # pragma: no mutate (INV-125 — frozen=False est equivalent :
# aucun code ne mute ce verdict, il est construit puis lu. Meme traitement que
# ClusterSnapshot dans quorum_detection.py (INV-085).
class BindExposureVerdict:
    safe: bool
    reason: str
    advertise_host: str
    public_addresses: tuple[str, ...]
    message: str


def _normalize(address: str) -> str:
    """Retire la forme CIDR (`51.210.105.102/32`) et les crochets IPv6."""
    # pragma no mutate x2 : `or ""` et le set de `strip` sont inobservables depuis
    # l'API publique. Une entree falsy n'est jamais publiquement routable (elle
    # echoue dans ip_address quel que soit le remplacement), et aucune adresse
    # reelle ne porte de caractere parasite en bord de chaine.
    return (address or "").strip().split("/")[0].strip("[]").strip()  # pragma: no mutate


def is_publicly_routable(address: str) -> bool:
    """True si `address` est une IP litterale globalement routable.

    Tolere la forme CIDR crachee par `ip -o addr show`. Retourne False pour tout
    ce qui n'est pas une IP litterale (nom d'hote, chaine vide) — l'appelant
    distingue ce cas via `is_ip_literal`.
    """
    candidate = _normalize(address)
    if not candidate:
        return False
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


def is_ip_literal(address: str) -> bool:
    try:
        ipaddress.ip_address(_normalize(address))
    except ValueError:
        return False
    return True


def is_wildcard_bind(advertise_host: str) -> bool:
    """True si la valeur fait publier Docker sur toutes les interfaces."""
    if advertise_host in NON_IP_WILDCARD_HOSTS:
        return True
    try:
        return ipaddress.ip_address(_normalize(advertise_host)).is_unspecified
    except ValueError:
        return False


def evaluate_bind_exposure(advertise_host, host_addresses) -> BindExposureVerdict:
    """Verdict sur la publication des surfaces cluster.

    `advertise_host` : valeur de `ADVERTISE_HOST` (None/vide accepte).
    `host_addresses` : adresses portees par la machine (forme nue ou CIDR).
    """
    public = tuple(
        _normalize(addr) for addr in host_addresses if is_publicly_routable(addr)
    )
    host = (advertise_host or "").strip()

    if not public:
        return BindExposureVerdict(
            safe=True,
            reason="no-public-address",
            advertise_host=host,
            public_addresses=(),
            message=(
                "Aucune adresse publiquement routable sur cette machine "  # pragma: no mutate
                "(CI/dev mono-host, ou nœud derriere NAT) : publier etcd/PG/"  # pragma: no mutate
                "Patroni sur toutes les interfaces n'expose rien a Internet."  # pragma: no mutate
            ),
        )

    exposed = ", ".join(public)

    if is_wildcard_bind(host):
        return BindExposureVerdict(
            safe=False,
            reason="wildcard-bind",
            advertise_host=host,
            public_addresses=public,
            message=(
                f"ADVERTISE_HOST={host!r} fait publier etcd (2379/2380), "  # pragma: no mutate
                f"PostgreSQL (5432) et Patroni REST (8008) sur TOUTES les "  # pragma: no mutate
                f"interfaces, dont l'adresse publique {exposed}. Ces trois "  # pragma: no mutate
                f"surfaces sont non authentifiees. Renseigner l'IP WireGuard "  # pragma: no mutate
                f"du nœud (ex: ADVERTISE_HOST=10.99.0.1)."  # pragma: no mutate
            ),
        )

    if is_publicly_routable(host):
        return BindExposureVerdict(
            safe=False,
            reason="public-bind",
            advertise_host=host,
            public_addresses=public,
            message=(
                f"ADVERTISE_HOST={host!r} est une adresse publiquement routable "  # pragma: no mutate
                f"— etcd/PG/Patroni seraient publies directement sur Internet. "  # pragma: no mutate
                f"Renseigner l'IP WireGuard du nœud (ex: 10.99.0.1)."  # pragma: no mutate
            ),
        )

    if not is_ip_literal(host):
        return BindExposureVerdict(
            safe=False,
            reason="non-literal-advertise-host",
            advertise_host=host,
            public_addresses=public,
            message=(
                f"ADVERTISE_HOST={host!r} n'est pas une IP litterale : "  # pragma: no mutate
                f"impossible de verifier sans resolution DNS alors que cette "  # pragma: no mutate
                f"machine porte l'adresse publique {exposed}. En prod "  # pragma: no mutate
                f"cross-machine, ADVERTISE_HOST doit etre l'IP mesh WireGuard "  # pragma: no mutate
                f"litterale du nœud."  # pragma: no mutate
            ),
        )

    return BindExposureVerdict(
        safe=True,
        reason="mesh-bind",
        advertise_host=host,
        public_addresses=public,
        message=(
            f"ADVERTISE_HOST={host} n'est pas routable : etcd/PG/Patroni "  # pragma: no mutate
            f"restent injoignables depuis l'adresse publique {exposed}."  # pragma: no mutate
        ),
    )
