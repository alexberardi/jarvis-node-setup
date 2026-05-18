#!/bin/bash
# Runs once when the postgres container is first initialized
# (docker-entrypoint-initdb.d/*.sh). Creates the additional databases that
# jarvis-auth and jarvis-config-service need.
#
# pgvector extension lives in jarvis_command_center only (CC's memory
# embeddings). The other DBs don't need it.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE DATABASE jarvis_auth;
    CREATE DATABASE jarvis_config;
EOSQL
