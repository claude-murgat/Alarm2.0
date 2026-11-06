"""INV-186 : conformite d'une machine sur site au standard (docs/STANDARD_MACHINE.md), chaque jour.

2026-11-06 : 4 OptiPlex 9020M avec 3 versions de BIOS, Secure Boot sur une seule, alarm-2 qui ne
redemarrait pas seule (Boot List Option Legacy). Le vrai script tourne contre un faux /sys et /etc
(ALARM_ROOT) et de fausses commandes (PATH), avec le vrai infra/onsite/standard.env.
"""
import os
import pathlib
import re
import subprocess
import time

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "alarm-node-check.sh"
STANDARD = REPO / "infra" / "onsite" / "standard.env"
N3 = "GDN64aY60tBSWKN4qA6GBd/JhistmDP2oF1qN0Xj9gw="
SECUREBOOT = "sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"
KEY = "cle-passerelle-de-test-ne-doit-pas-sortir"

EFI_OK = ("BootCurrent: 0001\nTimeout: 0 seconds\nBootOrder: 0001,0002\n"
          "Boot0001* debian\tHD(1,GPT,0c5e1d2a-0000-4000-8000-000000000001,0x800,0x1e8000)/File(\\EFI\\debian\\shimx64.efi)\n"
          "Boot0002* UEFI: IP4 Intel(R) Ethernet Connection I217-LM\tPciRoot(0x0)/Pci(0x19,0x0)/MAC(f8bc12000000,0)/IPv4(0.0.0.0)\n")
FAKES = {
    "id": 'echo "${FAKE_UID:-0}"',
    "hostname": 'cat "$H/hostname"',
    "efibootmgr": 'cat "$H/efibootmgr"',
    "dpkg": '[ "$1" = --audit ] && cat "$H/dpkg_audit"',
    "ufw": 'cat "$H/ufw"',
    "chronyc": 'cat "$H/chronyc"',
    "ip": 'cat "$H/ip_wg0"',
    "wg": '[ -f "$H/wg_down" ] && { echo "Unable to access interface: No such device" >&2; exit 1; }; cat "$H/wg"',
    "systemctl": r'''case "$1" in
  show) cat "$H/watchdog" ;;
  is-enabled) cat "$H/enabled/$2" 2>/dev/null || echo enabled ;;
  is-active) cat "$H/active/$2" 2>/dev/null || echo active ;;
esac''',
}


def write(p, content):
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.write_bytes if isinstance(content, bytes) else p.write_text)(content)


def handshake(age):
    return f"OtherPeerKeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx=\t0\n{N3}\t{int(time.time()) - age}\n"


@pytest.fixture
def h(tmp_path):
    """Une machine alarm-3 conforme au standard."""
    for name, body in FAKES.items():
        write(tmp_path / "bin" / name, f"#!/usr/bin/env bash\n{body}\n")
        (tmp_path / "bin" / name).chmod(0o755)
    r = tmp_path / "root"
    for f, v in (("sys_vendor", "Dell Inc."), ("product_name", "OptiPlex 9020M"), ("bios_version", "A19")):
        write(r / "sys/class/dmi/id" / f, v + "\n")
    write(r / SECUREBOOT, b"\x06\x00\x00\x00\x01")
    write(r / "etc/os-release", 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_ID="13"\n')
    write(r / "etc/resolv.conf", "nameserver 172.16.0.1\nnameserver 1.1.1.1\nnameserver 9.9.9.9\n")
    (r / "var/log/journal").mkdir(parents=True)
    write(r / "etc/alarm-gateway.env", f"DRY_CONTACT_SOURCE=host\nDRY_CONTACT_NORMAL_VALUE=0\n"
                                       f"GATEWAY_ID=alarm-3\nGATEWAY_KEY={KEY}\nGATEWAY_SITE=izeron\n")
    for f, v in (("hostname", "alarm-3\n"), ("efibootmgr", EFI_OK), ("dpkg_audit", ""), ("ufw", "Status: active\n"),
                 ("chronyc", "Reference ID    : 4D4D4D4D (ntp)\nStratum         : 3\nLeap status     : Normal\n"),
                 ("watchdog", "30s\n"), ("wg", handshake(30)),
                 ("ip_wg0", "5: wg0    inet 10.99.0.6/24 scope global wg0\\       valid_lft forever\n"),
                 ("enabled/ModemManager", "masked\n")):
        write(tmp_path / f, v)
    return tmp_path


def run(h, *args, uid=0):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    env.update(H=str(h), PATH=f"{h / 'bin'}:{os.environ['PATH']}", ALARM_ROOT=str(h / "root"),
               ALARM_STANDARD_ENV=str(STANDARD), FAKE_UID=str(uid), LANG="fr_FR.UTF-8")
    p = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout.splitlines(), p.stdout + p.stderr


def checks(lines, status):
    return sorted(l.split()[1] for l in lines if l.split()[0] == status)


FAMILIES = {"modele", "bios", "demarrage", "entrees_legacy", "secure_boot", "premier_demarrage", "os",
            "dpkg_audit", "dns_secours", "ufw", "chrony", "watchdog", "journald", "modemmanager", "docker",
            "wg0", "wg0_node3", "passerelle_cle", "passerelle_site", "passerelle_contact_sec"}


def test_conforming_machine_is_all_ok_and_never_prints_the_gateway_key(h):
    rc, lines, out = run(h)
    assert rc == 0, out
    assert all(re.match(r"^(OK|INFO) \S+ ", l) for l in lines), out
    assert FAMILIES <= set(checks(lines, "OK")), out
    units = re.search(r'STD_UNITS="([^"]+)"', STANDARD.read_text()).group(1).split()
    assert {f"unit:{u}" for u in units} <= set(checks(lines, "OK"))
    assert "unit:alarm-node-check.timer" in checks(lines, "OK")  # le controle se surveille lui-meme
    assert KEY not in out


def efi(h, text):
    (h / "efibootmgr").write_text(text)


DEVIATIONS = {
    # alarm-1 et alarm-2 le 2026-11-06 : BIOS A08 et A04
    "bios": (lambda h: write(h / "root/sys/class/dmi/id/bios_version", "A04\n"), ["bios"]),
    "modele": (lambda h: write(h / "root/sys/class/dmi/id/product_name", "OptiPlex 7020M\n"), ["modele"]),
    # Demarre en legacy : ni efivars ni NVRAM lisible
    "demarrage": (lambda h: (__import__("shutil").rmtree(h / "root/sys/firmware/efi"),
                             efi(h, "EFI variables are not supported on this system.\n")),
                  ["demarrage", "premier_demarrage", "secure_boot"]),
    # alarm-2 le 2026-11-06 : Boot List Option = Legacy
    "entrees_legacy": (lambda h: efi(h, EFI_OK + "Boot0003* Onboard NIC\tBBS(128,Onboard NIC,0x0)\n"),
                       ["entrees_legacy"]),
    "secure_boot": (lambda h: write(h / "root" / SECUREBOOT, b"\x06\x00\x00\x00\x00"), ["secure_boot"]),
    # alarm-2 le 2026-11-06 : entree « Windows Boot Manager » orpheline en tete
    "premier_demarrage": (lambda h: efi(h, EFI_OK.replace("BootOrder: 0001,0002", "BootOrder: 0000,0001")
                                        + "Boot0000* Windows Boot Manager\tHD(1,GPT,4077e54a-0000-4000-8000-000000000002,"
                                          "0x800,0x32000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"),
                          ["premier_demarrage"]),
    "os": (lambda h: write(h / "root/etc/os-release", 'ID=debian\nVERSION_ID="12"\n'), ["os"]),
    "dpkg_audit": (lambda h: write(h / "dpkg_audit", "The following packages are only half configured:\n grub-efi-amd64\n"),
                   ["dpkg_audit"]),
    # INV-181 : la box seule
    "dns_secours": (lambda h: write(h / "root/etc/resolv.conf", "nameserver 172.16.0.1\n"), ["dns_secours"]),
    "ufw": (lambda h: write(h / "ufw", "Status: inactive\n"), ["ufw"]),
    "chrony": (lambda h: write(h / "chronyc", "Leap status     : Not synchronised\n"), ["chrony"]),
    "watchdog": (lambda h: write(h / "watchdog", "0\n"), ["watchdog"]),
    "journald": (lambda h: (h / "root/var/log/journal").rmdir(), ["journald"]),
    "modemmanager": (lambda h: write(h / "enabled/ModemManager", "enabled\n"), ["modemmanager"]),
    "docker": (lambda h: write(h / "active/docker", "inactive\n"), ["docker"]),
    "wg0_adresse": (lambda h: write(h / "ip_wg0", "5: wg0    inet 10.99.0.4/24 scope global wg0\n"), ["wg0"]),
    "wg0_vieille_poignee": (lambda h: write(h / "wg", handshake(900)), ["wg0_node3"]),
    "wg0_jamais": (lambda h: write(h / "wg", f"{N3}\t0\n"), ["wg0_node3"]),
    "wg0_absent": (lambda h: (write(h / "ip_wg0", ""), write(h / "wg_down", "")), ["wg0", "wg0_node3"]),
    "cle_par_defaut": (lambda h: write(h / "root/etc/alarm-gateway.env",
                                       "DRY_CONTACT_SOURCE=host\nGATEWAY_KEY=changeme-in-prod\nGATEWAY_SITE=izeron\n"),
                       ["passerelle_cle"]),
    "cle_absente": (lambda h: write(h / "root/etc/alarm-gateway.env", "DRY_CONTACT_SOURCE=host\nGATEWAY_SITE=izeron\n"),
                    ["passerelle_cle"]),
    # alarm-3 est « izeron » dans le plan (INV-182), meme physiquement a Beaufort
    "site": (lambda h: write(h / "root/etc/alarm-gateway.env",
                             f"DRY_CONTACT_SOURCE=host\nGATEWAY_KEY={KEY}\nGATEWAY_SITE=beaufort\n"),
             ["passerelle_site"]),
    "contact_sec": (lambda h: write(h / "root/etc/alarm-gateway.env",
                                    f"DRY_CONTACT_SOURCE=modem\nGATEWAY_KEY={KEY}\nGATEWAY_SITE=izeron\n"),
                    ["passerelle_contact_sec"]),
    "unit": (lambda h: write(h / "enabled/alarm-host-sync.timer", "disabled\n"), ["unit:alarm-host-sync.timer"]),
}


@pytest.mark.parametrize("name", DEVIATIONS)
def test_each_deviation_is_reported_ko_on_its_check_only(h, name):
    mutate, expected = DEVIATIONS[name]
    mutate(h)
    rc, lines, out = run(h)
    assert rc == 1, out
    assert checks(lines, "KO") == sorted(expected), out
    assert KEY not in out


def test_machine_outside_the_plan_is_not_checked(h):
    """onsite-1/onsite-2 (autre materiel, en retrait) et NODE3 : hors standard, pas de faux KO quotidiens."""
    (h / "hostname").write_text("onsite-1\n")
    (h / "root/sys/class/dmi/id/product_name").write_text("NC03U\n")
    rc, lines, out = run(h)
    assert rc == 0, out
    assert len(lines) == 1 and lines[0].startswith("INFO "), out


def test_refuses_to_run_without_root(h):
    rc, lines, out = run(h, uid=1000)
    assert rc == 2, out
    assert lines == []


def test_journal_mode_logs_ko_lines_at_warning_level(h):
    (h / "root/sys/class/dmi/id/bios_version").write_text("A08\n")
    rc, lines, out = run(h, "--journal")
    assert rc == 1, out
    assert [l for l in lines if l.startswith("<4>")] == [l for l in lines if l.startswith("<4>KO bios ")]
    assert len([l for l in lines if l.startswith("<4>")]) == 1
    assert all(l.startswith(("<6>OK ", "<6>INFO ", "<4>KO ")) for l in lines), out


def test_install_sh_installs_the_check_and_a_daily_timer():
    install = (REPO / "infra/onsite/install.sh").read_text()
    default = re.search(r'ALARM_STANDARD_ENV:-([^}]+)}', SCRIPT.read_text()).group(1)
    assert re.search(rf'standard\.env"? +{re.escape(default)} ', install)  # la ou le script le lit
    assert re.search(r'alarm-node-check\.sh"? +/usr/local/bin/alarm-node-check ', install)
    assert re.search(r"for u in [^\n]*alarm-node-check\.timer[^\n]*; do\n\s*systemctl enable --now", install)
    assert re.search(r"alarm-node-check\.service alarm-node-check\.timer", install)  # units copiees
    service = (REPO / "infra/onsite/systemd/alarm-node-check.service").read_text()
    timer = (REPO / "infra/onsite/systemd/alarm-node-check.timer").read_text()
    assert "ExecStart=/usr/local/bin/alarm-node-check --journal" in service
    assert re.search(r"^OnCalendar=", timer, re.M) and "Persistent=true" in timer
