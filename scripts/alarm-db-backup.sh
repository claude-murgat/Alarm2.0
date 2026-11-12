#!/bin/bash
# alarm-db-backup.sh [--restore-test] — sauvegarde logique de la base alarm_db (INV-191).
#
# Sur NODE3 (en `alarm`, groupe docker), lance par alarm-db-backup.timer (chaque jour vers 03:00 UTC) :
#   1. vue du cluster (GET :8008/cluster) : un replica sain (streaming sur la timeline du leader,
#      node3 d'abord), sinon le leader ; dump du replica en echec -> une tentative sur le leader ;
#   2. pg_dump -Fc dans le conteneur local node3-patroni-1 (PostgreSQL 16) vers un temporaire,
#      verifie par pg_restore --list, puis renomme alarm_db-<UTC>-<noeud>.dump ;
#   3. retention 14 jours (jamais la plus recente), copie sur alarm-1 par SSH (WireGuard) de chaque sauvegarde
#      qu'alarm-1 n'a pas (rattrapage apres une panne), meme retention.
# --restore-test (alarm-db-restore-test.timer, chaque mois) : restaure la plus recente dans un
# conteneur PostgreSQL 16 jetable, sans reseau, verifie que les tables cles ont des lignes, le supprime.
#
# Echec -> email (send-alert-email.py, voie SMTP de la supervision disque, independante du cluster) et
# code 1. Sauvegarde deja en cours -> code 0, rien. Procedure de restauration : docs/CD_DESIGN.md §9quinquies.
# Tests : tests/unit/test_db_backup.py (variables surchargeables, faux docker/ssh/curl sur le PATH).

set -euo pipefail
umask 077  # mots de passe haches, telephones : fichiers 0600, dossiers 0700

NODE="${NODE:-node3}"
PG_CONTAINER="${PG_CONTAINER:-${NODE}-patroni-1}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/alarm/db-backup}"
# Copie hors site : alarm-1 (node4, Beaufort). Meme dossier que STD_DB_BACKUP_DIR de standard.env.
COPY_IP="${COPY_IP:-10.99.0.4}"
COPY_DIR="${COPY_DIR:-/var/lib/alarm/db-backup}"
KEEP_DAYS=14
MAILER="${MAILER:-/opt/alarm/send-alert-email.py}"
RESTORE_IMAGE="${RESTORE_IMAGE:-postgres:16-alpine}"
# Remplies au premier demarrage du backend, jamais vides en service.
RESTORE_TABLES="users escalation_config system_config"
PGUSER="${PGUSER:-alarm}"
NODE_IPS="10.99.0.1 10.99.0.2 10.99.0.3 10.99.0.4 10.99.0.5 10.99.0.6 10.99.0.7"
NAME_RE='^alarm_db-[0-9]{8}T[0-9]{6}Z-node[0-9]+\.dump$'
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)

UNIT=alarm-db-backup; WHAT="Sauvegarde de la base"
if [[ "${1:-}" == --restore-test ]]; then UNIT=alarm-db-restore-test; WHAT="Test de restauration de la base"; fi

log() { echo "[$UNIT] $*" >&2; }

fail() {
  log "ERREUR : $*"
  printf '%s\n\nMachine : %s\nJournal : journalctl -u %s -n 50\nProcedure : docs/CD_DESIGN.md §9quinquies (INV-191)\n' \
      "$*" "$(hostname -s)" "$UNIT" \
    | "$MAILER" "[ALARME-MURGAT] $WHAT en echec ($(hostname -s))" >&2 \
    || log "email d'alerte NON envoye ($MAILER)"
  exit 1
}

newest() { { ls -1 "$BACKUP_DIR" 2>/dev/null | grep -E "$NAME_RE" || true; } | sort | tail -n 1; }

# Sauvegardes a supprimer parmi les noms lus sur stdin : plus de KEEP_DAYS jours (horodatage du nom).
# Appele seulement apres une sauvegarde reussie : la plus recente, qui vient d'etre faite, n'est jamais concernee.
to_delete() {
  { grep -E "$NAME_RE" || true; } \
    | awk -F- -v c="$(date -u -d "-$KEEP_DAYS days" +%Y%m%dT%H%M%SZ)" '$2 < c'
}

if [[ "$UNIT" == alarm-db-restore-test ]]; then
  DUMP=$(newest)
  [[ -n "$DUMP" ]] || fail "aucune sauvegarde dans $BACKUP_DIR"
  CTR="alarm-db-restore-test-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  trap 'docker rm -f "$CTR" >/dev/null 2>&1 || true' EXIT
  # --network none : aucun port publie, aucun reseau ; trust : rien n'y entre que docker exec.
  docker run -d --name "$CTR" --network none -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=alarm_db \
    "$RESTORE_IMAGE" >/dev/null || fail "conteneur $RESTORE_IMAGE non demarre"
  # En TCP : pendant l'initialisation, le serveur temporaire de l'image n'ecoute que sur la socket.
  for i in $(seq 60); do
    docker exec "$CTR" pg_isready -q -h 127.0.0.1 -U postgres -d alarm_db && break
    (( i < 60 )) || fail "PostgreSQL du conteneur $CTR pas pret en 120 s"
    sleep 2
  done
  docker exec -i "$CTR" pg_restore -U postgres -d alarm_db --no-owner --no-acl --exit-on-error \
    < "$BACKUP_DIR/$DUMP" || fail "pg_restore de $DUMP en echec"
  COUNTS=""
  for t in $RESTORE_TABLES; do
    n=$(docker exec "$CTR" psql -U postgres -d alarm_db -tAc "SELECT count(*) FROM $t") || n=""
    [[ "$n" =~ ^[0-9]+$ && "$n" -gt 0 ]] || fail "table $t vide ou illisible apres restauration de $DUMP (${n:-erreur psql})"
    COUNTS+=" $t=$n"
  done
  log "Test de restauration OK : $DUMP ->$COUNTS"
  exit 0
fi

# Verrou : une seule sauvegarde a la fois ; une autre encore en cours -> ce tour est saute (pas une panne).
mkdir -p "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/backup.lock"
if ! flock -n 9; then
  log "Sauvegarde deja en cours (verrou $BACKUP_DIR/backup.lock), skip ce tour"
  exit 0
fi
rm -f "$BACKUP_DIR"/.alarm_db-*.tmp  # restes d'une sauvegarde interrompue (verrou tenu : rien d'autre n'ecrit)

[[ -n "${PGPASSWORD:-}" ]] || fail "PGPASSWORD absent (/etc/alarm/db-backup.env, cf docs/CD_DESIGN.md §9quinquies)"
export PGPASSWORD

# Candidats « <nom> <hote> <port> <role> » : le meilleur replica sain (node3 d'abord, puis le plus petit
# lag), puis le leader. Pas de leader running -> exit 1.
CANDIDATES_PY='
import json, sys
members = json.load(sys.stdin)["members"]
leader = next(m for m in members if m["role"] == "leader" and m.get("state") == "running")
ok = [m for m in members if m["role"] != "leader" and m.get("state") == "streaming"
      and m.get("timeline") == leader.get("timeline")]
ok.sort(key=lambda m: (m["name"] != sys.argv[1], m["lag"] if isinstance(m.get("lag"), int) else float("inf")))
for m in ok[:1] + [leader]:
    print(m["name"], m["host"], m["port"], "replica" if m is not leader else "leader")
'
VIEW=""
for ip in $NODE_IPS; do
  VIEW=$(curl -fsS --max-time 5 "http://${ip}:8008/cluster" 2>/dev/null \
    | python3 -c "$CANDIDATES_PY" "$NODE" 2>/dev/null) && break
  VIEW=""
done
[[ -n "$VIEW" ]] || fail "aucun leader Patroni running dans la vue du cluster (GET :8008/cluster, 7 noeuds essayes)"
mapfile -t CANDIDATES <<< "$VIEW"

TS=$(date -u +%Y%m%dT%H%M%SZ)
TMP=""
trap '[[ -z "$TMP" ]] || rm -f "$TMP"' EXIT
NAME=""
TRIED=""
for c in "${CANDIDATES[@]}"; do
  read -r member host port role <<< "$c"
  # Membre local : lu dans son propre conteneur (ufw de NODE3 ne laisse pas le reseau Docker joindre
  # 10.99.0.1:5432, et rien ne passe par le WAN).
  [[ "$member" == "$NODE" ]] && { host=127.0.0.1; port=5432; }
  if [[ "$role" == leader && -z "$TRIED" ]]; then
    log "Source : leader $member (aucun replica sain) ($host:$port)"
  else
    log "Source : $role $member ($host:$port)"
  fi
  TRIED+=" $member"
  TMP="$BACKUP_DIR/.alarm_db-$TS-$member.dump.tmp"
  # -e PGPASSWORD sans valeur : docker la prend dans l'environnement, jamais dans la ligne de commande.
  if ! docker exec -e PGPASSWORD -e PGCONNECT_TIMEOUT=10 "$PG_CONTAINER" \
       pg_dump -h "$host" -p "$port" -U "$PGUSER" -d alarm_db -Fc > "$TMP"; then
    log "pg_dump depuis $member en echec"
    rm -f "$TMP"; continue
  fi
  # Capture puis grep (et non un pipe : grep -q fermerait le pipe, pipefail ferait echouer pg_restore).
  TOC=$(docker exec -i "$PG_CONTAINER" pg_restore --list < "$TMP" 2>/dev/null) || TOC=""
  if ! grep -q ' TABLE DATA public users ' <<< "$TOC"; then
    log "Sauvegarde depuis $member illisible par pg_restore --list (ou sans la table users) : non gardee"
    rm -f "$TMP"; continue
  fi
  NAME="alarm_db-$TS-$member.dump"
  mv "$TMP" "$BACKUP_DIR/$NAME"
  TMP=""
  break
done
[[ -n "$NAME" ]] || fail "aucune sauvegarde valide (pg_dump ou verification pg_restore --list en echec depuis :$TRIED)"
log "Sauvegarde OK : $BACKUP_DIR/$NAME ($(stat -c %s "$BACKUP_DIR/$NAME") octets)"

# Retention locale, apres une sauvegarde reussie seulement : des echecs repetes ne suppriment rien.
for f in $(ls -1 "$BACKUP_DIR" | to_delete); do
  rm -f "$BACKUP_DIR/$f"
  log "Supprimee (plus de $KEEP_DAYS jours) : $f"
done

# Copie sur alarm-1 : chaque sauvegarde locale qu'alarm-1 n'a pas (rattrapage apres une panne), la plus
# ancienne d'abord, temporaire puis renommage ; meme retention la-bas. Retention locale deja faite : toutes
# les sauvegardes locales sont dans la fenetre. Meme nom = meme sauvegarde (horodatage UTC + noeud, renommage atomique).
remote() { ssh "${SSH_OPTS[@]}" "alarm@$COPY_IP" "$@"; }
REMOTE=$(remote "ls -1 '$COPY_DIR'") \
  || fail "copie de $NAME sur alarm-1 ($COPY_IP:$COPY_DIR) en echec (liste des copies illisible) ; sauvegardes locales gardees dans $BACKUP_DIR"
mapfile -t TODO < <(comm -23 <(ls -1 "$BACKUP_DIR" | grep -E "$NAME_RE" | sort) <(grep -E "$NAME_RE" <<< "$REMOTE" | sort))
CAUGHT_UP=0
for i in "${!TODO[@]}"; do
  f=${TODO[i]}
  remote "umask 077 && rm -f '$COPY_DIR'/.alarm_db-*.tmp && cat > '$COPY_DIR/.$f.tmp' && mv '$COPY_DIR/.$f.tmp' '$COPY_DIR/$f'" \
    < "$BACKUP_DIR/$f" \
    || fail "copie de ${TODO[*]:i} sur alarm-1 ($COPY_IP:$COPY_DIR) en echec ; sauvegardes locales gardees dans $BACKUP_DIR"
  log "Copie OK sur alarm-1 : $COPY_IP:$COPY_DIR/$f"
  [[ "$f" == "$NAME" ]] || CAUGHT_UP=$((CAUGHT_UP + 1))
done
s=""; (( CAUGHT_UP > 1 )) && s=s
(( CAUGHT_UP == 0 )) || log "$CAUGHT_UP sauvegarde$s rattrapée$s sur alarm-1"
OLD=$(to_delete <<< "$REMOTE")
if [[ -n "$OLD" ]]; then
  # Noms filtres par NAME_RE (to_delete) : rien a echapper.
  remote "cd '$COPY_DIR' && rm -f -- ${OLD//$'\n'/ }" || fail "retention des copies sur alarm-1 ($COPY_IP:$COPY_DIR) en echec"
  log "Supprimees sur alarm-1 (plus de $KEEP_DAYS jours) : ${OLD//$'\n'/ }"
fi
