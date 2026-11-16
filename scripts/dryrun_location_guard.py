#!/usr/bin/env python3
"""DRY RUN of the location guard over public pins. Reads the database, writes nothing.

For a random sample of public reports (plus any ids passed with --ids-csv, e.g. the
hand-audited rows), replay the geocode ladder against Mapbox and compare three rules:

  old     today's rule: the first hit that is in India, in the right state and in the
          right district wins — whatever it is called
  top1    the same, but the hit must also share a distinctive name with the stated
          location (pipeline.processing.placematch.evidence)
  best5   as top1, but the provider's first five candidates are considered and the
          first one with name evidence wins

Output: one CSV row per report and a JSON summary, in --out. The Mapbox answers are
cached in --out so a re-run costs no calls. Nothing here touches the pipeline's own
geocode cache, the incident table, or the site.

    python3 scripts/dryrun_location_guard.py --sample 600 --out /path/to/dir
"""
import argparse
import csv
import json
import math
import os
import random
import sys
import time
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.processing import geocode as g            # noqa: E402
from pipeline.processing import placematch as pm        # noqa: E402

MAPBOX = "https://api.mapbox.com/search/geocode/v6/forward"
_last = 0.0


def km(lat1, lon1, lat2, lon2) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return round(6371.0 * 2 * math.asin(math.sqrt(a)), 1)


def candidates(query: str, cache: dict, token: str) -> list[dict]:
    """Up to five normalized Mapbox candidates for a query, cached in memory/file."""
    key = " ".join(query.lower().split())
    if key in cache:
        return cache[key]
    global _last
    wait = _last + 0.12 - time.time()
    if wait > 0:
        time.sleep(wait)
    _last = time.time()
    out = []
    try:
        r = httpx.get(MAPBOX, params={"q": query, "country": "IN", "limit": 5,
                                      "language": "en", "access_token": token}, timeout=20)
        feats = (r.json() or {}).get("features", []) if r.status_code == 200 else []
    except Exception:
        feats = []
    for f in feats:
        lon, lat = f["geometry"]["coordinates"]
        p = f.get("properties", {})
        ctx = p.get("context", {}) or {}
        bbox = p.get("bbox") or f.get("bbox")
        out.append({
            "lat": float(lat), "lon": float(lon),
            "state": (ctx.get("region") or {}).get("name") or "",
            "district": ((ctx.get("district") or {}).get("name")
                         or (ctx.get("place") or {}).get("name") or ""),
            "name": p.get("name") or "", "ftype": p.get("feature_type") or "",
            "ctx": {k: (ctx.get(k) or {}).get("name") or ""
                    for k in ("street", "neighborhood", "locality", "place")},
            "span_km": g._bbox_span_km(bbox[1], bbox[0], bbox[3], bbox[2]) if bbox else 0.0,
        })
    cache[key] = out
    return out


def _district_towns() -> frozenset:
    """Every district name in the config spine, as name tokens — a road named after
    one of these ('Sitapur Road') is a direction, not a location."""
    path = Path(__file__).resolve().parent.parent / "config" / "config_districts_master.csv"
    out = set()
    for r in csv.DictReader(open(path)):
        out |= pm.name_tokens(r["district"])
    return frozenset(out)


OTHER_TOWNS = _district_towns()


def passes_admin(hit: dict, district, state) -> bool:
    return (g._in_india(hit["lat"], hit["lon"]) and g._state_ok(hit["state"], state)
            and g._district_ok(hit.get("district", ""), district))


def replay(row: dict, cache: dict, token: str) -> dict:
    loc, road, city = row["loc"], row["road"], row["city"]
    district, state = row["district"], row["state"]
    stated = pm.stated_names(loc, road, city, district, state, OTHER_TOWNS)
    res = {"old": None, "top1": None, "best5": None}
    for query, method, conf in g._variants(loc, road, city, district, state):
        cands = candidates(query, cache, token)
        ok = [h for h in cands if passes_admin(h, district, state)]
        if res["old"] is None and cands and passes_admin(cands[0], district, state):
            h = cands[0]
            res["old"] = {**h, "method": method, "conf": conf}
        if res["top1"] is None and cands and passes_admin(cands[0], district, state):
            prec, word = pm.evidence(stated, cands[0])
            if prec:
                res["top1"] = {**cands[0], "method": method, "precision": prec, "word": word}
        if res["best5"] is None:
            for h in ok:
                prec, word = pm.evidence(stated, h)
                if prec:
                    res["best5"] = {**h, "method": method, "precision": prec, "word": word}
                    break
        if all(res.values()):
            break
    return {"stated": sorted(stated.road | stated.place), **res}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=600)
    ap.add_argument("--seed", default="2026-11-16")
    ap.add_argument("--ids-csv", help="CSV with an incident_id column to include as well")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / "mapbox_candidates_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    token = os.environ["MAPBOX_TOKEN"]

    extra = []
    if args.ids_csv:
        extra = [int(r["incident_id"]) for r in csv.DictReader(open(args.ids_csv))
                 if r.get("incident_id", "").isdigit()]
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=20,
                         autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("set default_transaction_read_only = on")
        cur.execute("set statement_timeout = '60s'")
        sel = """select i.id, i.location_text_best, i.road_name, i.admin_city,
                        coalesce(i.admin_district, a.district),
                        coalesce(i.admin_state, a.state),
                        st_y(i.geom::geometry), st_x(i.geom::geometry), i.geocode_method, i.geocode_confidence,
                        (p.id is not null) as public
                 from incident i
                 left join public_incident p on p.id = i.id
                 left join source_article a on a.id = i.primary_source_id """
        cur.execute(sel + "where p.id is not null order by md5(i.id::text || %s) limit %s",
                    (args.seed, args.sample))
        rows = cur.fetchall()
        if extra:
            cur.execute(sel + "where i.id = any(%s)", (extra,))
            have = {r[0] for r in rows}
            rows += [r for r in cur.fetchall() if r[0] not in have]
    keys = ("id", "loc", "road", "city", "district", "state", "lat", "lon", "method",
            "conf", "public")
    rows = [dict(zip(keys, r)) for r in rows]
    extra_set = set(extra)
    print(f"{len(rows)} report(s): {args.sample} sampled + {len(extra_set)} listed")

    results = []
    for n, row in enumerate(rows, 1):
        rep = replay(row, cache, token)
        rec = {"id": row["id"], "sampled": row["id"] not in extra_set or n <= args.sample,
               "listed": row["id"] in extra_set, "public": row["public"],
               "loc": row["loc"], "district": row["district"],
               "db_lat": row["lat"], "db_lon": row["lon"], "db_method": row["method"],
               "stated": " ".join(rep["stated"])}
        for rule in ("old", "top1", "best5"):
            h = rep[rule]
            rec[f"{rule}_name"] = h and h["name"]
            rec[f"{rule}_ftype"] = h and h["ftype"]
            rec[f"{rule}_place"] = h and (h["ctx"].get("locality") or h["ctx"].get("place"))
            rec[f"{rule}_km_from_db"] = h and km(row["lat"], row["lon"], h["lat"], h["lon"])
            if rule != "old":
                rec[f"{rule}_precision"] = h["precision"] if h else "none"
                rec[f"{rule}_word"] = h and h["word"]
                rec[f"{rule}_lat"] = h and h["lat"]
                rec[f"{rule}_lon"] = h and h["lon"]
        results.append(rec)
        if n % 50 == 0:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False))
            print(f"  {n}/{len(rows)} · {len(cache)} queries cached")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False))

    with open(out / "location_guard_dryrun.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    random.seed(1)
    sample = [r for r in results if r["sampled"]][:args.sample]
    n = len(sample)

    def share(pred):
        k = sum(1 for r in sample if pred(r))
        return {"n": k, "pct": round(100.0 * k / n, 1)}

    summary = {"sampled": n, "mapbox_queries": len(cache)}
    for rule in ("top1", "best5"):
        summary[rule] = {c: share(lambda r, c=c: r[f"{rule}_precision"] == c)
                         for c in ("spot", "road", "area", "highway", "none")}
        summary[rule]["kept_within_2km_of_current_pin"] = share(
            lambda r: r[f"{rule}_precision"] not in ("none", "highway")
            and float(r[f"{rule}_km_from_db"] or 0) <= 2)
        summary[rule]["moved_over_10km"] = share(
            lambda r: r[f"{rule}_precision"] not in ("none", "highway")
            and float(r[f"{rule}_km_from_db"] or 0) > 10)
    summary["old_rule_reproduces_current_pin_within_2km"] = share(
        lambda r: r["old_km_from_db"] is not None and r["old_km_from_db"] <= 2)
    summary["old_hit_named_only_generic_words"] = share(
        lambda r: r["old_name"] is not None and not pm.name_tokens(r["old_name"]))
    (out / "location_guard_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
