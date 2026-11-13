#!/usr/bin/env bash
# Container entrypoint: wait for DB -> apply migrations -> exec the app.
set -euo pipefail

log() { echo "[entrypoint] $*"; }

# ─── 1. wait for PostgreSQL ───
log "waiting for database..."
python - <<'PY'
import os, sys, time
import psycopg

url = os.environ.get("DATABASE_URL", "")
# psycopg wants a plain libpq URL, not the SQLAlchemy dialect form
url = url.replace("postgresql+psycopg://", "postgresql://")
if not url:
    print("[entrypoint] DATABASE_URL not set", file=sys.stderr)
    sys.exit(1)

deadline = time.time() + 120
last = ""
while time.time() < deadline:
    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        print("[entrypoint] database is ready")
        sys.exit(0)
    except Exception as exc:
        last = str(exc)
        time.sleep(2)
print(f"[entrypoint] database not ready after 120s: {last}", file=sys.stderr)
sys.exit(1)
PY

# ─── 2. migrations ───
# Alembic takes a transaction-level lock on alembic_version, so concurrent starts
# (or a restart racing a previous instance) cannot double-apply a revision.
if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  log "applying migrations..."
  attempts=0
  until alembic upgrade head; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 5 ]; then
      log "migrations failed after $attempts attempts"
      exit 1
    fi
    log "migration attempt $attempts failed; retrying in 5s..."
    sleep 5
  done
  log "migrations applied"
else
  log "RUN_MIGRATIONS=false, skipping"
fi

# ─── 3. run ───
if [ "${DASHBOARD_ENABLED:-true}" = "true" ]; then
  log "starting uvicorn on ${DASHBOARD_HOST:-0.0.0.0}:${DASHBOARD_PORT:-8000}"
  exec uvicorn app.main:app \
      --host "${DASHBOARD_HOST:-0.0.0.0}" \
      --port "${DASHBOARD_PORT:-8000}" \
      --no-access-log \
      --proxy-headers
else
  log "dashboard disabled; starting scheduler-only worker"
  exec python -m app.worker
fi

