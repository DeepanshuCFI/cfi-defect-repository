#!/usr/bin/env python3
"""Run SQL migrations in order, tracked in schema_migrations.

Usage:
  python -m scripts.migrate            # apply pending
  python -m scripts.migrate --status   # show applied/pending
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import connect  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def main() -> None:
    status_only = "--status" in sys.argv
    files = sorted(MIGRATIONS_DIR.glob("[0-9]*.sql"))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "create table if not exists schema_migrations "
            "(name text primary key, applied_at timestamptz not null default now())"
        )
        conn.commit()
        cur.execute("select name from schema_migrations")
        applied = {r[0] for r in cur.fetchall()}
        for f in files:
            if f.name in applied:
                print(f"  applied  {f.name}")
                continue
            if status_only:
                print(f"  PENDING  {f.name}")
                continue
            print(f"  applying {f.name} …", flush=True)
            cur.execute(f.read_text())
            cur.execute("insert into schema_migrations (name) values (%s)", (f.name,))
            conn.commit()
            print(f"  applied  {f.name}")
    print("done.")


if __name__ == "__main__":
    main()
