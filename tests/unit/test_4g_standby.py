"""INV-137 : secours 4G (scripts/4g-standby.sh) — aucun client QMI fui, route posee meme
quand la passerelle operateur n'est pas dans un /30 avec l'adresse.

Le script tourne contre de faux `qmicli` / `ip` / `sleep` (PATH) qui journalisent leurs
appels : 3 demarrages rates, succes, session tombee, demarrage rate, succes, arret.
"""
import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.unit

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "4g-standby.sh"

QMICLI = r"""#!/usr/bin/env bash
echo "qmicli $*" >> "$H/calls.log"
n=$(cat "$H/cid" 2>/dev/null || echo 40)
case "$*" in
  *--wds-start-network*)
    a=$(( $(cat "$H/starts" 2>/dev/null || echo 0) + 1 )); echo $a > "$H/starts"; echo $((n+1)) > "$H/cid"
    if [ $a -le 3 ] || [ $a -eq 5 ]; then
      echo "error: couldn't start network: QMI protocol error (14): 'CallFailed'"; echo "    CID: '$n'"; exit 1
    fi
    echo "Packet data handle: '7$a'"; echo "    CID: '$n'" ;;
  *--wds-get-current-settings*) echo "IPv4 address: 10.133.126.63"; echo "IPv4 gateway address: 10.133.126.64" ;;
  *--wds-get-packet-service-status*)
    c=$(( $(cat "$H/st" 2>/dev/null || echo 0) + 1 )); echo $c > "$H/st"
    [ $c -eq 2 ] && echo "'disconnected'" || echo "'connected'" ;;
esac
"""
IP = '#!/usr/bin/env bash\necho "ip $*" >> "$H/calls.log"\n'
# Arrete le script (SIGTERM -> teardown) apres quelques tours de boucle
SLEEP = ('#!/usr/bin/env bash\nc=$(( $(cat "$H/sl" 2>/dev/null || echo 0) + 1 )); echo $c > "$H/sl"\n'
         '[ $c -ge 9 ] && kill -TERM $PPID\nexit 0\n')


@pytest.fixture
def calls(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {"qmicli": QMICLI, "ip": IP, "sleep": SLEEP}.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    # Pas de vrai modem : on court-circuite l'attente de /dev/cdc-wdm0 et la detection de l'interface
    src = SCRIPT.read_text()
    src = re.sub(r"^for i in \$\(seq 1 30\).*$", ":", src, flags=re.M)
    src = re.sub(r'^\[ -e "\$WDM" \] .*$', ":", src, flags=re.M)
    src = re.sub(r"^for c in /sys/class/net/ww\*.*$", "IF=wwtest", src, flags=re.M)
    src = re.sub(r'^\[ -n "\$IF" \] .*$', ":", src, flags=re.M)
    src = src.replace('echo Y > "/sys/class/net/$IF/qmi/raw_ip"', "true")
    script = tmp_path / "4g.sh"
    script.write_text(src)
    env = {"H": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin", "WDM": "/dev/null"}
    subprocess.run(["bash", str(script)], env=env, timeout=30, capture_output=True)
    return (tmp_path / "calls.log").read_text().splitlines()


def test_every_allocated_wds_client_is_released(calls):
    allocated = [str(40 + i) for i, l in enumerate(x for x in calls if "--wds-start-network" in x)]
    released = [m.group(1) for l in calls if (m := re.search(r"--client-cid=(\d+)", l))]
    assert len(allocated) == 6
    assert sorted(set(allocated) - set(released)) == [], "client QMI WDS fui"


def test_default_route_is_onlink(calls):
    routes = [l for l in calls if l.startswith("ip route replace default")]
    assert routes and all(l.endswith(" onlink") for l in routes), routes
