#!/bin/sh
set -e

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -h "$PRIMARY_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
    echo "waiting for primary at $PRIMARY_HOST"
    sleep 1
  done

  rm -rf "${PGDATA:?}"/*
  PGPASSWORD="$REPLICATION_PASSWORD" pg_basebackup \
    --host="$PRIMARY_HOST" \
    --username="$REPLICATION_USER" \
    --pgdata="$PGDATA" \
    --wal-method=stream \
    --slot="$REPLICATION_SLOT" \
    --checkpoint=fast \
    --write-recovery-conf \
    --progress
  chmod 700 "$PGDATA"
fi

exec postgres -c hot_standby=on -c hot_standby_feedback=on
