"""Unit test (tier 1) — garde-fou d'exposition reseau au bring-up du cluster.

Spec : INV-125 (texte propose dans la description de la PR — `tests/INVARIANTS.md`
est en denylist, le catalogue est edite par le proprietaire).

Regle verrouillee ici :

  Sur une machine portant au moins une adresse IPv4 **globalement routable**, le
  bring-up de la stack est **refuse** si `ADVERTISE_HOST` ne pointe pas sur une
  adresse non routable (mesh WireGuard). Sur une machine sans adresse publique
  (CI/dev mono-host, onsite derriere NAT), le bring-up est **toujours autorise**,
  quelle que soit la valeur de `ADVERTISE_HOST`.

Le second alinea est le cas discriminant : le defaut `0.0.0.0` du
`docker-compose.yml` est volontaire en CI/dev (3 nœuds sur un host, ports
differencies) et ne doit jamais etre bloque.

Test pur : stdlib only, aucun container, aucune DB. Le CLI est charge par chemin
(nom de fichier avec tiret, non importable) comme `test_dry_contact_host.py`.
"""
import importlib.util
import pathlib
import sys

import pytest

from backend.app.logic import bind_exposure as _bind

pytestmark = pytest.mark.unit

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_script(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_cli = _load_script("alarm_check_bind_exposure", "scripts/check-bind-exposure.py")


# Nœud cloud OVH (node3) : IP publique directe + IP mesh WG + bridge Docker.
# Forme CIDR volontaire — c'est ce que crache `ip -o -4 addr show`.
PUBLIC_HOST = ["127.0.0.1/8", "51.210.105.102/32", "10.99.0.1/24",
               "51.77.222.190/32", "172.17.0.1/16"]

# DEUX adresses publiques volontairement (node3 + le VPS facade) : avec une seule,
# le separateur de `", ".join(public)` serait inobservable et un mutant qui le
# remplace survivrait. La forme attendue ci-dessous verifie aussi que `exposed`
# est bien construit — un mutant qui le met a None ferait apparaitre "None".
EXPOSED = "51.210.105.102, 51.77.222.190"

# Onsite derriere NAT (LAN 172.16.1.0/24) ou runner CI : aucune IP routable.
# Contient volontairement une entree vide et une entree non-IP : une ligne
# malformee ne doit pas etre comptee comme adresse publique, sinon le garde-fou
# refuserait un bring-up legitime.
PRIVATE_HOST = ["127.0.0.1", "172.16.1.121/24", "", "pas-une-ip",
                "10.99.0.2", "172.17.0.1/16"]


@pytest.mark.parametrize("advertise_host", [None, "", "   ", "0.0.0.0", "0.0.0.0/0",
                                            "::", "::0", "[::]", "*"])
def test_wildcard_bind_on_publicly_routable_machine_is_refused(advertise_host):
    """ADVERTISE_HOST absent/vide/wildcard + IP publique → bring-up refuse.

    C'est le trou exact de PR #194 : un `.env` prod qui oublie `ADVERTISE_HOST`
    retombe sur le defaut `0.0.0.0` du compose et republie etcd/PG/Patroni sur
    Internet en silence.
    """
    verdict = _bind.evaluate_bind_exposure(advertise_host, PUBLIC_HOST)

    assert verdict.safe is False
    assert verdict.reason == "wildcard-bind"
    # L'operateur doit savoir QUELLE adresse l'expose.
    assert "51.210.105.102" in verdict.public_addresses
    assert EXPOSED in verdict.message


@pytest.mark.parametrize("advertise_host", ["10.99.0.1", "10.99.0.2", "192.168.1.5",
                                            "172.16.1.121", "127.0.0.1"])
def test_non_routable_bind_on_publicly_routable_machine_is_allowed(advertise_host):
    """ADVERTISE_HOST = adresse non routable (mesh WG) → bring-up autorise.

    Configuration prod attendue : node3 a une IP publique mais n'annonce que son
    IP WireGuard.
    """
    verdict = _bind.evaluate_bind_exposure(advertise_host, PUBLIC_HOST)

    assert verdict.safe is True
    assert verdict.reason == "mesh-bind"
    assert EXPOSED in verdict.message


@pytest.mark.parametrize("advertise_host", [None, "", "0.0.0.0", "::",
                                            "10.99.0.2", "host.docker.internal"])
def test_machine_without_routable_address_is_always_allowed(advertise_host):
    """Aucune IP routable sur la machine → bring-up toujours autorise.

    Verrou anti-regression CI : la CI lance 3 nœuds sur un host sans
    `ADVERTISE_HOST` (defaut `0.0.0.0` + advertise `host.docker.internal`).
    Ce cas ne doit JAMAIS etre refuse, sinon le garde-fou casse la CI.
    """
    verdict = _bind.evaluate_bind_exposure(advertise_host, PRIVATE_HOST)

    assert verdict.safe is True
    assert verdict.reason == "no-public-address"
    assert verdict.public_addresses == ()


@pytest.mark.parametrize("advertise_host,expected_reason", [
    ("51.210.105.102", "public-bind"),      # bind direct sur l'IP publique
    ("8.8.8.8", "public-bind"),             # n'importe quelle adresse routable
    ("host.docker.internal", "non-literal-advertise-host"),  # env CI utilise en prod
    ("node3", "non-literal-advertise-host"),
])
def test_routable_or_unverifiable_bind_on_public_machine_is_refused(advertise_host,
                                                                   expected_reason):
    """IP publique + ADVERTISE_HOST routable ou non litteral → bring-up refuse.

    Un nom d'hote n'est pas verifiable sans resolution DNS : fail closed. En prod
    cross-machine, `ADVERTISE_HOST` doit etre l'IP mesh litterale.
    """
    verdict = _bind.evaluate_bind_exposure(advertise_host, PUBLIC_HOST)

    assert verdict.safe is False
    assert verdict.reason == expected_reason
    if expected_reason == "non-literal-advertise-host":
        assert EXPOSED in verdict.message


@pytest.mark.parametrize("advertise_host,addresses,expected_code", [
    ("0.0.0.0", ["51.210.105.102"], 1),   # prod expose  → refus
    ("", ["51.210.105.102"], 1),          # ADVERTISE_HOST oublie → refus
    ("10.99.0.1", ["51.210.105.102"], 0),  # prod conforme → OK
    ("0.0.0.0", ["172.16.1.121"], 0),     # forme CI/dev → OK
])
def test_cli_exit_code_gates_the_bring_up(advertise_host, addresses, expected_code,
                                          capsys):
    """Le CLI rend un code de sortie exploitable par `start-prod-node.sh`.

    `scripts/start-prod-node.sh` s'appuie sur ce code pour refuser le
    `docker compose up` avant qu'etcd/PG/Patroni ne soient publies.
    """
    argv = ["--advertise-host", advertise_host]
    for addr in addresses:
        argv += ["--address", addr]

    assert _cli.main(argv) == expected_code

    if expected_code != 0:
        # Message actionnable : l'adresse publique ET la valeur fautive.
        output = capsys.readouterr()
        combined = output.out + output.err
        assert "51.210.105.102" in combined
        assert "ADVERTISE_HOST" in combined
