#!/bin/bash
set -e

if [ -z "$1" ]; then
  echo "Usage: ./alembic_migrate.sh \"migration message\""
  exit 1
fi

MESSAGE="$1"

echo "Generating Alembic migration: $MESSAGE"
alembic revision --autogenerate -m "$MESSAGE"