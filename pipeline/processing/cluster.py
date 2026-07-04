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
    cfg_eps = configload.settings().get("cluster_epsilon_m", {})
    eps = int(sum(cfg_eps.values()) / len(cfg_eps)) if cfg_eps else EPS_DEFAULT_M
    with connect() as conn, conn.cursor() as cur:
        # 1. cluster ids per incident (single pass, Web-Mercator metres)
        cur.execute(f"""
          with pts as (
            select id, ST_Transform(geom::geometry, 3857) g
            from incident
            where geom is not null and geocode_confidence >= %s
          )
          select id, ST_ClusterDBSCAN(g, eps := {int(eps)}, minpoints := 1) over () cl
          from pts""", (MIN_GEOCODE_CONF,))
        rows = cur.fetchall()
        if not rows:
            return {"incidents": 0, "hotspots": 0}
        by_cluster: dict[int, list[int]] = {}
        for iid, cl in rows:
            by_cluster.setdefault(cl, []).append(iid)

        # 2. snapshot old hotspots for stable matching
        cur.execute("select id, status, centroid_geom from hotspot")
        old = cur.fetchall()

        n_kept = n_new = 0
        for cl, ids in by_cluster.items():
            cur.execute("""
              select ST_Centroid(ST_Collect(geom::geometry))::geography,
                     count(*), coalesce(sum(fatalities),0), coalesce(sum(injuries),0),
                     min(crash_date), max(crash_date),
                     mode() within group (order by road_name),
                     mode() within group (order by admin_state),
                     mode() within group (order by admin_district),
                     mode() within group (order by admin_city)
              from incident where id = any(%s)""", (ids,))
            (cen, n, fat, inj, d0, d1, road, st, dist, city) = cur.fetchone()
            cur.execute("""
              select defect_type from incident_defect
              where incident_id = any(%s)
                and defect_type <> 'no_infrastructure_defect_identified'
              group by defect_type order by count(*) desc, max(defect_confidence) desc
              limit 3""", (ids,))
            dom = [r[0] for r in cur.fetchall()]

            # stable id: nearest old hotspot within STABLE_MATCH_M
            cur.execute("""
              select id from hotspot
              where ST_DWithin(centroid_geom, %s, %s)
              order by ST_Distance(centroid_geom, %s) limit 1""",
                (cen, STABLE_MATCH_M, cen))
            hit = cur.fetchone()
            if hit:
                hid = hit[0]
                cur.execute("""
                  update hotspot set centroid_geom=%s, road_name=%s, admin_state=%s,
                    admin_district=%s, admin_city=%s, incident_count=%s, fatality_count=%s,
                    injury_count=%s, first_crash_date=%s, last_crash_date=%s,
                    dominant_defects=%s, last_recomputed_at=now()
                  where id=%s""",
                    (cen, road, st, dist, city, n, fat, inj, d0, d1, dom, hid))
                n_kept += 1
            else:
                cur.execute("""
                  insert into hotspot (centroid_geom, road_name, admin_state, admin_district,
                    admin_city, incident_count, fatality_count, injury_count,
                    first_crash_date, last_crash_date, dominant_defects, last_recomputed_at)
                  values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) returning id""",
                    (cen, road, st, dist, city, n, fat, inj, d0, d1, dom))
                hid = cur.fetchone()[0]
                n_new += 1
            cur.execute("update incident set cluster_id=%s where id = any(%s)", (hid, ids))
        conn.commit()
        return {"incidents": len(rows), "hotspots": len(by_cluster),
                "kept": n_kept, "new": n_new, "eps_m": eps}
