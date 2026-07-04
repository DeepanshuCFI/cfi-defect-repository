"""Internal review-queue UI (BUILD_SPEC §8 / Phase 7).

Run:  python3 -m uvicorn review.app:app --port 8600 --reload
Internal-only tool (bind localhost). Every action audit-logs to review_action.
Actions: approve · edit · reject · merge (into another incident) · split (clone to
separate a second crash the extractor collapsed).
"""
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from pipeline.db import connect

app = FastAPI(title="CFI Defect Repository — Review Queue")

CSS = """
body{font-family:-apple-system,'Segoe UI',Arial,sans-serif;margin:0;background:#F8F7FF;color:#1a1c1c}
.top{background:#4A35FF;color:#fff;padding:14px 28px;font-weight:700}
.top small{opacity:.75;font-weight:400;margin-left:10px}
.wrap{max-width:1100px;margin:24px auto;padding:0 20px}
.card{background:#fff;border:1px solid #EDEEF2;border-radius:12px;padding:18px 22px;margin-bottom:16px}
.reason{display:inline-block;background:rgba(245,124,0,.1);color:#a85500;border:1px solid rgba(245,124,0,.35);
 border-radius:999px;padding:2px 12px;font-size:12px;font-weight:600}
.meta{color:#777589;font-size:13px;margin-top:6px}
.snippet{background:#F3F1FF;border-radius:8px;padding:10px 14px;margin:8px 0;font-size:13px}
.snippet b{color:#4A35FF}
.row{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
button,.btn{border:1px solid #EDEEF2;border-radius:999px;padding:8px 18px;font-size:13px;cursor:pointer;background:#fff}
button.ok{background:#00AA44;border-color:#00AA44;color:#fff}
button.no{background:#F10015;border-color:#F10015;color:#fff}
input,select{border:1px solid #EDEEF2;border-radius:8px;padding:7px 10px;font-size:13px;margin:2px 0}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
a{color:#4A35FF}
.count{color:#777589;font-size:14px;margin-bottom:14px}
"""


_conn = None


def get_conn():
    """One shared autocommit connection (per-query connects to Supabase cost ~1s TLS)."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = connect()
        _conn.autocommit = True
    return _conn


def q(sql: str, params=()) -> list[dict]:
    with get_conn().cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def x(sql: str, params=()) -> None:
    with get_conn().cursor() as cur:
        cur.execute(sql, params)


def log_action(entity_id: int, action: str, before=None, after=None, note: str = "") -> None:
    x("""insert into review_action (entity_type, entity_id, reviewer, action,
         before_json, after_json, note) values ('incident',%s,'reviewer:ui',%s,%s,%s,%s)""",
      (entity_id, action, json.dumps(before, default=str) if before else None,
       json.dumps(after, default=str) if after else None, note))


def snapshot(iid: int) -> dict | None:
    rows = q("select row_to_json(i) rj from incident i where id=%s", (iid,))
    return rows[0]["rj"] if rows else None


@app.get("/", response_class=HTMLResponse)
def queue():
    rows = q("""
      select r.id, r.queue_reason, r.crash_date, r.location_text_best, r.road_name,
             r.road_type, r.admin_district, r.admin_state, r.fatalities, r.injuries,
             r.narrative_summary, r.extraction_confidence, r.geocode_confidence,
             r.infra_implicated, a.url, a.outlet_name
      from review_queue r
      left join source_article a on a.id = r.primary_source_id
      order by r.fatalities desc, r.id""")
    n_pub = q("select count(*) c from public_incident")[0]["c"]
    cards = []
    for r in rows:
        defects = q("""select defect_type, defect_confidence, evidence_snippet
                       from incident_defect where incident_id=%s""", (r["id"],))
        dhtml = "".join(
            f"<div class='snippet'><b>{html.escape(d['defect_type'])}</b> "
            f"(conf {d['defect_confidence']})<br>“{html.escape(d['evidence_snippet'][:220])}”</div>"
            for d in defects) or "<div class='meta'>no defects tagged</div>"
        cards.append(f"""
<div class="card">
  <span class="reason">{r['queue_reason']}</span>
  <h3 style="margin:8px 0 0">#{r['id']} · {html.escape(r['location_text_best'] or '?')}</h3>
  <div class="meta">{r['crash_date'] or 'undated'} · {html.escape(r['road_name'] or '?')}
    [{r['road_type']}] · {html.escape(r['admin_district'] or '?')}, {html.escape(r['admin_state'] or '?')}
    · F{r['fatalities']}/I{r['injuries']} · extr {r['extraction_confidence']} · geo {r['geocode_confidence']}
    · infra {r['infra_implicated']}</div>
  <p style="font-size:14px">{html.escape(r['narrative_summary'] or '')}</p>
  {dhtml}
  <div class="meta"><a href="{html.escape(r['url'] or '#')}" target="_blank">source: {html.escape(r['outlet_name'] or r['url'] or '?')}</a></div>
  <div class="row">
    <form method="post" action="/incident/{r['id']}/approve"><button class="ok">Approve → publish</button></form>
    <form method="post" action="/incident/{r['id']}/reject"><button class="no">Reject</button></form>
    <a class="btn" href="/incident/{r['id']}/edit">Edit</a>
    <form method="post" action="/incident/{r['id']}/merge" style="display:flex;gap:6px">
      <input name="into" placeholder="merge into #id" size="10"><button>Merge</button></form>
    <form method="post" action="/incident/{r['id']}/split"><button>Split (clone)</button></form>
  </div>
</div>""")
    return f"""<style>{CSS}</style>
<div class="top">Crashfree India · Review Queue<small>{len(rows)} awaiting review · {n_pub} public</small></div>
<div class="wrap"><div class="count">Approve overrides the confidence gate. Every action is audit-logged.</div>
{''.join(cards) or '<div class="card">Queue is empty 🎉</div>'}</div>"""


@app.post("/incident/{iid}/approve")
def approve(iid: int):
    before = snapshot(iid)
    if not before:
        raise HTTPException(404)
    x("update incident set verification_status='reviewed' where id=%s", (iid,))
    log_action(iid, "approve", before, note="reviewer approved -> publish")
    return RedirectResponse("/", status_code=303)


@app.post("/incident/{iid}/reject")
def reject(iid: int):
    before = snapshot(iid)
    if not before:
        raise HTTPException(404)
    x("update incident set verification_status='rejected' where id=%s", (iid,))
    log_action(iid, "reject", before, note="reviewer rejected")
    return RedirectResponse("/", status_code=303)


EDITABLE = ["crash_date", "location_text_best", "road_name", "road_type", "admin_state",
            "admin_district", "admin_city", "fatalities", "injuries", "narrative_summary"]


@app.get("/incident/{iid}/edit", response_class=HTMLResponse)
def edit_form(iid: int):
    rows = q(f"select {', '.join(EDITABLE)} from incident where id=%s", (iid,))
    if not rows:
        raise HTTPException(404)
    r = rows[0]
    fields = "".join(
        f"<label style='font-size:12px;color:#777589'>{f}<br>"
        f"<input name='{f}' value=\"{html.escape('' if r[f] is None else str(r[f]))}\" style='width:95%'></label>"
        for f in EDITABLE)
    return f"""<style>{CSS}</style><div class="top">Edit incident #{iid}</div>
<div class="wrap"><div class="card"><form method="post" action="/incident/{iid}/edit">
<div class="grid">{fields}</div>
<div class="row"><button class="ok">Save (marks reviewed)</button><a class="btn" href="/">Cancel</a></div>
</form></div></div>"""


from fastapi import Request  # noqa: E402


@app.post("/incident/{iid}/edit")
async def edit_save_form(iid: int, request: Request):
    form = dict(await request.form())
    before = snapshot(iid)
    if not before:
        raise HTTPException(404)
    sets, vals = [], []
    for f in EDITABLE:
        v = form.get(f, "")
        v = None if v == "" else v
        sets.append(f"{f}=%s")
        vals.append(v)
    vals.append(iid)
    x(f"update incident set {', '.join(sets)}, verification_status='reviewed', "
      f"updated_at=now() where id=%s", tuple(vals))
    log_action(iid, "edit", before, snapshot(iid), note="reviewer edit -> reviewed")
    return RedirectResponse("/", status_code=303)


@app.post("/incident/{iid}/merge")
async def merge(iid: int, into: str = Form(...)):
    target = int(into.strip().lstrip("#"))
    if not snapshot(iid) or not snapshot(target):
        raise HTTPException(404)
    from pipeline.processing.dedup import merge_incident
    with connect() as conn:
        merge_incident(conn, keep_id=target, merge_id=iid)
    log_action(target, "merge", note=f"reviewer merged #{iid} into #{target}")
    return RedirectResponse("/", status_code=303)


@app.post("/incident/{iid}/split")
def split(iid: int):
    before = snapshot(iid)
    if not before:
        raise HTTPException(404)
    rows = q("""insert into incident (crash_date, crash_time, location_text_raw,
        location_text_best, road_name, road_type, admin_state, admin_district, admin_city,
        admin_ward, fatalities, injuries, vehicles_involved, victim_types,
        narrative_summary, infra_implicated, extraction_confidence, primary_source_id)
      select crash_date, crash_time, location_text_raw, location_text_best, road_name,
        road_type, admin_state, admin_district, admin_city, admin_ward, 0, 0,
        vehicles_involved, victim_types, narrative_summary || ' [SPLIT — edit me]',
        infra_implicated, extraction_confidence, primary_source_id
      from incident where id=%s returning id""", (iid,))
    new_id = rows[0]["id"]
    x("""insert into incident_source (incident_id, source_article_id, match_confidence)
         select %s, source_article_id, match_confidence from incident_source
         where incident_id=%s on conflict do nothing""", (new_id, iid))
    log_action(iid, "split", before, note=f"cloned to #{new_id}; edit both")
    return RedirectResponse(f"/incident/{new_id}/edit", status_code=303)
