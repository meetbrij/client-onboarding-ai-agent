#!/bin/sh
# Runs once, on first start with an empty volume (the postgres image's initdb hook). Creates the roles and
# databases the service needs; passwords come from the pg-secret Kubernetes Secret, never from this file.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=owner_pw="$ONBOARDING_OWNER_PASSWORD" \
  --set=app_pw="$ONBOARDING_APP_PASSWORD" \
  --set=bank_pw="$MOCKBANK_PASSWORD" <<'SQL'
CREATE ROLE onboarding_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE onboarding_app LOGIN PASSWORD :'app_pw';
CREATE DATABASE onboarding OWNER onboarding_owner;
CREATE ROLE mockbank LOGIN PASSWORD :'bank_pw';
CREATE DATABASE mockbank OWNER mockbank;
SQL
