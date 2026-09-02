"""
One-shot schema bootstrap.

On startup, applies schema.sql if the database looks empty (no `issues`
table yet), then never again. This is NOT a real migration system --
it only ever runs schema.sql once, against a fresh database. Schema
changes after the first deploy need their own migration step (e.g. a
hand-written `ALTER TABLE` run once, or a real migration tool like
Alembic if the schema starts changing often); editing schema.sql after
the database already exists has no effect here, by design -- it never
re-runs.
"""
import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger("uvicorn.error")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def run_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT to_regclass('public.issues')")).scalar()

    if exists:
        logger.info("xlr8r-archive: schema already present, skipping migration")
        return

    logger.info("xlr8r-archive: no schema found, applying schema.sql")
    sql = SCHEMA_PATH.read_text()

    # Raw DBAPI connection, not the ORM execute path: schema.sql is a
    # script of many semicolon-separated statements, and psycopg2's
    # cursor.execute() sends a whole multi-statement script to the
    # server in one round trip, which is what we want here.
    raw_conn = engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        try:
            cursor.execute(sql)
            raw_conn.commit()
            logger.info("xlr8r-archive: schema.sql applied successfully")
        except Exception:
            raw_conn.rollback()
            # Most likely two replicas started at once and the other one
            # already won this race and applied it. Don't crash the app
            # over that -- log it and move on; if the schema genuinely
            # failed to apply, every DB-touching request will surface
            # that clearly anyway.
            logger.exception("xlr8r-archive: schema.sql failed to apply (may already exist)")
        finally:
            cursor.close()
    finally:
        raw_conn.close()
