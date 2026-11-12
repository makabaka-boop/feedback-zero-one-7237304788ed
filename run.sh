#!/usr/bin/env bash
# Convenience launcher for a local, root-less PostgreSQL + the service.
set -euo pipefail

PGBIN="${PGBIN:-/tmp/pgprefix/usr/lib/postgresql/15/bin}"
PGDATA="${PGDATA:-/tmp/pgdata}"
export APP_DSN="${APP_DSN:-postgresql://app@/appdb?host=/tmp&port=55432}"

if ! "$PGBIN/pg_isready" -h /tmp -p 55432 >/dev/null 2>&1; then
  if [ ! -d "$PGDATA/base" ]; then
    "$PGBIN/initdb" -D "$PGDATA" -U app --auth=trust --no-locale --encoding=UTF8
    printf "unix_socket_directories = '/tmp'\nlisten_addresses = ''\nport = 55432\n" \
      >> "$PGDATA/postgresql.conf"
  fi
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/server.log" start
  for _ in $(seq 1 30); do
    "$PGBIN/pg_isready" -h /tmp -p 55432 >/dev/null 2>&1 && break
    sleep 0.3
  done
  "$PGBIN/psql" -h /tmp -p 55432 -U app -d postgres -tc \
    "SELECT 1 FROM pg_database WHERE datname='appdb'" | grep -q 1 \
    || "$PGBIN/psql" -h /tmp -p 55432 -U app -d postgres -c "CREATE DATABASE appdb"
fi

cd "$(dirname "$0")"
python3 -m scripts.admin init-schema
exec python3 -m app.httpapi
