"""INV-181 : un noeud sur site garde un resolveur DNS public apres celui du reseau local.

2026-11-02 : alarm-1 sans reseau local, passe sur sa 4G (VPN OK), seul resolveur = la box morte ->
plus de registre, de GitHub ni de SMTP. Le vrai script tourne sur des fichiers temporaires.
"""
import os
import pathlib
import subprocess

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "dns-fallback.sh"
DHCP_LINE = "append domain-name-servers 1.1.1.1, 9.9.9.9;"


def run(resolv, dhclient):
    p = subprocess.run(["bash", str(SCRIPT), str(resolv), str(dhclient)], capture_output=True, text=True,
                       timeout=10)
    assert p.returncode == 0, p.stderr
    return p.stdout


def nameservers(f):
    return [line.split()[1] for line in f.read_text().splitlines() if line.startswith("nameserver")]


def test_lan_only_gets_public_fallbacks_after_the_lan_resolver(tmp_path):
    r = tmp_path / "resolv.conf"
    r.write_text("# Generated\nsearch alarm.local\nnameserver 172.16.0.1\n")
    run(r, tmp_path / "absent")
    assert nameservers(r) == ["172.16.0.1", "1.1.1.1", "9.9.9.9"]  # la box reste la premiere
    assert "search alarm.local" in r.read_text()
    first = r.read_text()
    run(r, tmp_path / "absent")
    assert r.read_text() == first  # idempotent


@pytest.mark.parametrize("content", ["nameserver 192.168.1.1\nnameserver 1.1.1.1\n",  # onsite-1
                                     "nameserver 1.1.1.1\nnameserver 9.9.9.9\n",  # onsite-2
                                     "nameserver 10.0.0.1\nnameserver 10.0.0.2\nnameserver 10.0.0.3\n"])
def test_public_already_present_or_three_resolvers_unchanged(tmp_path, content):
    r = tmp_path / "resolv.conf"
    r.write_text(content)
    run(r, tmp_path / "absent")
    assert r.read_text() == content


def test_two_resolvers_get_only_one_fallback(tmp_path):
    r = tmp_path / "resolv.conf"
    r.write_text("nameserver 172.16.0.1\nnameserver 172.16.0.2\n")
    run(r, tmp_path / "absent")
    assert nameservers(r) == ["172.16.0.1", "172.16.0.2", "1.1.1.1"]  # glibc n'en lit que 3


def test_symlinked_resolv_conf_is_left_to_its_manager(tmp_path):
    target = tmp_path / "stub-resolv.conf"
    target.write_text("nameserver 127.0.0.53\n")
    r = tmp_path / "resolv.conf"
    r.symlink_to(target)
    out = run(r, tmp_path / "absent")
    assert r.is_symlink() and target.read_text() == "nameserver 127.0.0.53\n"
    assert "lien" in out


def test_dhclient_conf_gets_the_append_once_and_is_never_created(tmp_path):
    r = tmp_path / "resolv.conf"
    r.write_text("nameserver 172.16.0.1\n")
    d = tmp_path / "dhclient.conf"
    d.write_text("request subnet-mask, routers, domain-name-servers;\n")
    run(r, d)
    run(r, d)
    assert d.read_text().count(DHCP_LINE) == 1
    absent = tmp_path / "none.conf"
    run(r, absent)
    assert not absent.exists()


def test_file_without_final_newline_is_not_corrupted(tmp_path):
    r = tmp_path / "resolv.conf"
    r.write_text("nameserver 172.16.0.1")
    d = tmp_path / "dhclient.conf"
    d.write_text("request routers;")
    run(r, d)
    assert nameservers(r) == ["172.16.0.1", "1.1.1.1", "9.9.9.9"]
    assert d.read_text().splitlines()[0] == "request routers;"
