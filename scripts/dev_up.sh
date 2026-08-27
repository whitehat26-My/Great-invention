#!/usr/bin/env bash
# Bring up PostgreSQL + Redis locally WITHOUT Docker.
#
# docker-compose.yml is the normal path on a workstation. This script exists for
# environments where the Docker daemon is unavailable (CI containers, sandboxes):
# it initialises a throwaway Postgres cluster and a Redis instance under
# .devdata/, on the ports from .env.example, and is safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/.devdata"
PGDATA="$DATA/pg"
PGPORT="${POSTGRES_PORT:-5433}"
PGUSER_NAME="${POSTGRES_USER:-restaurant}"
PGPASSWORD_VAL="${POSTGRES_PASSWORD:-restaurant}"
PGDB="${POSTGRES_DB:-restaurant_ai}"
REDIS_PORT="${REDIS_PORT:-6380}"

PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)"
if [[ -z "$PGBIN" ]]; then
  echo "ERROR: no PostgreSQL server found under /usr/lib/postgresql." >&2
  echo "Install it (apt-get install postgresql) or use: docker compose up -d" >&2
  exit 1
fi

mkdir -p "$DATA"

# --- PostgreSQL -------------------------------------------------------------
if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
  echo "==> initdb $PGDATA"
  rm -rf "$PGDATA"; mkdir -p "$PGDATA"
  # The cluster runs as an unprivileged user; initdb refuses to run as root.
  RUNAS=""
  if [[ "$(id -u)" == "0" ]]; then
    id postgres &>/dev/null || useradd -r -s /bin/false postgres
    chown -R postgres "$DATA"; RUNAS="postgres"
  fi
  PWFILE="$DATA/.pgpw"; printf '%s' "$PGPASSWORD_VAL" > "$PWFILE"
  [[ -n "$RUNAS" ]] && chown postgres "$PWFILE"
  run() { if [[ -n "$RUNAS" ]]; then su "$RUNAS" -s /bin/bash -c "$1"; else bash -c "$1"; fi; }
  run "'$PGBIN/initdb' -D '$PGDATA' -U '$PGUSER_NAME' --auth=scram-sha-256 --pwfile='$PWFILE' -E UTF8" >/dev/null
  rm -f "$PWFILE"
  echo "unix_socket_directories = '$DATA'" >> "$PGDATA/postgresql.conf"
  echo "listen_addresses = 'localhost'"    >> "$PGDATA/postgresql.conf"
  echo "port = $PGPORT"                    >> "$PGDATA/postgresql.conf"
  echo "fsync = off"                       >> "$PGDATA/postgresql.conf"  # dev only
fi

RUNAS=""; [[ "$(id -u)" == "0" ]] && RUNAS="postgres"
run() { if [[ -n "$RUNAS" ]]; then su "$RUNAS" -s /bin/bash -c "$1"; else bash -c "$1"; fi; }
chown -R postgres "$DATA" 2>/dev/null || true

if run "'$PGBIN/pg_ctl' -D '$PGDATA' status" >/dev/null 2>&1; then
  echo "==> postgres already running on :$PGPORT"
else
  echo "==> starting postgres on :$PGPORT"
  run "'$PGBIN/pg_ctl' -D '$PGDATA' -l '$DATA/pg.log' -w start" >/dev/null
fi

export PGPASSWORD="$PGPASSWORD_VAL"
if ! psql -h localhost -p "$PGPORT" -U "$PGUSER_NAME" -lqt 2>/dev/null | cut -d\| -f1 | grep -qw "$PGDB"; then
  echo "==> creating database $PGDB"
  createdb -h localhost -p "$PGPORT" -U "$PGUSER_NAME" "$PGDB"
fi

# --- Redis ------------------------------------------------------------------
if redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1; then
  echo "==> redis already running on :$REDIS_PORT"
else
  echo "==> starting redis on :$REDIS_PORT"
  redis-server --port "$REDIS_PORT" --daemonize yes --dir "$DATA" \
    --dbfilename dump.rdb --logfile "$DATA/redis.log" --save ''
  for _ in $(seq 1 30); do redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1 && break; sleep 0.2; done
fi

echo
echo "Ready:"
echo "  postgres  localhost:$PGPORT/$PGDB  (user: $PGUSER_NAME)"
echo "  redis     localhost:$REDIS_PORT"
echo
echo "Next:  make migrate && make seed"
