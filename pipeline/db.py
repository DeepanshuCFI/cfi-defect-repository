"""Postgres connection helper (psycopg 3). One entry point: connect()."""
import psycopg

from pipeline.settings import require_db


def connect() -> psycopg.Connection:
    """Autocommit=False; callers own transactions."""
    return psycopg.connect(require_db())
