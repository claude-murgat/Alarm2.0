#!/usr/bin/env bash
# /opt/alarm/disk-check.sh — supervision du remplissage disque (/ + volume docker).
# Envoie UN email d'alerte par episode quand un seuil est franchi (WARN ~85%,
# CRIT ~92%), re-notifie un probleme persistant au plus toutes les RENOTIFY_HOURS,
# et envoie un email de retour a la normale quand ca redescend. Lance par
# alarm-disk-check.timer (toutes les 15 min) sur les 3 noeuds.
#
# POURQUOI : le 2026-06-22 le cluster Patroni est tombe (plus de leader, alarme
# incapable de sonner) parce que du WAL a sature les disques de node1 (207 Go de
# WAL -> 100%) et de node3 (cloud, 100%). PostgreSQL ne redemarrait plus. La panne
# est restee SILENCIEUSE 2,5 jours faute de supervision disque. Ce check est ce
# garde-fou manquant : alerter AVANT la saturation, sur les 3 noeuds.
#
# Reutilise le meme envoi SMTP que le watchdog modem (send-alert-email.py, memes
# secrets /opt/alarm/.env.prod.*). Dependances : bash + coreutils (df) + python3.
set -uo pipefail

WARN="${DISK_WARN_PCENT:-85}"
CRIT="${DISK_CRIT_PCENT:-92}"
# Chemins a verifier. /var/lib/docker (data-root docker, ou vit le volume pgdata
# qui a deborde le 2026-06-22) est souvent sur le meme fs que / : on dedoublonne
# par device, donc aucune double alerte dans ce cas.
PATHS="${DISK_CHECK_PATHS:-/ /var/lib/docker}"
# Tant qu'un fs reste au-dessus du seuil, ne re-mailer qu'une fois toutes les N h
# (sinon spam toutes les 15 min). Mettre 0 pour mailer a chaque run.
RENOTIFY_HOURS="${DISK_RENOTIFY_HOURS:-6}"
STATE_DIR="${DISK_CHECK_STATE_DIR:-/var/lib/alarm/disk-check}"
MAILER="${DISK_MAILER:-/opt/alarm/send-alert-email.py}"
HOST="$(hostname -s 2>/dev/null || hostname)"

log(){ echo "$(date '+%F %T') [disk-check] $*"; }

DRY=0; TEST_EMAIL=0
for a in "$@"; do
  case "$a" in
    --dry-run)    DRY=1;;
    --test-email) TEST_EMAIL=1;;
    -h|--help)    echo "usage: disk-check.sh [--dry-run] [--test-email]"; exit 0;;
    *)            log "argument inconnu: $a";;
  esac
done

mailer(){ # $1 = sujet ; corps lu sur stdin
  if [ -f "$MAILER" ]; then
    /usr/bin/python3 "$MAILER" "$1"
  else
    log "mailer absent ($MAILER) -> email NON envoye: $1"; cat >/dev/null; return 1
  fi
}

if [ "$TEST_EMAIL" = 1 ]; then
  echo "Test d'alerte disque depuis ${HOST} — chaine SMTP operationnelle." \
    | mailer "[ALARME-MURGAT] Test supervision disque (${HOST})"
  exit $?
fi

rank(){ case "$1" in crit) echo 2;; warn) echo 1;; *) echo 0;; esac; }

mkdir -p "$STATE_DIR" 2>/dev/null || true

declare -A seen
status=0

for p in $PATHS; do
  line="$(df -P "$p" 2>/dev/null | awk 'NR==2{cap=$5; gsub(/%/,"",cap); m=$6; for(i=7;i<=NF;i++) m=m" "$i; printf "%s\t%s\t%s", $1, cap, m}')"
  [ -n "$line" ] || { log "df indisponible pour $p (chemin absent ?) — ignore"; continue; }
  IFS=$'\t' read -r dev cap mnt <<<"$line"
  # Dedoublonnage par device : si /var/lib/docker est sur le meme fs que /, on
  # ne le compte qu'une fois (pas de double alerte pour le meme disque).
  [ -n "${seen[$dev]:-}" ] && continue
  seen[$dev]=1
  case "$cap" in ''|*[!0-9]*) log "pourcentage illisible pour $p ('$cap') — ignore"; continue;; esac

  if   [ "$cap" -ge "$CRIT" ]; then level=crit
  elif [ "$cap" -ge "$WARN" ]; then level=warn
  else                              level=ok; fi

  if [ "$DRY" = 1 ]; then
    log "DRY ${mnt} (${dev}) = ${cap}% -> ${level} (WARN=${WARN} CRIT=${CRIT})"
    continue
  fi

  key="$(printf '%s' "$dev" | tr -c 'A-Za-z0-9' '_')"
  sf="$STATE_DIR/$key.state"
  prev_level=ok; prev_ts=0
  if [ -r "$sf" ]; then read -r prev_level prev_ts < "$sf" 2>/dev/null || true; fi
  [ -n "$prev_level" ] || prev_level=ok
  case "$prev_ts" in ''|*[!0-9]*) prev_ts=0;; esac

  now="$(date +%s)"
  cur="$(rank "$level")"; prev="$(rank "$prev_level")"
  do_mail=""; new_ts="$prev_ts"

  if [ "$cur" -gt "$prev" ]; then
    do_mail=alert                                  # ok->warn, ok->crit, warn->crit
  elif [ "$cur" -eq "$prev" ] && [ "$cur" -gt 0 ]; then
    if [ "$RENOTIFY_HOURS" -gt 0 ] && [ "$((now - prev_ts))" -ge "$((RENOTIFY_HOURS * 3600))" ]; then
      do_mail=alert                                # toujours en alerte -> rappel
    fi
  elif [ "$cur" -lt "$prev" ] && [ "$cur" -eq 0 ]; then
    do_mail=recovery                               # warn/crit -> ok
  fi

  if [ "$do_mail" = alert ]; then
    if [ "$level" = crit ]; then
      subj="[ALARME-MURGAT] DISQUE CRITIQUE ${cap}% sur ${HOST} (${mnt}) — AGIR"
      hdr="CRITIQUE : le disque ${mnt} de ${HOST} est a ${cap}% (seuil critique ${CRIT}%)."
    else
      subj="[ALARME-MURGAT] Disque a ${cap}% sur ${HOST} (${mnt}) — surveiller"
      hdr="Le disque ${mnt} de ${HOST} est a ${cap}% (seuil d'alerte ${WARN}%)."
    fi
    {
      echo "$hdr"; echo
      df -h "$p" 2>/dev/null; echo
      echo "Device : ${dev}"
      echo "IMPACT si saturation : PostgreSQL/Patroni ne peut plus ecrire son WAL,"
      echo "le noeud perd le leader et l'alarme devient incapable de sonner — c'est"
      echo "exactement la panne du 2026-06-22 (restee silencieuse 2,5 jours)."
      echo "Liberer de l'espace : WAL, vieux journaux (journald), images docker"
      echo "dangling (docker image prune), backups obsoletes."
    } | mailer "$subj" \
      && { new_ts="$now"; log "ALERTE ${level} envoyee: ${mnt} ${cap}%"; } \
      || { status=1; log "echec envoi alerte ${mnt} ${cap}%"; }
  elif [ "$do_mail" = recovery ]; then
    {
      echo "Retour a la normale : le disque ${mnt} de ${HOST} est redescendu a"
      echo "${cap}% (sous le seuil d'alerte ${WARN}%). Aucune action requise."
      echo
      df -h "$p" 2>/dev/null
    } | mailer "[ALARME-MURGAT] Disque revenu a la normale sur ${HOST} (${mnt}) — ${cap}%" \
      && log "RECUP envoyee: ${mnt} ${cap}%" || { status=1; log "echec envoi recup ${mnt}"; }
    new_ts="$now"
  else
    log "${mnt} (${dev}) = ${cap}% -> ${level} (pas d'email)"
  fi

  printf '%s %s\n' "$level" "$new_ts" > "$sf" 2>/dev/null \
    || log "ecriture state KO ($sf) — disque plein ?"
done

exit "$status"
