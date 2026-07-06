"""Postgres connection helper (psycopg 3). One entry point: connect()."""
import psycopg

from pipeline.settings import require_db


def connect() -> psycopg.Connection:
    """Autocommit=False; callers own transactions."""
    return psycopg.connect(require_db(), connect_timeout=15,
                           options="-c statement_timeout=120000",
                           keepalives=1, keepalives_idle=30,
                           keepalives_interval=10, keepalives_count=3)
