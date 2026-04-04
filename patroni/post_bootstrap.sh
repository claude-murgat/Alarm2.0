#!/bin/sh
# Execute une seule fois par Patroni sur le leader initial apres initdb.
# Cree l'utilisateur applicatif et la base.
psql -h /tmp -U postgres <<SQL
CREATE USER alarm WITH PASSWORD 'alarm_secret' CREATEDB;
CREATE USER replicator WITH PASSWORD 'rep_secret' REPLICATION;
CREATE DATABASE alarm_db OWNER alarm;
SQL
