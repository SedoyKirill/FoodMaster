#!/bin/sh
# Container entrypoint shared by the `api` and `scheduler` services.
#
# Ordering matters after a Windows reboot: Docker restarts containers by
# restart policy and IGNORES depends_on, so `scheduler` may well come up before
# `db` is accepting connections and before `api` has migrated anything. Hence
# both waits below run unconditionally rather than trusting compose ordering.
set -eu

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT_INTERNAL:-5432}"
WAIT_DB_SECONDS="${WAIT_DB_SECONDS:-90}"
WAIT_SCHEMA_SECONDS="${WAIT_SCHEMA_SECONDS:-180}"

log() { echo "[entrypoint] $*" >&2; }

wait_for_db() {
  log "waiting for postgres at ${DB_HOST}:${DB_PORT} (up to ${WAIT_DB_SECONDS}s)"
  elapsed=0
  until pg_isready -h "$DB_HOST" -p "$DB_PORT" -q; do
    if [ "$elapsed" -ge "$WAIT_DB_SECONDS" ]; then
      log "postgres did not become ready in ${WAIT_DB_SECONDS}s"
      exit 1
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  log "postgres is ready"
}

wait_for_schema() {
  log "waiting for migrations to be applied (up to ${WAIT_SCHEMA_SECONDS}s)"
  elapsed=0
  until python -m app.core.migrate --check; do
    if [ "$elapsed" -ge "$WAIT_SCHEMA_SECONDS" ]; then
      log "schema was not ready in ${WAIT_SCHEMA_SECONDS}s"
      exit 1
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  log "schema is ready"
}

wait_for_db

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  log "applying migrations"
  python -m app.core.migrate
else
  wait_for_schema
fi

log "starting: $*"
exec "$@"
