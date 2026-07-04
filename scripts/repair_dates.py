#!/usr/bin/env python3
"""One-off repair: crash_date was extracted without the article's publication date,
so relative dates ('on Friday') got the wrong year. Re-extract ONLY the date for
incidents where crash_date is null or >30 days before publication, using the fixed
prompt. Audit-logs every change to review_action.

Usage: python -m scripts.repair_dates [--dry]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import connect                    # noqa: E402
from pipeline.processing.extract import extract    # noqa: E402


def main() -> None:
    dry = "--dry" in sys.argv
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select i.id, i.crash_date, a.published_at::date, a.clean_text,
                 left(a.clean_text, 140)
          from incident i
          join source_article a on a.id = i.primary_source_id
          where a.clean_text is not null and a.published_at is not null
            and (i.crash_date is null
                 or a.published_at::date - i.crash_date > 30)
          order by i.id""")
        rows = cur.fetchall()
        print(f"{len(rows)} incident(s) with suspicious crash_date…")
        fixed = kept = 0
        for iid, old_date, pub, text, title in rows:
            try:
                res, _ = extract(title, text, published_at=str(pub))
            except Exception as e:
                print(f"  #{iid} WARN re-extract failed: {e}")
                continue
            new_date = res.get("crash_date")
            if str(new_date) == str(old_date):
                kept += 1
                continue
            print(f"  #{iid}: {old_date} -> {new_date}  (published {pub})")
            if not dry:
                cur.execute("update incident set crash_date=%s, updated_at=now() where id=%s",
                            (new_date, iid))
                cur.execute("""insert into review_action
                    (entity_type, entity_id, reviewer, action, before_json, after_json, note)
                    values ('incident',%s,'pipeline:date_repair','edit',%s,%s,
                            'crash_date re-derived with publication date in prompt')""",
                            (iid, json.dumps({"crash_date": str(old_date)}),
                             json.dumps({"crash_date": str(new_date)})))
                conn.commit()
            fixed += 1
        print(f"done: {fixed} corrected, {kept} confirmed-old (kept), dry={dry}")


if __name__ == "__main__":
    main()
