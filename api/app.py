"""Public read-only API (Phase 9, BUILD_SPEC §10) + the corrections intake.

Run:  python3 -m uvicorn api.app:app --port 8610
Docs: /docs (OpenAPI, auto-generated). Reads ONLY public_* views — the confidence
gate is enforced by the data layer, not this code. One write endpoint: corrections.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from pipeline.db import connect

app = FastAPI(
    title="CFI Road Infrastructure Defect Repository — Public API",
    description=("Read-only access to the public registry of news-evidenced road "
                 "infrastructure defects and crash hotspots in India. Every entry "
                 "traces to its sources. Defects are as reported in news media; "
                 "locations are indicative pending physical audit."),
    version="0.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
                   allow_headers=["*"])

_conn = None


def db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = connect()
        _conn.autocommit = True
    return _conn


def rows(sql, params=()):
    with db().cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.get("/api/meta")
def meta():
    m = rows("""select
      (select count(*) from public_hotspot) hotspots,
      (select count(*) from public_incident) incidents,
      (select coalesce(sum(fatalities),0) from public_incident) fatalities,
      (select coalesce(sum(injuries),0) from public_incident) injuries""")[0]
    m["states"] = [r["admin_state"] for r in rows(
        "select distinct admin_state from public_hotspot where admin_state is not null order by 1")]
    m["defect_taxonomy"] = {r["code"]: r["label"] for r in rows(
        "select code, label from config_defect_taxonomy where maps_to_defects and enabled")}
    m["disclaimer"] = ("Defects are as reported in news media; locations are indicative "
                       "pending physical audit. Absence of data is absence of coverage.")
    return m


@app.get("/api/hotspots")
def hotspots(state: str | None = None, district: str | None = None,
             tier_min_score: float = Query(0, ge=0, le=100),
             repeat_only: bool = False, defect: str | None = None,
             limit: int = Query(100, le=500), offset: int = 0):
    sql = """select id, ST_Y(centroid_geom::geometry) lat, ST_X(centroid_geom::geometry) lon,
             road_name, admin_state, admin_district, admin_city,
             n_public_incidents incidents, public_fatalities fatalities,
             public_injuries injuries, first_crash_date, last_crash_date,
             dominant_defects, priority_score, score_breakdown, escalation_candidate, status
             from public_hotspot where coalesce(priority_score,0) >= %s"""
    params: list = [tier_min_score]
    if state:
        sql += " and admin_state = %s"; params.append(state)
    if district:
        sql += " and admin_district = %s"; params.append(district)
    if repeat_only:
        sql += " and n_public_incidents >= 2"
    if defect:
        sql += " and %s = any(dominant_defects)"; params.append(defect)
    sql += " order by priority_score desc nulls last limit %s offset %s"
    params += [limit, offset]
    return {"hotspots": rows(sql, tuple(params))}


@app.get("/api/hotspots/{hid}")
def hotspot(hid: int):
    hs = rows("""select id, ST_Y(centroid_geom::geometry) lat, ST_X(centroid_geom::geometry) lon,
                 road_name, admin_state, admin_district, admin_city,
                 n_public_incidents incidents, public_fatalities fatalities,
                 public_injuries injuries, first_crash_date, last_crash_date,
                 dominant_defects, priority_score, score_breakdown, escalation_candidate,
                 status from public_hotspot where id=%s""", (hid,))
    if not hs:
        raise HTTPException(404, "hotspot not found or not public")
    h = hs[0]
    h["incidents_detail"] = _incidents_full("i.cluster_id=%s", (hid,))
    return h


@app.get("/api/incidents")
def incidents(state: str | None = None, district: str | None = None,
              date_from: str | None = None, date_to: str | None = None,
              limit: int = Query(100, le=500), offset: int = 0):
    where, params = "true", []
    if state:
        where += " and i.admin_state=%s"; params.append(state)
    if district:
        where += " and i.admin_district=%s"; params.append(district)
    if date_from:
        where += " and i.crash_date >= %s"; params.append(date_from)
    if date_to:
        where += " and i.crash_date <= %s"; params.append(date_to)
    where += " order by i.crash_date desc nulls last limit %s offset %s"
    params += [limit, offset]
    return {"incidents": _incidents_full(where, tuple(params))}


@app.get("/api/incidents/{iid}")
def incident(iid: int):
    out = _incidents_full("i.id=%s", (iid,))
    if not out:
        raise HTTPException(404, "incident not found or not public")
    return out[0]


def _incidents_full(where: str, params: tuple) -> list[dict]:
    incs = rows(f"""
      select i.id, i.cluster_id hotspot_id, i.crash_date, i.location_text_best,
             i.road_name, i.road_type, i.admin_state, i.admin_district,
             i.fatalities, i.injuries, i.victim_types, i.narrative_summary,
             i.verification_status, i.geocode_confidence, i.geocode_method
      from public_incident i where {where}""", params)
    for inc in incs:
        inc["defects"] = rows("""
          select d.defect_type, t.label, d.defect_confidence, d.evidence_snippet
          from public_incident_defect d
          join config_defect_taxonomy t on t.code=d.defect_type
          where d.incident_id=%s""", (inc["id"],))
        inc["sources"] = rows("""
          select a.url, a.outlet_name, a.published_at::date published
          from incident_source s join source_article a on a.id=s.source_article_id
          where s.incident_id=%s order by a.published_at""", (inc["id"],))
    return incs


class Correction(BaseModel):
    entity_type: str = Field(pattern="^(incident|hotspot)$")
    entity_id: int
    message: str = Field(min_length=10, max_length=4000)
    contact: str | None = Field(default=None, max_length=200)


@app.post("/api/corrections", status_code=201)
def submit_correction(c: Correction):
    """Files a public correction. The entry gains a visible 'disputed' badge while
    reviewed; it is not removed (prevents censorship-by-correction)."""
    exists = rows(f"select 1 from public_{c.entity_type} where id=%s", (c.entity_id,))
    if not exists:
        raise HTTPException(404, f"{c.entity_type} not found or not public")
    with db().cursor() as cur:
        cur.execute("""insert into correction (entity_type, entity_id, message, contact)
                       values (%s,%s,%s,%s) returning id""",
                    (c.entity_type, c.entity_id, c.message, c.contact))
        cid = cur.fetchone()[0]
        if c.entity_type == "incident":
            cur.execute("""update incident set verification_status='disputed'
                           where id=%s and verification_status <> 'rejected'""",
                        (c.entity_id,))
        cur.execute("""insert into review_action (entity_type, entity_id, reviewer,
                       action, note) values (%s,%s,'public:correction','edit',%s)""",
                    (c.entity_type, c.entity_id, f"correction #{cid} filed: {c.message[:120]}"))
    return {"correction_id": cid, "status": "open",
            "note": "Entry marked disputed pending review. Thank you."}
