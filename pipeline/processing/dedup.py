"""Article→incident dedup (BUILD_SPEC §7.5): merge incidents describing the SAME crash.

Match rule (pure function, unit-tested): crash_date within ±1 day AND spatial distance
≤ dedup.spatial_window_m AND casualties compatible (|Δfatalities| ≤ 1, |Δinjuries| ≤ 3).
Undated or ungeocoded incidents never auto-merge (too risky — reviewer can merge).

Merge keeps the higher-extraction-confidence incident as canonical, moves sources and
defects across (deduped by type, max confidence), and audit-logs to review_action.
"""
import json
from datetime import date

from pipeline import configload
from pipeline.db import connect


def is_same_crash(a: dict, b: dict, dist_m: float, spatial_window_m: float = 1000,
                  date_window_days: int = 1) -> bool:
    """Pure decision rule — a/b need: crash_date (date|None), fatalities, injuries."""
    if not a.get("crash_date") or not b.get("crash_date"):
        return False
    dd = abs((a["crash_date"] - b["crash_date"]).days)
    if dd > date_window_days:
        return False
    if dist_m is None or dist_m > spatial_window_m:
        return False
    if abs((a.get("fatalities") or 0) - (b.get("fatalities") or 0)) > 1:
        return False
    if abs((a.get("injuries") or 0) - (b.get("injuries") or 0)) > 3:
        return False
    return True


def find_pairs(conn) -> list[tuple[int, int, float]]:
    """Candidate (keep_id, merge_id, dist_m) pairs, keep = higher confidence."""
    cfg = configload.settings()["dedup"]
    with conn.cursor() as cur:
        cur.execute("""
          select a.id, b.id, ST_Distance(a.geom, b.geom),
                 a.crash_date, b.crash_date, a.fatalities, b.fatalities,
                 a.injuries, b.injuries, a.extraction_confidence, b.extraction_confidence
          from incident a
          join incident b on a.id < b.id
            and a.crash_date is not null and b.crash_date is not null
            and abs(a.crash_date - b.crash_date) <= %s
            and a.geom is not null and b.geom is not null
            and ST_DWithin(a.geom, b.geom, %s)""",
            (cfg["date_window_days"], cfg["spatial_window_m"]))
        out = []
        for (ida, idb, dist, da, db_, fa, fb, ia, ib, ca, cb) in cur.fetchall():
            a = {"crash_date": da, "fatalities": fa, "injuries": ia}
            b = {"crash_date": db_, "fatalities": fb, "injuries": ib}
            if is_same_crash(a, b, dist, cfg["spatial_window_m"], cfg["date_window_days"]):
                keep, merge = (ida, idb) if (ca or 0) >= (cb or 0) else (idb, ida)
                out.append((keep, merge, dist))
        return out


def merge_incident(conn, keep_id: int, merge_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("select row_to_json(i) from incident i where id=%s", (merge_id,))
        before = cur.fetchone()[0]
        # sources across (match_confidence 0.9 = auto-merged)
        cur.execute("""
          insert into incident_source (incident_id, source_article_id, match_confidence)
          select %s, source_article_id, 0.9 from incident_source where incident_id=%s
          on conflict do nothing""", (keep_id, merge_id))
        # defects across, then dedupe by type keeping max confidence
        cur.execute("update incident_defect set incident_id=%s where incident_id=%s",
                    (keep_id, merge_id))
        cur.execute("""
          delete from incident_defect a using incident_defect b
          where a.incident_id=b.incident_id and a.defect_type=b.defect_type
            and (a.defect_confidence < b.defect_confidence
                 or (a.defect_confidence = b.defect_confidence and a.id > b.id))
            and a.incident_id=%s""", (keep_id,))
        cur.execute("delete from incident where id=%s", (merge_id,))
        cur.execute("""
          insert into review_action (entity_type, entity_id, reviewer, action, before_json, note)
          values ('incident', %s, 'pipeline:dedup', 'merge', %s::jsonb, %s)""",
            (keep_id, json.dumps(before, default=str),
             f"auto-merged incident #{merge_id} into #{keep_id}"))
    conn.commit()


def run() -> dict:
    merged = 0
    with connect() as conn:
        pairs = find_pairs(conn)
        for keep, merge, dist in pairs:
            print(f"  merging #{merge} -> #{keep} ({dist:.0f} m apart)")
            merge_incident(conn, keep, merge)
            merged += 1
    return {"pairs_found": len(pairs), "merged": merged}
