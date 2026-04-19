FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# NOTE: migrations used to run at build time against a baked-in SQLite file.
# That's gone — the DB is now external (Cloud SQL Postgres in production),
# so there's nothing to migrate against during `docker build`. All schema +
# seed work happens at container boot via scripts/startup.py, which is
# idempotent and preserves user-edited data across restarts.

EXPOSE 8080

# On boot: run migrations against whatever CRAIG_DATABASE_URL points at,
# then hand off to uvicorn. If the migration script fails the container
# exits non-zero and Cloud Run keeps the previous healthy revision.
# Cloud Run sets $PORT; default to 8080 for local docker runs.
CMD python -m scripts.startup && uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}
