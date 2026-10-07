#!/bin/sh
# Runs once on first start of the local Postgres volume (run `docker compose down -v` to re-run it).
# Local development credentials only; synthetic data.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE mockbank LOGIN PASSWORD 'mockbank-local';
CREATE DATABASE mockbank OWNER mockbank;
-- the onboarding database: an owner that migrates, and a restricted role the service runs as
CREATE ROLE onboarding_owner LOGIN PASSWORD 'onboarding-owner-local';
CREATE ROLE onboarding_app LOGIN PASSWORD 'onboarding-app-local';
CREATE DATABASE onboarding OWNER onboarding_owner;
SQL
