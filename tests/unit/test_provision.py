"""Scripts de provisioning d'une machine sur site (scripts/provision/, docs/STANDARD_MACHINE.md).

Faits a la main pour alarm-3/alarm-4 le 2026-11-05 (scripts jetables, mots de passe en dur), rendus
reproductibles : machine reconnue par le plan de infra/onsite/standard.env, idempotents, sans aucun
mot de passe. Les vrais scripts tournent dans une copie du depot, contre un faux / (ALARM_ROOT) et de
fausses commandes (PATH) qui journalisent leurs appels.
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess

import pytest

# no_mutation : lance de vrais scripts bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

REPO = pathlib.Path(__file__).resolve().parents[2]
N3 = "GDN64aY60tBSWKN4qA6GBd/JhistmDP2oF1qN0Xj9gw="
NEWKEY = "kO/o+xzXVeKNQywK4UDupDRdnM7mZ5cBYauo32OxU2A="
GENERIC = 'n=${0##*/}; echo "$n${*:+ $*}" >> "$H/calls.log"\n[ -f "$H/out/$n" ] && cat "$H/out/$n"\n' \
          'exit "$(cat "$H/rc/$n" 2>/dev/null || echo 0)"'
SPECIAL = {
    "id": 'echo "${FAKE_UID:-0}"',
    "hostname": 'cat "$H/hostname"',
    "wg": r'''echo "wg $*" >> "$H/calls.log"
case "$1" in
  genkey) echo "PRIVtestprivtestprivtestprivtestprivtest000=" ;;
  pubkey) cat >/dev/null; echo "PUBtestpubtestpubtestpubtestpubtestpub0000=" ;;
  show) cat "$H/out/wg_show" 2>/dev/null ;;
esac''',
    "dpkg": r'''echo "dpkg $*" >> "$H/calls.log"
case "$1" in --print-architecture) echo amd64 ;; --audit) cat "$H/out/dpkg_audit" 2>/dev/null ;; esac''',
    "curl": r'''echo "curl $*" >> "$H/calls.log"
o=""; for a in "$@"; do [ "$o" = 1 ] && { echo FAKE-GPG > "$a"; exit 0; }; [ "$a" = -o ] && o=1; done
cat "$H/out/curl" 2>/dev/null''',
}
FAKES = ["apt-get", "usermod", "systemctl", "ufw", "systemd-tmpfiles", "sysctl", "sshd", "ip", "ping",
         "systemd-run", "grub-install", "update-grub", "blkid", "efibootmgr", "runuser", "docker"]
NODE_SCRIPTS = {"00-base.sh": [], "10-harden.sh": [], "20-wireguard-node.sh": [], "40-join.sh": [],
                "50-gateway.sh": [], "60-change-ip.sh": ["192.168.1.52/24", "192.168.1.1"],
                "80-bios-stage.sh": ["/nulle/part/9020MA19.exe"], "90-uefi-repair.sh": []}


def write(p, content, mode=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    if mode is not None:
        p.chmod(mode)


@pytest.fixture
def h(tmp_path):
    """Copie du depot (scripts de provisioning + infra/onsite) et une machine alarm-3 vierge."""
    for name in FAKES:
        write(tmp_path / "bin" / name, f"#!/usr/bin/env bash\n{GENERIC}\n", 0o755)
    for name, body in SPECIAL.items():
        write(tmp_path / "bin" / name, f"#!/usr/bin/env bash\n{body}\n", 0o755)
    repo = tmp_path / "repo"
    shutil.copytree(REPO / "scripts" / "provision", repo / "scripts" / "provision")
    shutil.copytree(REPO / "infra" / "onsite", repo / "infra" / "onsite")
    shutil.copy(REPO / "scripts" / "alarm-cd-pull.sh", repo / "scripts" / "alarm-cd-pull.sh")
    write(tmp_path / "hostname", "alarm-3\n")
    write(tmp_path / "root/etc/os-release", 'ID=debian\nVERSION_ID="13"\nVERSION_CODENAME=trixie\n')
    return tmp_path


def run(h, script, *args, uid=0, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    env.update(H=str(h), PATH=f"{h / 'bin'}:{os.environ['PATH']}", ALARM_ROOT=str(h / "root"),
               FAKE_UID=str(uid), HOME=str(h / "operator"), **extra)
    p = subprocess.run(["bash", str(h / "repo/scripts/provision" / script), *args], env=env,
                       capture_output=True, text=True, timeout=60, cwd=h)
    return p.returncode, p.stdout + p.stderr


def calls(h):
    f = h / "calls.log"
    return f.read_text().splitlines() if f.exists() else []


# --- Garde-fous communs ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", NODE_SCRIPTS)
def test_unexpected_hostname_is_refused_before_any_change(h, script):
    """Une commande lancee sur la mauvaise machine (ou un Debian pas encore renomme) ne touche a rien."""
    (h / "hostname").write_text("debian\n")
    rc, out = run(h, script, *NODE_SCRIPTS[script])
    assert rc == 1, out
    assert "absente du plan" in out
    assert calls(h) == []


@pytest.mark.parametrize("script", [*NODE_SCRIPTS, "21-wireguard-node3.sh"])
def test_root_scripts_refuse_a_normal_user(h, script):
    rc, out = run(h, script, *NODE_SCRIPTS.get(script, ["10.99.0.6", NEWKEY]), uid=1000)
    assert rc == 1 and "root" in out, out
    assert calls(h) == []


# --- 00-base : paquets, Docker CE, journaux limites -------------------------------------------------

def test_base_installs_docker_from_the_official_repo_with_bounded_logs(h):
    rc, out = run(h, "00-base.sh")
    assert rc == 0, out
    assert (h / "root/etc/apt/sources.list.d/docker.list").read_text() == (
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable\n")
    assert json.loads((h / "root/etc/docker/daemon.json").read_text()) == {
        "log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}
    assert "usermod -aG docker alarm" in calls(h)
    rc, out = run(h, "00-base.sh")  # idempotent : docker n'est pas relance pour rien
    assert rc == 0, out
    assert calls(h).count("systemctl restart docker") == 1


# --- 10-harden : pare-feu au sous-reseau du LAN, watchdog, SSH par cle -------------------------------

def test_harden_opens_ssh_and_backend_to_the_current_lan_only(h):
    write(h / "out/ip", "2: eno1    inet 172.16.1.120/16 brd 172.16.255.255 scope global eno1\n")
    rc, out = run(h, "10-harden.sh")
    assert rc == 0, out
    c = calls(h)
    assert "ufw allow from 172.16.0.0/16 to any port 22 proto tcp comment SSH LAN site" in c
    assert "ufw allow from 172.16.0.0/16 to any port 8000 proto tcp comment Backend LAN site (apps Android)" in c
    assert "ufw allow in on wg0 comment Wireguard mesh prod inter-noeuds" in c
    # 2026-11-09 : Patroni (reseau Docker du projet) vers l'etcd local par l'IP WireGuard du plan (alarm-3)
    assert "ufw allow from 172.18.0.0/16 to 10.99.0.6 port 2379 proto tcp comment Patroni (conteneur) -> etcd local" in c
    assert "ufw --force enable" in c
    assert "RuntimeWatchdogSec=30s" in (h / "root/etc/systemd/system.conf.d/watchdog.conf").read_text()
    assert (h / "root/etc/chrony/chrony.conf").read_text() == (REPO / "infra/onsite/chrony.conf").read_text()
    assert not (h / "root/etc/ssh/sshd_config.d/99-hardening.conf").exists()  # sans --ssh


def test_harden_ssh_password_off_only_when_a_key_is_deployed(h):
    write(h / "out/ip", "2: eno1    inet 192.168.1.52/24 brd 192.168.1.255 scope global eno1\n")
    rc, out = run(h, "10-harden.sh", "--ssh")
    assert rc == 1 and "authorized_keys" in out, out
    assert not (h / "root/etc/ssh/sshd_config.d/99-hardening.conf").exists()
    write(h / "root/home/alarm/.ssh/authorized_keys", "ssh-ed25519 AAAAC3Nza operateur\n")
    rc, out = run(h, "10-harden.sh", "--ssh")
    assert rc == 0, out
    assert (h / "root/etc/ssh/sshd_config.d/99-hardening.conf").exists()
    assert "sshd -t" in calls(h)


# --- 20 / 21 : WireGuard -------------------------------------------------------------------------

WG0 = f"""[Interface]
Address = 10.99.0.6/24
ListenPort = 51820
MTU = 1280
PrivateKey = PRIVtestprivtestprivtestprivtestprivtest000=

[Peer]
# NODE3 cloud OVH : relais vers tous les autres noeuds (10.99.0.0/24)
PublicKey = {N3}
Endpoint = 51.210.105.102:51820
AllowedIPs = 10.99.0.0/24
PersistentKeepalive = 25
"""


def test_wireguard_node_has_node3_as_its_only_peer(h):
    rc, out = run(h, "20-wireguard-node.sh", WG_WAIT_SECONDS="0")
    assert rc == 0, out
    conf = h / "root/etc/wireguard/wg0.conf"
    assert WG0 in conf.read_text()
    assert conf.read_text().count("[Peer]") == 1
    assert conf.stat().st_mode & 0o777 == 0o600
    assert "21-wireguard-node3.sh 10.99.0.6 PUBtestpubtestpubtestpubtestpubtestpub0000= alarm-3" in out
    first = conf.read_text()
    rc, out = run(h, "20-wireguard-node.sh", WG_WAIT_SECONDS="0")  # idempotent : meme cle, pas de relance
    assert rc == 0 and conf.read_text() == first, out
    assert calls(h).count("wg genkey") == 1
    assert calls(h).count("systemctl restart wg-quick@wg0") == 1


def test_wireguard_node_refuses_to_overwrite_a_different_conf_without_force(h):
    conf = h / "root/etc/wireguard/wg0.conf"
    write(conf, "[Interface]\nAddress = 10.99.0.6/24\n\n[Peer]\n# pair LAN direct\nEndpoint = 172.16.1.119:51820\n", 0o600)
    rc, out = run(h, "20-wireguard-node.sh", WG_WAIT_SECONDS="0")
    assert rc == 1 and "--force" in out, out
    assert "pair LAN direct" in conf.read_text()
    rc, out = run(h, "20-wireguard-node.sh", "--force", WG_WAIT_SECONDS="0")
    assert rc == 0, out
    assert WG0 in conf.read_text()
    assert any("pair LAN direct" in b.read_text() for b in conf.parent.glob("wg0.conf.bak-*"))


NODE3_CONF = """[Interface]
Address = 10.99.0.1/24
ListenPort = 51820
PrivateKey = node3privnode3privnode3privnode3privnode3p=

[Peer]
# NODE4 alarm-1
PublicKey = alarm1pubalarm1pubalarm1pubalarm1pubalarm1p=
AllowedIPs = 10.99.0.4/32
"""


def test_node3_declares_the_new_peer_once_without_endpoint(h):
    conf = h / "root/etc/wireguard/wg0.conf"
    write(conf, NODE3_CONF, 0o600)
    write(h / "root/proc/sys/net/ipv4/ip_forward", "1\n")
    for _ in range(2):
        rc, out = run(h, "21-wireguard-node3.sh", "10.99.0.6", NEWKEY, "alarm-3")
        assert rc == 0, out
    text = conf.read_text()
    assert text.startswith(NODE3_CONF)
    assert text.count(f"PublicKey = {NEWKEY}\nAllowedIPs = 10.99.0.6/32\n") == 1
    assert "Endpoint" not in text  # derriere NAT : appris par PersistentKeepalive
    assert f"wg set wg0 peer {NEWKEY} allowed-ips 10.99.0.6/32" in calls(h)


@pytest.mark.parametrize("args,rc_expected", [
    (["10.99.0.4", NEWKEY], 1),           # IP deja portee par une autre cle (remplacement : retirer l'ancien pair)
    (["10.99.0.6", "pas-une-cle"], 2),
    (["10.99.1.6", NEWKEY], 2),
    (["10.99.0.1", NEWKEY], 2),           # NODE3 lui-meme
    (["10.99.0.6"], 2),
])
def test_node3_refuses_bad_or_conflicting_peers(h, args, rc_expected):
    conf = h / "root/etc/wireguard/wg0.conf"
    write(conf, NODE3_CONF, 0o600)
    rc, out = run(h, "21-wireguard-node3.sh", *args)
    assert rc == rc_expected, out
    assert conf.read_text() == NODE3_CONF
    assert not [c for c in calls(h) if c.startswith("wg set")]


def test_node3_script_refuses_to_run_elsewhere(h):
    write(h / "root/etc/wireguard/wg0.conf", "[Interface]\nAddress = 10.99.0.6/24\n", 0o600)
    rc, out = run(h, "21-wireguard-node3.sh", "10.99.0.7", NEWKEY)
    assert rc == 1 and "NODE3" in out, out


# --- 30-secrets (poste operateur) : flux NODE3 -> machine, rien sur le poste ni a l'ecran ------------

SSH = r'''#!/usr/bin/env bash
# Faux ssh : execute la commande distante dans $H/hosts/<cible> (/opt/alarm et ~ y sont redirigés).
t=""; while [ $# -gt 0 ]; do case "$1" in -p|-i|-o) shift 2 ;; -*) shift ;; *) t=$1; shift; break ;; esac; done
d="$H/hosts/$t"; [ -d "$d" ] || { echo "ssh: connect to host $t: No route" >&2; exit 255; }
cmd="$*"; cmd=${cmd//\/opt\/alarm/$d/opt/alarm}
echo "$t" >> "$H/ssh.log"
cd "$d" && HOME="$d/home" exec bash -c "$cmd"
'''
SECRETS = "SECRET_KEY=s3cr3t-jwt-valeur\nGATEWAY_KEY=gw-k3y-valeur\nSMTP_USER=alertes@example.org\n"


@pytest.fixture
def ops(h):
    write(h / "bin/ssh", SSH, 0o755)
    n3 = h / "hosts/alarm@node3"
    write(n3 / "opt/alarm/.env.prod.secrets", SECRETS, 0o600)
    write(n3 / "opt/alarm/backend/firebase-service-account.json", '{"project_id": "alarme"}\n')
    write(n3 / "home/.ssh/id_ed25519.pub", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOrchestrateur alarm@node3\n")
    node = h / "hosts/alarm@172.16.1.120"
    (node / "opt/alarm/backend").mkdir(parents=True)
    (node / "home").mkdir()
    (h / "hosts/alarm@10.99.0.6").symlink_to(node)  # meme machine, vue de NODE3 par le VPN
    # hostname distant : chaque faux hote repond son nom
    write(h / "bin/hostname", '#!/usr/bin/env bash\ncase "$HOME" in *172.16.1.120*) echo alarm-3 ;; '
                              '*) cat "$H/hostname" ;; esac\n', 0o755)
    return h


def run_ops(h):
    return run(h, "30-secrets.sh", "alarm@172.16.1.120", NODE3_SSH="ssh alarm@node3")


def test_secrets_are_streamed_from_node3_and_never_printed(ops):
    for _ in range(2):  # idempotent
        rc, out = run_ops(ops)
        assert rc == 0, out
    node = ops / "hosts/alarm@172.16.1.120"
    s = node / "opt/alarm/.env.prod.secrets"
    assert s.read_text() == SECRETS and s.stat().st_mode & 0o777 == 0o600
    assert (node / "opt/alarm/backend/firebase-service-account.json").read_text() == '{"project_id": "alarme"}\n'
    ak = (node / "home/.ssh/authorized_keys").read_text()
    assert ak.count("AAAAIOrchestrateur") == 1
    for secret in ("s3cr3t-jwt-valeur", "gw-k3y-valeur"):
        assert secret not in out
    assert not (ops / "operator").exists()  # rien d'ecrit sur le poste
    assert "alarm@10.99.0.6" in (ops / "ssh.log").read_text()  # NODE3 joint la machine par cle (CD)


def test_secrets_empty_on_node3_never_overwrite_the_node(ops):
    (ops / "hosts/alarm@node3/opt/alarm/.env.prod.secrets").unlink()
    node_secrets = ops / "hosts/alarm@172.16.1.120/opt/alarm/.env.prod.secrets"
    write(node_secrets, "SECRET_KEY=ancienne\n", 0o600)
    rc, out = run_ops(ops)
    assert rc == 1, out
    assert node_secrets.read_text() == "SECRET_KEY=ancienne\n"


def test_secrets_refused_for_a_machine_outside_the_plan(ops):
    write(ops / "bin/hostname", "#!/usr/bin/env bash\necho debian\n", 0o755)
    rc, out = run_ops(ops)
    assert rc == 1 and "absente du plan" in out, out
    assert not (ops / "hosts/alarm@172.16.1.120/opt/alarm/.env.prod.secrets").exists()


def test_secrets_needs_a_target(h):
    rc, out = run(h, "30-secrets.sh")
    assert rc == 2 and "usage" in out.lower(), out


# --- 40-join : entree dans le cluster (non votant) -------------------------------------------------

CLUSTER = '{"members": [{"name": "node3", "role": "leader", "state": "running"}, ' \
          '{"name": "node6", "role": "replica", "state": "%s", "lag": 0}]}'


def join_ready(h, state="streaming"):
    write(h / "repo/.env.prod.node6", "NODE_NAME=node6\n")
    write(h / "repo/.env.prod.secrets", "SECRET_KEY=x\n", 0o600)
    write(h / "out/curl", CLUSTER % state)


def test_join_starts_the_node_and_waits_for_streaming(h):
    join_ready(h)
    rc, out = run(h, "40-join.sh", JOIN_POLL_SECONDS="0")
    assert rc == 0, out
    c = calls(h)
    assert c.index("systemctl start alarm-cd-pull.service alarm-host-sync.service") \
        < c.index("runuser -u alarm -- ./scripts/start-prod-node.sh --pull 6")
    assert "node6 : streaming" in out


@pytest.mark.parametrize("broken", ["env", "secrets_mode", "never_streaming"])
def test_join_stops_on_missing_prerequisites_or_no_streaming(h, broken):
    join_ready(h, state="running" if broken == "never_streaming" else "streaming")
    if broken == "env":
        (h / "repo/.env.prod.node6").unlink()
    if broken == "secrets_mode":
        (h / "repo/.env.prod.secrets").chmod(0o644)
    rc, out = run(h, "40-join.sh", JOIN_POLL_SECONDS="0", JOIN_TRIES="2")
    assert rc == 1, out
    started = any("start-prod-node.sh" in c for c in calls(h))
    assert started == (broken == "never_streaming")


# --- 50-gateway : cle, site, contact sec ------------------------------------------------------------

def test_gateway_env_gets_key_site_and_source_without_printing_the_key(h):
    (h / "hostname").write_text("alarm-1\n")
    env = h / "root/etc/alarm-gateway.env"
    write(env, "DRY_CONTACT_SOURCE=modem\nGATEWAY_ID=alarm-1\nGATEWAY_KEY=changeme-in-prod\nNODE1_URL=http://10.99.0.1:8000\n",
          0o640)
    write(h / "repo/.env.prod.secrets", "SECRET_KEY=x\nGATEWAY_KEY=gw-secret-123\n", 0o600)
    rc, out = run(h, "50-gateway.sh")
    assert rc == 0, out
    lines = env.read_text().splitlines()
    for kv in ("GATEWAY_KEY=gw-secret-123", "GATEWAY_SITE=beaufort", "DRY_CONTACT_SOURCE=host",
               "DRY_CONTACT_NORMAL_VALUE=0", "GATEWAY_ID=alarm-1", "NODE1_URL=http://10.99.0.1:8000"):
        assert lines.count(kv) == 1, (kv, lines)
    assert not [l for l in lines if l.startswith("GATEWAY_KEY=") and l != "GATEWAY_KEY=gw-secret-123"]
    assert env.stat().st_mode & 0o777 == 0o640
    assert "gw-secret-123" not in out
    assert calls(h).count("systemctl restart alarm-sms-gateway") == 1
    rc, out = run(h, "50-gateway.sh")  # deja au standard : rien ne change, pas de relance
    assert rc == 0, out
    assert calls(h).count("systemctl restart alarm-sms-gateway") == 1


def test_gateway_without_key_in_secrets_changes_nothing(h):
    env = h / "root/etc/alarm-gateway.env"
    write(env, "DRY_CONTACT_SOURCE=host\n", 0o640)
    write(h / "repo/.env.prod.secrets", "SECRET_KEY=x\n", 0o600)
    rc, out = run(h, "50-gateway.sh")
    assert rc == 1 and "GATEWAY_KEY" in out, out
    assert env.read_text() == "DRY_CONTACT_SOURCE=host\n"


# --- 60-change-ip -----------------------------------------------------------------------------------

IFACES = """source /etc/network/interfaces.d/*

auto lo
iface lo inet loopback

allow-hotplug eno1
iface eno1 inet static
	address 172.16.1.120/16
	gateway 172.16.0.1

iface wlp2s0 inet static
	address 10.0.0.5/24
	gateway 10.0.0.1
"""


def test_change_ip_rewrites_only_the_lan_stanza_then_reboots(h):
    f = h / "root/etc/network/interfaces"
    write(f, IFACES)
    write(h / "rc/ping", "1")  # personne ne repond sur la nouvelle adresse
    rc, out = run(h, "60-change-ip.sh", "192.168.1.52/24", "192.168.1.1")
    assert rc == 0, out
    assert f.read_text() == IFACES.replace("address 172.16.1.120/16", "address 192.168.1.52/24") \
                                  .replace("gateway 172.16.0.1", "gateway 192.168.1.1")
    assert list(f.parent.glob("interfaces.bak-*"))
    assert any(c.startswith("systemd-run") and c.endswith("systemctl reboot") for c in calls(h))


@pytest.mark.parametrize("args,rc_expected", [
    (["192.168.1.52/24", "192.168.1.1"], 1),   # l'adresse repond deja : conflit (echange de cables)
    (["192.168.1.300/24", "192.168.1.1"], 2),
    (["192.168.1.52", "192.168.1.1"], 2),       # prefixe obligatoire
    (["192.168.1.52/24", "10.0.0.1"], 2),       # passerelle hors du reseau
    ([], 2),
])
def test_change_ip_refuses_conflicts_and_bad_addresses(h, args, rc_expected):
    f = h / "root/etc/network/interfaces"
    write(f, IFACES)
    write(h / "rc/ping", "0")
    rc, out = run(h, "60-change-ip.sh", *args)
    assert rc == rc_expected, out
    assert f.read_text() == IFACES
    assert not any(c.startswith("systemd-run") for c in calls(h))


# --- 80-bios-stage ----------------------------------------------------------------------------------

def bios_setup(h, version="A04"):
    exe = h / "dl/9020MA19.exe"
    write(exe, "faux BIOS Dell\n")
    std = (REPO / "infra/onsite/standard.env").read_text()
    std = std.replace("STD_BIOS_SIZE=9874896", f"STD_BIOS_SIZE={exe.stat().st_size}").replace(
        "6ead68a5423ef181492fee85e0e4c667a67465d40b7a2fbbb4bdf7a78c548a7d", hashlib.sha256(exe.read_bytes()).hexdigest())
    write(h / "std.env", std)
    write(h / "root/sys/class/dmi/id/bios_version", version + "\n")
    (h / "root/boot/efi/EFI/debian").mkdir(parents=True)
    return exe, str(h / "std.env")


def test_bios_file_is_staged_on_the_esp_only_if_it_matches_the_dell_catalog(h):
    exe, std = bios_setup(h)
    rc, out = run(h, "80-bios-stage.sh", str(exe), ALARM_STANDARD_ENV=std)
    assert rc == 0, out
    assert (h / "root/boot/efi/9020MA19.exe").read_bytes() == exe.read_bytes()
    assert "F12" in out
    write(exe, "fichier corrompu ou falsifie\n")
    (h / "root/boot/efi/9020MA19.exe").unlink()
    rc, out = run(h, "80-bios-stage.sh", str(exe), ALARM_STANDARD_ENV=std)
    assert rc == 1, out
    assert not (h / "root/boot/efi/9020MA19.exe").exists()


def test_bios_already_at_target_version_stages_nothing(h):
    exe, std = bios_setup(h, version="A19")
    rc, out = run(h, "80-bios-stage.sh", str(exe), ALARM_STANDARD_ENV=std)
    assert rc == 0, out
    assert not (h / "root/boot/efi/9020MA19.exe").exists()


# --- 90-uefi-repair ---------------------------------------------------------------------------------

EFI_WIN = ("BootOrder: 0000,0001\n"
           "Boot0000* Windows Boot Manager\tHD(1,GPT,4077e54a-1111-4222-8333-444455556666,0x800,0x32000)"
           "/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
           "Boot0001* debian\tHD(1,GPT,0c5e1d2a-0000-4000-8000-000000000001,0x800,0x1e8000)/File(\\EFI\\debian\\shimx64.efi)\n")


@pytest.mark.parametrize("windows_partition", [False, True])
def test_uefi_repair_reinstalls_grub_and_drops_only_orphan_windows_entries(h, windows_partition):
    (h / "root/sys/firmware/efi").mkdir(parents=True)
    write(h / "out/efibootmgr", EFI_WIN)
    write(h / "out/blkid", "0c5e1d2a-0000-4000-8000-000000000001\n"
                           + ("4077E54A-1111-4222-8333-444455556666\n" if windows_partition else ""))
    write(h / "out/dpkg_audit", "The following packages are only half configured:\n grub-efi-amd64\n")
    rc, out = run(h, "90-uefi-repair.sh")
    assert rc == 0, out
    c = calls(h)
    assert "dpkg --configure -a" in c
    assert ("grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=debian "
            "--force-extra-removable --recheck") in c
    assert "update-grub" in c
    assert ("efibootmgr -q -b 0000 -B" in c) == (not windows_partition)


def test_uefi_repair_refuses_a_legacy_boot(h):
    rc, out = run(h, "90-uefi-repair.sh")
    assert rc == 1 and "UEFI" in out, out
    assert not [c for c in calls(h) if c.startswith(("grub-install", "efibootmgr"))]
