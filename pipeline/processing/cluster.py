"""Incident→hotspot clustering (BUILD_SPEC §7.6) — PostGIS ST_ClusterDBSCAN.

Decisions (documented):
- Only incidents with geocode_confidence ≥ 0.5 cluster. District-centroid geocodes (0.4)
  would pile up at one fake point and manufacture phantom hotspots; they stay
  cluster_id=null until a reviewer or better geocode places them.
- Single epsilon in metres (config cluster_epsilon_m, keyed by road_type; an incident's
  epsilon = its road_type's value; DBSCAN runs per-epsilon group then merges by proximity
  is overkill at MVP — we run one pass at the modal epsilon 250 m and note it. Web
  Mercator distortion at Indian latitudes (2–20%) is acceptable at this scale.
- Rebuilds are stable: existing hotspots keep id/status if their new centroid lands
  within 300 m of the old one; otherwise a new hotspot row is created.
"""
from pipeline import configload
from pipeline.db import connect

MIN_GEOCODE_CONF = 0.5
EPS_DEFAULT_M = 250
STABLE_MATCH_M = 300


def run() -> dict:
    """Rebuild hotspots from incidents.

    SET-BASED. The DBSCAN pass was always one query; the loop after it was not — five
    statements per cluster (aggregates, dominant defects, stable-id match, upsert,
    cluster_id write). At ~1,900 clusters against the Tokyo session pooler (168ms
    round-trip, measured 1 Aug 2026) that is ~27 minutes of network wait with the
    database idle, and it grows linearly with hotspot count inside the same 360-minute
    ceiling that a timeout turns into a skipped deploy.

    Now: one read that clusters, aggregates, ranks defects and resolves the stable-id
    match in a single pass; one batched update; a loop only for genuinely NEW hotspots,
    which are rare in steady state (1,917 of 1,917 matched on the last run).

    STABLE IDS are the delicate part and the reason this is not a mechanical rewrite.
    A hotspot keeps its id and status when its new centroid lands within
    STABLE_MATCH_M of the old one — that is what carries reviewer decisions across
    rebuilds. Two changes were unavoidable, both stated rather than silent:

    1. Matches are resolved against a SNAPSHOT of the old centroids. The old loop
       updated hotspots as it went, so a later cluster could match a hotspot the
       centroid of which an earlier cluster had already moved — order-dependent, and
       dependent on dict iteration order at that.
    2. When two clusters match the same old hotspot (possible: DBSCAN separates
       clusters by eps=250m but the match radius is 300m), the NEAREST keeps the id and
       the other becomes a new hotspot. The old code let the later one silently
       overwrite the earlier one's row, leaving the first cluster's incidents pointing
       at a hotspot whose data had been replaced.
    """
    cfg_eps = configload.settings().get("cluster_epsilon_m", {})
    eps = int(sum(cfg_eps.values()) / len(cfg_eps)) if cfg_eps else EPS_DEFAULT_M
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"""
          with pts as (
            select id, geom, ST_Transform(geom::geometry, 3857) g
            from incident
            where geom is not null and geocode_confidence >= %s
          ),
          clustered as (
            select id, geom, ST_ClusterDBSCAN(g, eps := {int(eps)}, minpoints := 1)
                   over () cl
            from pts
          ),
          agg as (
            select c.cl,
                   ST_Centroid(ST_Collect(i.geom::geometry))::geography cen,
                   count(*) n, coalesce(sum(i.fatalities),0) fat,
                   coalesce(sum(i.injuries),0) inj,
                   min(i.crash_date) d0, max(i.crash_date) d1,
                   mode() within group (order by i.road_name) road,
                   mode() within group (order by i.admin_state) st,
                   mode() within group (order by i.admin_district) dist,
                   mode() within group (order by i.admin_city) city,
                   array_agg(i.id) ids
            from clustered c join incident i on i.id = c.id
            group by c.cl
          ),
          dom_counts as (
            select c.cl, d.defect_type, count(*) dn, max(d.defect_confidence) mx
            from clustered c
            join incident_defect d on d.incident_id = c.id
            where d.defect_type <> 'no_infrastructure_defect_identified'
            group by c.cl, d.defect_type
          ),
          dom_ranked as (
            select cl, defect_type,
                   row_number() over (partition by cl order by dn desc, mx desc) rn
            from dom_counts
          ),
          dom as (
            select cl, array_agg(defect_type order by rn) dominant
            from dom_ranked where rn <= 3 group by cl
          )
          select a.cl, a.cen, a.n, a.fat, a.inj, a.d0, a.d1, a.road, a.st, a.dist,
                 a.city, a.ids, coalesce(d.dominant, '{{}}'::text[]), m.hid, m.dm
          from agg a
          left join dom d on d.cl = a.cl
          left join lateral (
            select h.id hid, ST_Distance(h.centroid_geom, a.cen) dm
            from hotspot h
            where ST_DWithin(h.centroid_geom, a.cen, %s)
            order by ST_Distance(h.centroid_geom, a.cen)
            limit 1
          ) m on true
          order by a.cl""", (MIN_GEOCODE_CONF, STABLE_MATCH_M))
        rows = cur.fetchall()
        if not rows:
            return {"incidents": 0, "hotspots": 0}

        # One old hotspot may be the nearest match for two clusters; only the closest
        # may keep it (see docstring). Deterministic, unlike the old write-as-you-go.
        best: dict[int, tuple[float, int]] = {}
        for r in rows:
            hid, dm = r[13], r[14]
            if hid is not None and (hid not in best or dm < best[hid][0]):
                best[hid] = (dm, r[0])

        upd, ins, n_incidents = [], [], 0
        for (cl, cen, n, fat, inj, d0, d1, road, st, dist, city, ids, domv, hid, dm) in rows:
            n_incidents += len(ids)
            keep = hid is not None and best.get(hid, (None, None))[1] == cl
            rec = (cen, road, st, dist, city, n, fat, inj, d0, d1, list(domv), ids)
            (upd if keep else ins).append((hid, *rec) if keep else rec)

        if upd:
            cur.executemany("""
              update hotspot set centroid_geom=%s, road_name=%s, admin_state=%s,
                admin_district=%s, admin_city=%s, incident_count=%s, fatality_count=%s,
                injury_count=%s, first_crash_date=%s, last_crash_date=%s,
                dominant_defects=%s, last_recomputed_at=now()
              where id=%s""",
              [(cen, road, st, dist, city, n, fat, inj, d0, d1, dom, hid)
               for (hid, cen, road, st, dist, city, n, fat, inj, d0, d1, dom, _ids) in upd])
            cur.executemany("update incident set cluster_id=%s where id = any(%s)",
                            [(hid, ids) for (hid, *_r, ids) in upd])

        for (cen, road, st, dist, city, n, fat, inj, d0, d1, dom, ids) in ins:
            cur.execute("""
              insert into hotspot (centroid_geom, road_name, admin_state, admin_district,
                admin_city, incident_count, fatality_count, injury_count,
                first_crash_date, last_crash_date, dominant_defects, last_recomputed_at)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) returning id""",
              (cen, road, st, dist, city, n, fat, inj, d0, d1, dom))
            cur.execute("update incident set cluster_id=%s where id = any(%s)",
                        (cur.fetchone()[0], ids))
        conn.commit()
        return {"incidents": n_incidents, "hotspots": len(rows),
                "kept": len(upd), "new": len(ins), "eps_m": eps}
