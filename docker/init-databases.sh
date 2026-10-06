#!/bin/sh
# Runs once on first start of the local Postgres volume. Local development credentials only.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE mockbank LOGIN PASSWORD 'mockbank-local';
CREATE DATABASE mockbank OWNER mockbank;
SQL
