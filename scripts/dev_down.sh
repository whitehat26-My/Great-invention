#!/usr/bin/env bash
# Stop the local dev services started by dev_up.sh. Data under .devdata/ is kept
# unless --purge is passed.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/.devdata"
REDIS_PORT="${REDIS_PORT:-6380}"
PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)"

if [[ -n "$PGBIN" && -s "$DATA/pg/PG_VERSION" ]]; then
  RUNAS=""; [[ "$(id -u)" == "0" ]] && RUNAS="postgres"
  cmd="'$PGBIN/pg_ctl' -D '$DATA/pg' -m fast stop"
  if [[ -n "$RUNAS" ]]; then su "$RUNAS" -s /bin/bash -c "$cmd" || true; else bash -c "$cmd" || true; fi
fi
redis-cli -p "$REDIS_PORT" shutdown nosave 2>/dev/null || true

if [[ "${1:-}" == "--purge" ]]; then rm -rf "$DATA"; echo "purged $DATA"; fi
echo "stopped"
