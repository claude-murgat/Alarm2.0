#!/bin/bash
# Exécuté UNE SEULE FOIS au premier démarrage du primaire PostgreSQL.
# (docker-entrypoint-initdb.d — skippé si PGDATA/PG_VERSION existe déjà)
set -e

echo "=== [primary_init] Création de l'utilisateur de réplication ==="

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER replicator WITH REPLICATION LOGIN;
EOSQL

# Autoriser les connexions de réplication depuis n'importe quelle IP
# (réseau Docker local — trust car pas de données sensibles en dev)
cat >> "$PGDATA/pg_hba.conf" <<-'EOF'

# Réplication streaming — autorisée depuis le réseau Docker (sans mot de passe)
host    replication     replicator      all             trust
EOF

echo "=== [primary_init] Utilisateur replicator créé, pg_hba.conf mis à jour ==="
