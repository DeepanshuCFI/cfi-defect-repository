#!/usr/bin/env python3
"""Re-check every public pin against the name-evidence guard. DRY RUN unless --apply-from.

Two phases, deliberately separate, so that what is applied is exactly what was reviewed:

  1. PLAN (default, read-only)
       python3 scripts/repair_location_guard.py --out DIR
     Re-geocodes each public report with the current geocode() — the name guard
     included — and writes DIR/plan.jsonl (one line per report: before, after, outcome)
     and DIR/summary.json. Nothing is written to the database. The provider answers
     go to DIR/geocode_cache.json, a throwaway cache: the pipeline's own cache holds
     answers in the pre-guard shape and must not be touched by a dry run.

  2. APPLY (writes; needs the owner's go)
       python3 scripts/repair_location_guard.py --apply-from DIR/plan.jsonl
     Applies the plan line by line. A report whose pin has changed since the plan was
     made is skipped, not overwritten. Every change is audit-logged in review_action
     with the before and after values (reviewer 'pipeline:location_guard'), so each
     one is individually reversible. Migration 018 must already be applied.

Outcomes per report:
  keep     the guard accepts a pin within 2 km of the current one (class recorded)
  move     the guard accepts a pin elsewhere (the place the article actually names)
  unpin    nothing in the provider's index names the place — the report stays public
           and is listed by district without a marker
  highway  only a numbered highway matched — same treatment, highway recorded

Scope: public reports in the covered states (--all-states for every public report).
Non-public records are not touched here; they are not on the map.
"""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.processing import geocode as g            # noqa: E402

REVIEWER = "pipeline:location_guard"
KEEP_WITHIN_KM = 2.0
SELECT = """
  select i.id, i.location_text_best, i.road_name, i.admin_city,
         coalesce(i.admin_district, a.district), coalesce(i.admin_state, a.state),
         st_y(i.geom::geometry), st_x(i.geom::geometry),
         i.geocode_confidence, i.geocode_method, i.geocode_qualifier, i.cluster_id
  from public_incident p
  join incident i on i.id = p.id
  left join source_article a on a.id = i.primary_source_id"""


def km(lat1, lon1, lat2, lon2) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return round(6371.0 * 2 * math.asin(math.sqrt(a)), 2)


def outcome(before: dict, after: dict) -> str:
    """keep | move | unpin | highway | none (pure). 'none' = had no pin, still has none."""
    if after.get("lat") is None:
        if before.get("lat") is None:
            return "none"
        return "highway" if after.get("geocode_precision") == "highway" else "unpin"
    d = km(before.get("lat"), before.get("lon"), after["lat"], after["lon"])
    return "keep" if d is not None and d <= KEEP_WITHIN_KM else "move"


def _connect(read_only: bool):
    conn = psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=20)
    with conn.cursor() as cur:
        if read_only:
            cur.execute("set default_transaction_read_only = on")
        cur.execute("set statement_timeout = '60s'")
    conn.commit()
    return conn


def plan(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # throwaway provider cache — see the module docstring
    g.CACHE_PATH = out / "geocode_cache.json"
    g._cache = None
    saved_at = [time.time()]
    real_save = g._save_cache

    def lazy_save():                     # the cache is rewritten whole; not on every call
        if time.time() - saved_at[0] > 60:
            real_save()
            saved_at[0] = time.time()
    g._save_cache = lazy_save

    with _connect(read_only=True) as conn, conn.cursor() as cur:
        cur.execute("select value from config_setting where key='ingestion'")
        row = cur.fetchone()
        states = (row[0] or {}).get("daily_states", []) if row else []
        sql, params = SELECT, []
        where = []
        if states and not args.all_states:
            where.append("coalesce(i.admin_state, a.state) = any(%s)")
            params.append(states)
        if args.ids:
            where.append("i.id = any(%s)")
            params.append([int(x) for x in args.ids.split(",")])
        if where:
            sql += " where " + " and ".join(where)
        sql += " order by i.id"
        if args.limit:
            sql += " limit %s"
            params.append(args.limit)
        cur.execute(sql, params)
        rows = cur.fetchall()

    done = {}
    plan_path = out / "plan.jsonl"
    if plan_path.exists() and args.resume:
        for line in plan_path.read_text().splitlines():
            rec = json.loads(line)
            done[rec["id"]] = rec
    print(f"{len(rows)} public report(s) in scope · {len(done)} already planned")
    with open(plan_path, "a" if args.resume else "w") as fh:
        for n, (iid, loc, road, city, dist, state, lat, lon, conf, method, qual, cluster) in enumerate(rows, 1):
            if iid in done:
                continue
            before = {"lat": lat, "lon": lon, "geocode_confidence": conf,
                      "geocode_method": method, "geocode_qualifier": qual, "cluster_id": cluster}
            r = g.geocode(loc or "", road_name=road, admin_city=city, admin_district=dist,
                          admin_state=state)
            after = {k: r.get(k) for k in ("lat", "lon", "geocode_confidence", "geocode_method",
                                           "geocode_qualifier", "geocode_precision", "geocode_match")}
            rec = {"id": iid, "state": state, "district": dist, "location": loc,
                   "before": before, "after": after, "outcome": outcome(before, after),
                   "moved_km": km(lat, lon, after["lat"], after["lon"])}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            done[iid] = rec
            if n % 250 == 0:
                print(f"  {n}/{len(rows)}", flush=True)
    real_save()
    summarize(list(done.values()), out)


def summarize(recs: list[dict], out: Path) -> dict:
    n = len(recs) or 1
    count = lambda pred: sum(1 for r in recs if pred(r))                      # noqa: E731
    s = {"reports": len(recs),
         "outcome": {o: count(lambda r, o=o: r["outcome"] == o)
                     for o in ("keep", "move", "unpin", "highway", "none")},
         "precision_after": {c: count(lambda r, c=c: r["after"].get("geocode_precision") == c)
                             for c in ("spot", "road", "locality", "town", "highway")},
         "moved_over_10km": count(lambda r: r["outcome"] == "move" and (r["moved_km"] or 0) > 10),
         "below_publish_bar_after": count(lambda r: r["after"].get("lat") is not None
                                          and (r["after"].get("geocode_confidence") or 0) < 0.6)}
    s["share"] = {k: round(100.0 * v / n, 1) for k, v in s["outcome"].items()}
    (out / "summary.json").write_text(json.dumps(s, indent=2))
    print(json.dumps(s, indent=2))
    return s


def apply(args) -> None:
    recs = [json.loads(line) for line in Path(args.apply_from).read_text().splitlines() if line.strip()]
    todo = [r for r in recs if r["outcome"] != "none"]
    print(f"applying {len(todo)} planned change(s) from {args.apply_from}")
    stats = {"applied": 0, "skipped_pin_changed": 0, "skipped_missing": 0}
    conn = _connect(read_only=False)
    try:
        for n, r in enumerate(todo, 1):
            b, a = r["before"], r["after"]
            with conn.cursor() as cur:
                cur.execute("""select st_y(geom::geometry), st_x(geom::geometry)
                               from incident where id = %s for update""", (r["id"],))
                row = cur.fetchone()
                if row is None:
                    stats["skipped_missing"] += 1
                    conn.rollback()
                    continue
                d = km(row[0], row[1], b["lat"], b["lon"])
                if (row[0] is None) != (b["lat"] is None) or (d is not None and d > 0.05):
                    stats["skipped_pin_changed"] += 1      # geocoded again since the plan
                    conn.rollback()
                    continue
                if a["lat"] is None:
                    cur.execute("""update incident set geom = null, geocode_confidence = null,
                                   geocode_method = null, geocode_qualifier = null,
                                   geocode_precision = %s, geocode_match = %s, cluster_id = null,
                                   geocode_attempted_at = now(), updated_at = now()
                                   where id = %s""",
                                (a["geocode_precision"], a["geocode_match"], r["id"]))
                else:
                    cur.execute("""update incident set geom = ST_GeogFromText(%s),
                                   geocode_confidence = %s, geocode_method = %s,
                                   geocode_qualifier = %s, geocode_precision = %s,
                                   geocode_match = %s, geocode_attempted_at = now(),
                                   updated_at = now() where id = %s""",
                                (f"POINT({a['lon']} {a['lat']})", a["geocode_confidence"],
                                 a["geocode_method"], a["geocode_qualifier"],
                                 a["geocode_precision"], a["geocode_match"], r["id"]))
                cur.execute("""insert into review_action (entity_type, entity_id, reviewer, action,
                               before_json, after_json, note)
                               values ('incident', %s, %s, 'edit', %s::jsonb, %s::jsonb, %s)""",
                            (r["id"], REVIEWER, json.dumps(b), json.dumps(a),
                             f"location guard: {r['outcome']}"
                             + (f", moved {r['moved_km']} km" if r["outcome"] == "move" else "")
                             + (f" — pinned to {a['geocode_match']} ({a['geocode_precision']})"
                                if a["lat"] is not None else " — no name evidence for a pin")))
            conn.commit()
            stats["applied"] += 1
            if n % 250 == 0:
                print(f"  {n}/{len(todo)} {stats}", flush=True)
    finally:
        conn.close()
    print(f"DONE {stats}. Next: python3 -m pipeline.run recompute, then the export.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", help="directory for plan.jsonl, summary.json and the throwaway cache")
    ap.add_argument("--apply-from", help="apply a reviewed plan.jsonl (WRITES to the database)")
    ap.add_argument("--all-states", action="store_true", help="include reports outside the covered states")
    ap.add_argument("--ids", help="comma-separated incident ids only")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="continue an interrupted plan in --out")
    args = ap.parse_args()
    if args.apply_from:
        return apply(args)
    if not args.out:
        ap.error("--out is required for a plan (dry run)")
    plan(args)


if __name__ == "__main__":
    main()
