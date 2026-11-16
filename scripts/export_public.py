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
from pipeline.processing.score import quantile_thresholds  # noqa: E402
from pipeline.settings import ROOT       # noqa: E402

OUT = ROOT / "web" / "public" / "data"


# What the public site shows beyond the data itself. Read from config_setting key
# 'public_site' (synced from config/config_settings.json); these are the defaults when
# the key is absent. Both were switched on 16 Nov 2026 after the location audit:
#   coverage_only      only states the collector actually covers appear on the site and
#                      in its headline numbers. 297 reports from other states — 4% of
#                      reports — carried 181 of the 613 deaths in the headline.
#   repeat_crash_flag  the red "Repeat crashes" flag. Withdrawn: of 671 flags, none
#                      could be shown to be three crashes at one spot; the votes were
#                      crashes from across a district sharing one geocoder point.
PUBLIC_SITE_DEFAULTS = {"coverage_only": True, "repeat_crash_flag": False}

# Coarsest last. A location is only as precise as its least precise report.
PRECISION_RANK = {"spot": 0, "road": 1, "locality": 2, "town": 3}


def public_site_settings(value) -> dict:
    """Defaults overlaid with whatever valid booleans the config row carries."""
    out = dict(PUBLIC_SITE_DEFAULTS)
    if isinstance(value, dict):
        out.update({k: bool(v) for k, v in value.items() if k in out})
    return out


def in_coverage(state, states, coverage_only: bool = True) -> bool:
    """With no coverage list at all, nothing can be excluded (fail open on DISPLAY:
    hiding every report because a config row is missing would blank the site)."""
    return (not coverage_only) or (not states) or state in states


def evidence_class(n_public: int, escalation: bool, repeat_crash_flag: bool = False) -> str:
    """How much evidence stands behind this map location.

    cluster_min_points = 1, so DBSCAN forms a cluster from a SINGLE incident and every
    geocoded record becomes a "hotspot". Measured 1 Aug 2026: 540 of 645 public
    hotspots (84%) held exactly one incident and 69% had no casualties at all. Calling
    a lone pothole report a hotspot on a map government officials act from claims a
    repeat pattern the evidence does not support.

    Rather than raise min_points — which would delete single reports from the map
    entirely, and a verified defect report IS evidence worth publishing — the class
    ships with every feature so the surface can say what it actually has.

    'escalation' is only ever returned when the site setting repeat_crash_flag is on."""
    if escalation and repeat_crash_flag:
        return "escalation"      # >=3 incidents in 6 months (spec headline rule)
    if (n_public or 0) >= 2:
        return "repeat"          # more than one independent report at this location
    return "single"              # one report — real evidence, not a pattern


def hotspot_precision(members: list[dict]) -> str:
    """The location's precision class: the COARSEST class among its reports, or
    'unchecked' when none has been assessed by the name guard yet (pure)."""
    classes = [m.get("precision") for m in members if m.get("precision") in PRECISION_RANK]
    return max(classes, key=PRECISION_RANK.get) if classes else "unchecked"


def hotspot_place(members: list[dict]) -> str | None:
    """The name the pins here were accepted on — the most common one (pure)."""
    names: dict = {}
    for m in members:
        if m.get("pinned_to"):
            names[m["pinned_to"]] = names.get(m["pinned_to"], 0) + 1
    return max(sorted(names), key=names.get) if names else None


def unmapped_reason(inc: dict) -> str:
    """Why a public report has no pin: only a numbered highway matched, or nothing in
    the provider's index matched the place the article names (pure)."""
    return "highway" if inc.get("precision") == "highway" else "unplaced"


def split_mapped(incidents: list[dict], hotspot_ids) -> tuple[list[dict], list[dict]]:
    """(mapped, unmapped). Mapped = has a pin AND belongs to an exported location (pure).
    A report with a stale location link but no pin is unmapped, never listed under a
    place it is not at."""
    ids = set(hotspot_ids)
    mapped, unmapped = [], []
    for inc in incidents:
        (mapped if inc.get("pinned") and inc.get("hotspot_id") in ids else unmapped).append(inc)
    return mapped, unmapped


def tier_of(score, tiers):
    if score is None:
        return "watch"
    for name in ("critical", "high", "medium", "watch"):
        if score >= tiers[name]:
            return name
    return "watch"


def group_by_hotspot(incidents: list[dict], hotspot_ids) -> dict:
    """{hotspot_id: [incident, ...]} for every id in hotspot_ids (pure, unit-tested).
    Members keep the exporter's incident shape; hotspots without members get []."""
    out = {hid: [] for hid in hotspot_ids}
    for inc in incidents:
        out.setdefault(inc["hotspot_id"], []).append(inc)
    return out


def attach_children(incidents: list[dict], defect_rows, source_rows, labels: dict) -> list[dict]:
    """Group bulk-fetched defects and sources onto their incidents (pure, unit-tested).

    defect_rows: (incident_id, defect_type, confidence, evidence_snippet), in output order.
    source_rows: (incident_id, url, outlet, published_date), already ordered by date.
    Every incident gets both keys, empty lists when it has no children."""
    defects: dict = {}
    for iid, t, c, snippet in defect_rows:
        defects.setdefault(iid, []).append(
            {"type": t, "label": labels.get(t, t), "conf": c, "evidence": snippet})
    sources: dict = {}
    for iid, url, outlet, d in source_rows:
        sources.setdefault(iid, []).append(
            {"url": url, "outlet": outlet, "date": str(d) if d else None})
    for inc in incidents:
        inc["defects"] = defects.get(inc["id"], [])
        inc["sources"] = sources.get(inc["id"], [])
    return incidents


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with connect() as conn, conn.cursor() as cur:
        # tiers are quantiles of the public score distribution (see score.py); the
        # optional config key holds the shares, never fixed cut-offs
        cur.execute("select value from config_setting where key='priority_tier_quantiles'")
        row = cur.fetchone()
        quantiles = row[0] if row and isinstance(row[0], dict) and \
            all(0 < float(v) <= 1 for v in row[0].values()) else None
        cur.execute("""select code, label from config_defect_taxonomy""")
        labels = dict(cur.fetchall())
        # what the collector actually covers today — so the map can say it, instead of
        # letting an empty state read as a safe one (launch review, 16 Oct 2026)
        cur.execute("select value from config_setting where key='ingestion'")
        row = cur.fetchone()
        states = (row[0] or {}).get("daily_states", []) if row else []
        cur.execute("select count(*) from config_district where enabled and state = any(%s)", (states,))
        coverage = {"states": states, "districts": int(cur.fetchone()[0])}
        cur.execute("select value from config_setting where key='public_site'")
        row = cur.fetchone()
        site = public_site_settings(row[0] if row else None)
        covered = lambda st: in_coverage(st, states, site["coverage_only"])   # noqa: E731

        # ---- hotspots (public members only) -> GeoJSON
        cur.execute("""
          select h.id, ST_X(h.centroid_geom::geometry), ST_Y(h.centroid_geom::geometry),
                 h.road_name, h.admin_state, h.admin_district, h.admin_city,
                 h.n_public_incidents, h.public_fatalities, h.public_injuries,
                 h.first_crash_date, h.last_crash_date, h.dominant_defects,
                 h.priority_score, h.score_breakdown, h.escalation_candidate, h.status
          from public_hotspot h""")
        rows = [r for r in cur.fetchall() if covered(r[4])]
        tiers = quantile_thresholds([r[13] for r in rows], quantiles)
        feats = []
        for (hid, lon, lat, road, st, dist, city, n, fat, inj, d0, d1,
             dom, score, breakdown, esc, status) in rows:
            raw = (breakdown or {}).get("raw") or {}
            flagged = bool(esc) and site["repeat_crash_flag"]
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                "properties": {
                    "id": hid, "road_name": road, "state": st, "district": dist,
                    "city": city, "incidents": n, "fatalities": fat, "injuries": inj,
                    "first": str(d0) if d0 else None, "last": str(d1) if d1 else None,
                    "defects": dom or [], "score": score,
                    "tier": tier_of(score, tiers),
                    "breakdown": breakdown, "escalation": flagged, "status": status,
                    "evidence": evidence_class(n, esc, site["repeat_crash_flag"]),
                    # what the score rests on vs what the reader can see (16 Oct 2026)
                    "records_public": raw.get("records_public", n),
                    "records_hidden": max(0, int(raw.get("records_total", n) or 0) - int(raw.get("records_public", n) or 0)),
                    "hidden_casualty_crashes_6mo": raw.get("hidden_casualty_crashes_6mo", 0),
                }})

        # ---- public incidents with defects + all sources
        cur.execute("""
          select i.id, i.cluster_id, i.crash_date, i.location_text_best, i.road_name,
                 i.road_type, i.admin_state, i.admin_district, i.fatalities, i.injuries,
                 i.victim_types, i.narrative_summary, i.verification_status,
                 i.extraction_confidence, i.geocode_confidence, i.geocode_method,
                 i.geocode_precision, i.geocode_match, (i.geom is not null)
          from public_incident i""")
        cols = ["id", "hotspot_id", "date", "location", "road_name", "road_type",
                "state", "district", "fatalities", "injuries", "victim_types",
                "summary", "verification", "extraction_conf", "geocode_conf",
                "geocode_method", "precision", "pinned_to", "pinned"]
        incidents, outside = [], {"reports": 0, "deaths": 0}
        for r in cur.fetchall():
            inc = dict(zip(cols, r))
            if not covered(inc["state"]):
                outside["reports"] += 1
                outside["deaths"] += inc["fatalities"] or 0
                continue
            inc["date"] = str(inc["date"]) if inc["date"] else None
            incidents.append(inc)
        # Children in TWO set queries, not two per incident. The per-incident version
        # cost 2 round trips × ~160 ms to the pooler for every public incident: 6,188
        # incidents = 12,376 round trips = the 33-minute export step of 14 Oct 2026,
        # growing with the table. Both still read through public_* views (§8 gate).
        cur.execute("""
          select d.incident_id, d.defect_type, d.defect_confidence, d.evidence_snippet
          from public_incident_defect d
          order by d.incident_id, d.id""")
        defect_rows = cur.fetchall()
        cur.execute("""
          select s.incident_id, a.url, a.outlet_name, a.published_at::date
          from incident_source s
          join source_article a on a.id = s.source_article_id
          join public_incident i on i.id = s.incident_id
          order by s.incident_id, a.published_at, a.id""")   # a.id: stable order for same-day sources
        source_rows = cur.fetchall()
        attach_children(incidents, defect_rows, source_rows, labels)

        # A public report is either ON THE MAP (it has a pin and an exported location)
        # or LISTED WITHOUT A PIN. The second group exists since 16 Nov 2026: a report
        # whose place the provider cannot name is still a published defect report — it
        # is shown by district, never at a guessed point.
        mapped, unmapped = split_mapped(incidents, [f["properties"]["id"] for f in feats])
        for inc in incidents:
            inc["mapped"] = False
        for inc in mapped:
            inc["mapped"] = True
        for inc in unmapped:
            inc["hotspot_id"] = None
            inc["unmapped_reason"] = unmapped_reason(inc)
        (OUT / "incidents.json").write_text(json.dumps(incidents, ensure_ascii=False))

        # Per-hotspot member files (launch review, 16 Oct 2026): the map page used to
        # fetch and parse the whole incidents.json (10.6 MB raw, 2.1 MB compressed) on
        # every visit so the dossier could filter it by hotspot. Now the dossier fetches
        # data/incidents/<hotspot_id>.json on open; the page loads hotspots + meta only.
        # Every public hotspot gets a file (empty list if it somehow has no members) so
        # the frontend never sees a 404. Stale files from merged hotspots are removed.
        inc_dir = OUT / "incidents"
        inc_dir.mkdir(parents=True, exist_ok=True)
        for stale in inc_dir.glob("*.json"):
            stale.unlink()
        by_hotspot = group_by_hotspot(mapped, [f["properties"]["id"] for f in feats])
        for hid, members in by_hotspot.items():
            (inc_dir / f"{hid}.json").write_text(json.dumps(members, ensure_ascii=False))
        for f in feats:
            members = by_hotspot.get(f["properties"]["id"], [])
            # road types per hotspot, so the road-type filter needs no incident file
            f["properties"]["road_types"] = sorted({m["road_type"] for m in members if m.get("road_type")})
            # how precisely this location is known, and the name it is pinned to
            f["properties"]["precision"] = hotspot_precision(members)
            f["properties"]["place"] = hotspot_place(members)
        (OUT / "hotspots.geojson").write_text(json.dumps(
            {"type": "FeatureCollection", "features": feats}, ensure_ascii=False))

        # Reports without a pin: one file per district, and an index the page loads
        # only when the reader opens the list.
        un_dir = OUT / "unmapped"
        un_dir.mkdir(parents=True, exist_ok=True)
        for stale in un_dir.glob("*.json"):
            stale.unlink()
        groups: dict = {}
        for inc in unmapped:
            groups.setdefault((inc.get("state") or "Unknown", inc.get("district") or "Unknown"), []).append(inc)
        index = []
        for n, ((st, dist), items) in enumerate(sorted(groups.items()), 1):
            (un_dir / f"{n}.json").write_text(json.dumps(items, ensure_ascii=False))
            index.append({"state": st, "district": dist, "count": len(items), "file": f"{n}.json",
                          "highway": sum(1 for i in items if i["unmapped_reason"] == "highway")})
        (un_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False))

        meta = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "hotspots": len(feats),
            "by_evidence": {c: sum(1 for f in feats
                                   if f["properties"]["evidence"] == c)
                            for c in ("escalation", "repeat", "single")},
            "by_precision": {c: sum(1 for f in feats if f["properties"]["precision"] == c)
                             for c in (*PRECISION_RANK, "unchecked")},
            "incidents": len(incidents),
            "mapped": len(mapped),
            "unmapped": len(unmapped),
            "unmapped_highway": sum(1 for i in unmapped if i["unmapped_reason"] == "highway"),
            "with_casualties": sum(1 for i in incidents if (i["fatalities"] or 0) > 0 or (i["injuries"] or 0) > 0),
            "fatalities": sum(i["fatalities"] or 0 for i in incidents),
            "injuries": sum(i["injuries"] or 0 for i in incidents),
            "road_types": sorted({i["road_type"] for i in incidents if i.get("road_type")}),
            "coverage": coverage,
            "outside_coverage": outside,
            "site": site,
            "states": sorted({f["properties"]["state"] for f in feats
                              if f["properties"]["state"]}),
            "defect_labels": labels,
            "tiers": tiers,
            "tier_rule": "quantiles of the public score distribution: " + ", ".join(
                f"{k} = top {int(v * 100)}%" for k, v in (quantiles or {"critical": .05, "high": .15, "medium": .40}).items()),
        }
        (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    print(f"exported: {len(feats)} locations · {len(mapped)} mapped + {len(unmapped)} unmapped "
          f"reports in {len(index)} district list(s) · {outside['reports']} outside coverage "
          f"held back -> {OUT}")


if __name__ == "__main__":
    main()
