#!/usr/bin/env python3
"""One-off repair: some incidents stored a roundup's totals as if they were one crash's
casualties ("Jaipur recorded 560 road accident deaths in 10 months"). On 2 Nov 2026
three such public rows supplied 1,170 of the site's 1,827 "deaths on record". Zero the
casualties of every incident whose summary reads as a period/area total (same predicate
coerce_incident now applies to new extractions). The figure stays in narrative_summary.
Audit-logs every change to review_action, so each row can be restored.

Dry run by default: lists every row it would change and the public before/after totals.

Usage: python -m scripts.repair_aggregate_casualties [--apply] [--skip 123,11693]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import connect                                 # noqa: E402
from pipeline.store import looks_like_aggregate_casualties     # noqa: E402


def _skip_ids(argv: list[str]) -> set[int]:
    if "--skip" not in argv:
        return set()
    return {int(x) for x in argv[argv.index("--skip") + 1].split(",") if x.strip()}


def main() -> None:
    apply = "--apply" in sys.argv
    skip = _skip_ids(sys.argv)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select i.id, i.fatalities, i.injuries, i.narrative_summary,
                 i.id in (select id from public_incident) as is_public
          from incident i
          where i.fatalities + i.injuries > 0
          order by i.fatalities desc, i.id""")
        rows = cur.fetchall()
        hits = [r for r in rows
                if r[0] not in skip and looks_like_aggregate_casualties(r[3], r[1], r[2])]
        cur.execute("select coalesce(sum(fatalities),0), coalesce(sum(injuries),0), "
                    "count(*) filter (where fatalities + injuries > 0) from public_incident")
        pub_f, pub_i, pub_cas = cur.fetchone()
        pub_hits = [r for r in hits if r[4]]
        print(f"{len(hits)} incident(s) read as roundups ({len(pub_hits)} public); "
              f"skipped by hand: {sorted(skip) or 'none'}")
        for iid, f, i, summary, is_public in hits:
            print(f"  {'PUBLIC ' if is_public else '       '}#{iid}: F{f} I{i}  {summary[:110]}")
        print(f"public deaths {pub_f} -> {pub_f - sum(r[1] for r in pub_hits)}, "
              f"injuries {pub_i} -> {pub_i - sum(r[2] for r in pub_hits)}, "
              f"reports with casualties {pub_cas} -> {pub_cas - len(pub_hits)}")
        if not apply:
            print("dry run: nothing written (pass --apply)")
            return
        for iid, f, i, _summary, _pub in hits:
            cur.execute("update incident set fatalities=0, injuries=0, updated_at=now() "
                        "where id=%s", (iid,))
            cur.execute("""insert into review_action
                (entity_type, entity_id, reviewer, action, before_json, after_json, note)
                values ('incident',%s,'pipeline:aggregate_casualties','edit',%s,%s,
                        'roundup totals are not one crash''s casualties; figure kept in summary')""",
                        (iid, json.dumps({"fatalities": f, "injuries": i}),
                         json.dumps({"fatalities": 0, "injuries": 0})))
        conn.commit()
        print(f"applied: {len(hits)} incident(s) zeroed; hotspot totals update on the next "
              f"recompute + export (the daily run)")


if __name__ == "__main__":
    main()
