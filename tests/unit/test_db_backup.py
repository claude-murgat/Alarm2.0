"""INV-191 : sauvegarde logique quotidienne de alarm_db sur NODE3, copie sur alarm-1, test de restauration mensuel.

Jusqu'au 2026-11-09 la base n'avait aucune sauvegarde (seulement la replication Patroni). Le vrai script tourne
contre de faux `docker` / `curl` / `ssh` et un faux mailer (PATH) : faux Patroni (GET :8008/cluster de
Patroni 4.1.5), faux pg_dump / pg_restore / psql dans les conteneurs, alarm-1 = un dossier local.
"""
import fcntl
import json
import os
import pathlib
import re
import stat
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

# no_mutation : lance le vrai script bash, sans toucher backend/app/logic.
pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "alarm-db-backup.sh"
IP = {1: "10.99.0.2", 2: "10.99.0.3", 3: "10.99.0.1", 4: "10.99.0.4", 5: "10.99.0.5", 6: "10.99.0.6", 7: "10.99.0.7"}
PASSWORD = "mot-de-passe-de-test-ne-doit-pas-sortir"
DUMP = "PGDMP sauvegarde de test\n"  # une archive pg_dump -Fc commence par « PGDMP »
NAME_RE = r"alarm_db-(\d{8}T\d{6}Z)-node\d\.dump"

DOCKER = r'''#!/usr/bin/env bash
echo "$*" >> "$H/docker.log"
cmd=$1; shift
case "$cmd" in
  run) printf '%s\n' "$@" | sed -n '/^--name$/{n;p}' >> "$H/containers"; exit 0 ;;
  rm) name=${@: -1}; sed -i "/^$name\$/d" "$H/containers"; echo "$name" >> "$H/removed"; exit 0 ;;
esac
# exec [-i] [-e VAR]... <conteneur> <commande> <args...>
while [[ "$1" == -* ]]; do [ "$1" = -e ] && shift; shift; done
shift
tool=$1; shift
case "$tool" in
  pg_dump)
    while [ $# -gt 0 ] && [ "$1" != -h ]; do shift; done
    echo "$2" >> "$H/dumped_from"
    [ "${PGPASSWORD:-}" = "$EXPECTED_PASSWORD" ] || { echo "pg_dump: password authentication failed" >&2; exit 1; }
    if grep -qx "$2" "$H/dump_fail" 2>/dev/null; then printf 'PGDMP tronque'; echo "pg_dump: error: connection lost" >&2; exit 1; fi
    cat "$H/dump_content" ;;
  pg_restore)
    if [ "$1" = --list ]; then
      [[ "$(cat)" == PGDMP* ]] || { echo "pg_restore: error: input file does not appear to be a valid archive" >&2; exit 1; }
      printf ';\n; Archive created at 2026-11-09 03:05:12 UTC\n3345; 0 16386 TABLE DATA public users alarm\n'
    else
      cat > "$H/restored"
      if [ -f "$H/restore_fail" ]; then echo "pg_restore: error: could not execute query" >&2; exit 1; fi
    fi ;;
  pg_isready) exit 0 ;;
  psql) t=${@: -1}; cat "$H/count.${t##* }" 2>/dev/null || echo 3 ;;
esac
'''
CURL = r'''#!/usr/bin/env bash
for a; do case "$a" in http://*) url=$a ;; esac; done
echo "$url" >> "$H/curl.log"
ip=${url#http://}; ip=${ip%%:*}
case " $(cat "$H/api_down" 2>/dev/null) " in *" $ip "*) exit 7 ;; esac
cat "$H/cluster.json"
'''
# alarm-1 = un dossier local : la commande « distante » est executee ici.
SSH = r'''#!/usr/bin/env bash
echo "$*" >> "$H/ssh.log"
[ -f "$H/ssh_down" ] && { echo "ssh: connect to host 10.99.0.4 port 22: Connection timed out" >&2; exit 255; }
# disque plein sur alarm-1 : SSH repond, l'ecriture echoue
[[ -f "$H/disk_full" && "${@: -1}" == *"cat >"* ]] && { echo "cat: write error: No space left on device" >&2; exit 1; }
exec bash -c "${@: -1}"
'''
MAILER = '#!/usr/bin/env bash\n{ echo "SUJET $1"; cat; } >> "$H/mail"\n'


def cluster(**over):
    """GET :8008/cluster : node1 leader (timeline 290), node2..node7 replicas streaming, lag 10 x N."""
    members = []
    for n in range(1, 8):
        m = {"name": f"node{n}", "role": "replica", "state": "streaming", "api_url": f"http://{IP[n]}:8008/patroni",
             "host": IP[n], "port": 5432, "timeline": 290, "lag": 10 * n}
        if n == 1:
            m.update(role="leader", state="running")
            del m["lag"]
        m.update(over.get(f"node{n}", {}))
        members.append(m)
    return json.dumps({"members": members, "scope": "alarm-cluster"})


@pytest.fixture
def h(tmp_path):
    for name, body in (("docker", DOCKER), ("curl", CURL), ("ssh", SSH), ("mailer", MAILER)):
        f = tmp_path / "bin" / name
        f.parent.mkdir(exist_ok=True)
        f.write_text(body)
        f.chmod(0o755)
    for d in ("backup", "alarm1"):
        (tmp_path / d).mkdir()
    (tmp_path / "dump_content").write_text(DUMP)
    (tmp_path / "cluster.json").write_text(cluster())
    (tmp_path / "containers").write_text("")
    return tmp_path


def run(h, *args, password=PASSWORD):
    env = {k: v for k, v in os.environ.items() if k != "PGPASSWORD"}
    env.update(H=str(h), PATH=f"{h / 'bin'}:{os.environ['PATH']}", BACKUP_DIR=str(h / "backup"),
               COPY_DIR=str(h / "alarm1"), MAILER=str(h / "bin" / "mailer"), EXPECTED_PASSWORD=PASSWORD)
    if password:
        env["PGPASSWORD"] = password
    p = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stderr


def dumps(d):
    return sorted(f.name for f in d.iterdir() if f.name.startswith("alarm_db-"))


def leftovers(d):
    return [f.name for f in d.iterdir() if f.name.endswith(".tmp")]


def read(h, name):
    f = h / name
    return f.read_text() if f.exists() else ""


def stamp(days_ago, node="node3"):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return f"alarm_db-{ts:%Y%m%dT%H%M%SZ}-{node}.dump"


def mode(f):
    return stat.S_IMODE(f.stat().st_mode)


def test_dump_from_healthy_local_replica_verified_then_renamed_private(h):
    rc, err = run(h)
    assert rc == 0, err
    [name] = dumps(h / "backup")
    m = re.fullmatch(NAME_RE, name)
    assert m and name.endswith("-node3.dump"), name
    taken = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - taken).total_seconds()) < 120  # horodatage UTC
    assert (h / "backup" / name).read_text() == DUMP
    assert mode(h / "backup" / name) == 0o600
    assert leftovers(h / "backup") == []
    assert read(h, "curl.log").split()[0] == "http://10.99.0.1:8008/cluster"  # vue lue sur NODE3 d'abord
    assert read(h, "dumped_from").split() == ["127.0.0.1"]  # node3 lu dans son propre conteneur
    docker = read(h, "docker.log")
    assert re.search(r"^exec -e PGPASSWORD .*node3-patroni-1 pg_dump .*-Fc", docker, re.M), docker
    assert re.search(r"^exec -i node3-patroni-1 pg_restore --list", docker, re.M), docker
    assert PASSWORD not in docker + read(h, "ssh.log") + err
    assert "Source : replica node3" in err
    assert read(h, "mail") == ""


@pytest.mark.parametrize("node3", [{"state": "stopped"}, {"state": "running"}, {"timeline": 289, "lag": 0}],
                         ids=["arrete", "bloque", "ancienne_timeline"])
def test_node3_not_healthy_takes_the_healthy_replica_with_the_smallest_lag(h, node3):
    # node2 : le plus petit lag, mais sur une ancienne timeline -> jamais choisi
    (h / "cluster.json").write_text(cluster(node3=node3, node2={"timeline": 289, "lag": 0}, node6={"lag": 5}))
    rc, err = run(h)
    assert rc == 0, err
    assert read(h, "dumped_from").split() == ["10.99.0.6"]
    assert [n[-10:] for n in dumps(h / "backup")] == ["node6.dump"]
    assert "Source : replica node6" in err


def test_leader_when_no_replica_is_healthy(h):
    (h / "cluster.json").write_text(cluster(
        node2={"state": "stopped"}, node3={"state": "running"}, node4={"state": "starting"},
        node5={"timeline": 289}, node6={"timeline": 288}, node7={"state": "crashed"}))
    rc, err = run(h)
    assert rc == 0, err
    assert read(h, "dumped_from").split() == ["10.99.0.2"]
    assert [n[-10:] for n in dumps(h / "backup")] == ["node1.dump"]
    assert "Source : leader node1 (aucun replica sain)" in err


def test_leader_when_the_replica_dump_fails(h):
    (h / "dump_fail").write_text("127.0.0.1\n")
    rc, err = run(h)
    assert rc == 0, err
    assert read(h, "dumped_from").split() == ["127.0.0.1", "10.99.0.2"]
    [name] = dumps(h / "backup")
    assert name.endswith("-node1.dump") and (h / "backup" / name).read_text() == DUMP
    assert leftovers(h / "backup") == []
    assert "Source : leader node1" in err
    assert read(h, "mail") == ""


@pytest.mark.parametrize("case", ["archive_illisible", "pg_dump_en_echec"])
def test_unverified_dump_is_never_kept_and_alerts(h, case):
    old = stamp(1)
    (h / "backup" / old).write_text(DUMP)
    if case == "archive_illisible":
        (h / "dump_content").write_text("pas une archive pg_dump\n")
    else:  # sortie partielle qui commence bien par PGDMP, mais pg_dump sort en erreur
        (h / "dump_fail").write_text("127.0.0.1\n10.99.0.2\n")
    rc, err = run(h)
    assert rc == 1, err
    assert dumps(h / "backup") == [old]
    assert leftovers(h / "backup") == []
    assert read(h, "ssh.log") == ""  # rien n'est copie
    assert "SUJET [ALARME-MURGAT] Sauvegarde" in read(h, "mail")


def test_retention_14_days_on_node3_and_on_alarm1(h):
    old = {age: stamp(age) for age in (1, 2, 13, 15, 20, 40)}
    for d in ("backup", "alarm1"):
        for name in old.values():
            (h / d / name).write_text(DUMP)
    (h / "alarm1" / "notes.txt").write_text("pas une sauvegarde\n")
    rc, err = run(h)
    assert rc == 0, err
    for d in ("backup", "alarm1"):
        names = dumps(h / d)
        assert names[:-1] == sorted(old[a] for a in (1, 2, 13)), d
        assert names[-1] not in old.values()  # la nouvelle
    assert (h / "alarm1" / "notes.txt").exists()


def test_failed_backup_deletes_nothing_the_last_good_one_stays_even_if_old(h):
    old = sorted([stamp(30), stamp(20)])
    for name in old:
        (h / "backup" / name).write_text(DUMP)
    (h / "dump_content").write_text("pas une archive pg_dump\n")
    rc, err = run(h)
    assert rc == 1, err
    assert dumps(h / "backup") == old


def test_copy_to_alarm1_over_wireguard_ssh(h):
    rc, err = run(h)
    assert rc == 0, err
    [name] = dumps(h / "backup")
    assert dumps(h / "alarm1") == [name]
    assert (h / "alarm1" / name).read_bytes() == (h / "backup" / name).read_bytes()
    assert mode(h / "alarm1" / name) == 0o600
    assert leftovers(h / "alarm1") == []
    ssh = read(h, "ssh.log")
    assert "alarm@10.99.0.4" in ssh and "BatchMode=yes" in ssh and "ConnectTimeout=10" in ssh, ssh


@pytest.mark.parametrize("failure", ["ssh_down", "disk_full"])
def test_copy_failure_keeps_the_local_dump_and_alerts(h, failure):
    (h / failure).write_text("")
    rc, err = run(h)
    assert rc == 1, err
    assert len(dumps(h / "backup")) == 1
    assert dumps(h / "alarm1") == []
    mail = read(h, "mail")
    assert "SUJET [ALARME-MURGAT] Sauvegarde" in mail and "alarm-1" in mail


def test_skips_while_another_backup_holds_the_lock(h):
    with open(h / "backup" / "backup.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, err = run(h)
    assert rc == 0, err
    assert "deja en cours" in err
    assert dumps(h / "backup") == []
    assert read(h, "docker.log") == read(h, "curl.log") == read(h, "mail") == ""


@pytest.mark.parametrize("case", ["vue_illisible", "sans_leader", "sans_mot_de_passe"])
def test_nothing_to_dump_alerts(h, case):
    if case == "vue_illisible":
        (h / "api_down").write_text(" ".join(IP.values()))
    if case == "sans_leader":  # leader perdu : plus personne ne diffuse
        (h / "cluster.json").write_text(cluster(node1={"role": "replica", "state": "stopped"},
                                                **{f"node{n}": {"state": "running"} for n in range(2, 8)}))
    rc, err = run(h, password=None if case == "sans_mot_de_passe" else PASSWORD)
    assert rc == 1, err
    assert dumps(h / "backup") == []
    assert "SUJET [ALARME-MURGAT] Sauvegarde" in read(h, "mail")
    if case == "vue_illisible":
        assert len(read(h, "curl.log").split()) == 7  # les 7 noeuds essayes


def containers_run(h):
    lines = [l.split() for l in read(h, "docker.log").splitlines() if l.startswith("run ")]
    return [l[l.index("--name") + 1] for l in lines], lines


def test_monthly_restore_test_restores_the_newest_dump_in_a_throwaway_container(h):
    (h / "backup" / stamp(3)).write_text("PGDMP ancienne\n")
    (h / "backup" / stamp(0.1)).write_text("PGDMP la plus recente\n")
    rc, err = run(h, "--restore-test")
    assert rc == 0, err
    assert read(h, "restored") == "PGDMP la plus recente\n"
    [name], [line] = containers_run(h)
    assert "--network none" in " ".join(line) and "postgres:16-alpine" in line
    assert not {"-p", "--publish", "-P", "--publish-all"} & set(line)  # aucun port
    assert read(h, "removed").split() == [name]
    assert read(h, "containers").strip() == ""
    assert "users=3" in err and "escalation_config=3" in err and "system_config=3" in err
    assert read(h, "curl.log") == read(h, "mail") == ""

    rc, err = run(h, "--restore-test")
    assert rc == 0, err
    names, _ = containers_run(h)
    assert len(set(names)) == 2  # nom unique a chaque test


@pytest.mark.parametrize("case,reason", [("pg_restore_en_echec", "pg_restore"), ("users_vide", "table users vide"),
                                         ("aucune_sauvegarde", "aucune sauvegarde")])
def test_restore_test_failure_alerts_and_always_removes_the_container(h, case, reason):
    if case != "aucune_sauvegarde":
        (h / "backup" / stamp(1)).write_text(DUMP)
    if case == "pg_restore_en_echec":
        (h / "restore_fail").write_text("")
    if case == "users_vide":
        (h / "count.users").write_text("0\n")
    rc, err = run(h, "--restore-test")
    assert rc == 1, err
    names, _ = containers_run(h)
    assert read(h, "removed").split() == names
    assert read(h, "containers").strip() == ""
    mail = read(h, "mail")
    assert "SUJET [ALARME-MURGAT] Test de restauration" in mail and reason in mail, mail


def test_units_daily_backup_and_monthly_restore_test_as_alarm():
    sd = REPO / "infra" / "onsite" / "systemd"
    for unit, arg in (("alarm-db-backup", ""), ("alarm-db-restore-test", " --restore-test")):
        service = (sd / f"{unit}.service").read_text()
        assert f"\nExecStart=/usr/local/bin/alarm-db-backup{arg}\n" in service, unit
        for line in ("User=alarm", "StateDirectory=alarm/db-backup", "StateDirectoryMode=0700", "UMask=0077"):
            assert f"\n{line}\n" in service, (unit, line)
    assert "\nEnvironmentFile=-/etc/alarm/db-backup.env\n" in (sd / "alarm-db-backup.service").read_text()
    for unit, calendar in (("alarm-db-backup", r"\*-\*-\* 03:00:00 UTC"), ("alarm-db-restore-test", r"\*-\*-01 05:00:00 UTC")):
        timer = (sd / f"{unit}.timer").read_text()
        assert re.search(rf"^OnCalendar={calendar}$", timer, re.M), unit
        assert re.search(r"^RandomizedDelaySec=30min$", timer, re.M) and "\nPersistent=true\n" in timer, unit
