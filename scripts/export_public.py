#!/usr/bin/env python3
"""Export the PUBLIC views to static JSON for the dashboard (web/public/data/).

The public site never touches the DB: this export is the only bridge, and it reads
ONLY public_* views — the §8 gate enforced at the data layer. Run after recompute.

Usage: python -m scripts.export_public
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import connect          # noqa: E402
from pipeline.settings import ROOT       # noqa: E402

OUT = ROOT / "web" / "public" / "data"


def tier_of(score, tiers):
    if score is None:
        return "watch"
    for name in ("critical", "high", "medium", "watch"):
        if score >= tiers[name]:
            return name
    return "watch"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select value from config_setting where key='priority_tiers'")
        tiers = cur.fetchone()[0]
        cur.execute("""select code, label from config_defect_taxonomy""")
        labels = dict(cur.fetchall())

        # ---- hotspots (public members only) -> GeoJSON
        cur.execute("""
          select h.id, ST_X(h.centroid_geom::geometry), ST_Y(h.centroid_geom::geometry),
                 h.road_name, h.admin_state, h.admin_district, h.admin_city,
                 h.n_public_incidents, h.public_fatalities, h.public_injuries,
                 h.first_crash_date, h.last_crash_date, h.dominant_defects,
                 h.priority_score, h.score_breakdown, h.escalation_candidate, h.status
          from public_hotspot h""")
        feats = []
        for (hid, lon, lat, road, st, dist, city, n, fat, inj, d0, d1,
             dom, score, breakdown, esc, status) in cur.fetchall():
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                "properties": {
                    "id": hid, "road_name": road, "state": st, "district": dist,
                    "city": city, "incidents": n, "fatalities": fat, "injuries": inj,
                    "first": str(d0) if d0 else None, "last": str(d1) if d1 else None,
                    "defects": dom or [], "score": score,
                    "tier": tier_of(score, tiers),
                    "breakdown": breakdown, "escalation": esc, "status": status,
                }})
        (OUT / "hotspots.geojson").write_text(json.dumps(
            {"type": "FeatureCollection", "features": feats}, ensure_ascii=False))

        # ---- public incidents with defects + all sources
        cur.execute("""
          select i.id, i.cluster_id, i.crash_date, i.location_text_best, i.road_name,
                 i.road_type, i.admin_state, i.admin_district, i.fatalities, i.injuries,
                 i.victim_types, i.narrative_summary, i.verification_status,
                 i.extraction_confidence, i.geocode_confidence, i.geocode_method
          from public_incident i""")
        cols = ["id", "hotspot_id", "date", "location", "road_name", "road_type",
                "state", "district", "fatalities", "injuries", "victim_types",
                "summary", "verification", "extraction_conf", "geocode_conf",
                "geocode_method"]
        incidents = []
        for r in cur.fetchall():
            inc = dict(zip(cols, r))
            inc["date"] = str(inc["date"]) if inc["date"] else None
            cur.execute("""
              select d.defect_type, d.defect_confidence, d.evidence_snippet
              from public_incident_defect d where d.incident_id=%s""", (inc["id"],))
            inc["defects"] = [{"type": t, "label": labels.get(t, t), "conf": c,
                               "evidence": s} for t, c, s in cur.fetchall()]
            cur.execute("""
              select a.url, a.outlet_name, a.published_at::date
              from incident_source s join source_article a on a.id = s.source_article_id
              where s.incident_id=%s order by a.published_at""", (inc["id"],))
            inc["sources"] = [{"url": u, "outlet": o, "date": str(d) if d else None}
                              for u, o, d in cur.fetchall()]
            incidents.append(inc)
        (OUT / "incidents.json").write_text(json.dumps(incidents, ensure_ascii=False))

        meta = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "hotspots": len(feats),
            "incidents": len(incidents),
            "fatalities": sum(i["fatalities"] for i in incidents),
            "injuries": sum(i["injuries"] for i in incidents),
            "states": sorted({f["properties"]["state"] for f in feats
                              if f["properties"]["state"]}),
            "defect_labels": labels,
            "tiers": tiers,
        }
        (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    print(f"exported: {len(feats)} hotspots · {len(incidents)} incidents -> {OUT}")


if __name__ == "__main__":
    main()
