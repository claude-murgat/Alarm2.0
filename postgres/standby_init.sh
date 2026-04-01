#!/bin/sh
# Init container : clone le primaire via pg_basebackup pour initialiser le standby.
# S'exécute en tant que root, utilise su-exec pour switcher en user postgres (uid 70).
# Se termine avec exit 0 quand l'initialisation est faite → docker compose attend ce signal.
set -e

DATA_DIR=/var/lib/postgresql/data
PRIMARY_HOST=${PRIMARY_HOST:-db}
PRIMARY_PORT=${PRIMARY_PORT:-5432}

echo "=== [standby_init] Attente du primaire PostgreSQL à ${PRIMARY_HOST}:${PRIMARY_PORT} ==="
TRIES=0
until pg_isready -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -q; do
    TRIES=$((TRIES + 1))
    if [ "$TRIES" -ge 60 ]; then
        echo "ERREUR : primaire non disponible après 120s. Abandon."
        exit 1
    fi
    echo "  Tentative $TRIES/60 — nouvelle tentative dans 2s..."
    sleep 2
done
echo "  Primaire prêt."

if [ -f "${DATA_DIR}/PG_VERSION" ]; then
    echo "=== [standby_init] Données déjà présentes (PG_VERSION trouvé) — pg_basebackup ignoré ==="
    exit 0
fi

echo "=== [standby_init] Clonage du primaire via pg_basebackup ==="
mkdir -p "$DATA_DIR"

# Exécuter pg_basebackup en tant que postgres (uid 70) pour que les fichiers lui appartiennent.
# --write-recovery-conf  → crée standby.signal + primary_conninfo dans postgresql.auto.conf
# --wal-method=stream    → transfère les WAL en parallèle (évite les trous)
su-exec postgres \
    pg_basebackup \
        -h "$PRIMARY_HOST" \
        -p "$PRIMARY_PORT" \
        -U replicator \
        -D "$DATA_DIR" \
        -P \
        --wal-method=stream \
        --write-recovery-conf \
        -v

echo "=== [standby_init] pg_basebackup terminé. Standby prêt à démarrer. ==="
