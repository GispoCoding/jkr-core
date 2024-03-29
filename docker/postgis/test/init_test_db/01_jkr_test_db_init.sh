#!/bin/bash
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER $JKR_USER with CREATEROLE login password '$JKR_PASSWORD';
    CREATE DATABASE $JKR_TEST_DB;
    GRANT ALL PRIVILEGES ON DATABASE $JKR_TEST_DB TO $JKR_USER;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$JKR_TEST_DB" <<-EOSQL
    CREATE EXTENSION postgis;
    CREATE EXTENSION btree_gist;
    CREATE EXTENSION pg_stat_statements;
EOSQL
