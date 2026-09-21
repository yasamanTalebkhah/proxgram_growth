#!/usr/bin/env bash
set -e

echo "Starting Zero-Downtime Deployment..."

git pull origin main

docker compose up -d postgres redis

echo "Waiting for core services..."
docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-proxgram}"

if [ -f "src/database/init_schema.sql" ]; then
    echo "Applying schema check..."
    docker compose exec -T postgres psql -U "${POSTGRES_USER:-proxgram}" -d "${POSTGRES_DB:-proxgram_growth}" -f - < src/database/init_schema.sql || true
fi

echo "Rebuilding and restarting worker..."
docker compose build worker
docker compose up -d --no-deps worker

echo "Deployment finished cleanly."
docker compose ps
