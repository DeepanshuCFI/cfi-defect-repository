#!/usr/bin/env python3
"""One-time import of no-DB-mode JSONL data (data/*.jsonl) into Postgres.
Used once after Supabase came online; idempotent (skips URLs already present)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import connect          # noqa: E402
from pipeline.settings import ROOT       # noqa: E402
from pipeline.store import ARTICLE_FIELDS, INCIDENT_FIELDS, coerce_incident  # noqa: E402


def main() -> None:
    data = ROOT / "data"
    arts = [json.loads(l) for l in (data / "source_article.jsonl").read_text().splitlines()] \
        if (data / "source_article.jsonl").exists() else []
    incs = [json.loads(l) for l in (data / "incident.jsonl").read_text().splitlines()] \
        if (data / "incident.jsonl").exists() else []
    print(f"importing {len(arts)} articles, {len(incs)} incidents…")

    idmap: dict[int, int] = {}
    with connect() as conn, conn.cursor() as cur:
        for a in arts:
            raw_p = data / "raw_html" / f"{a['id']}.html"
            a["raw_html"] = raw_p.read_text() if raw_p.exists() else None
            cols = ", ".join(ARTICLE_FIELDS)
            ph = ", ".join(["%s"] * len(ARTICLE_FIELDS))
            cur.execute(
                f"insert into source_article ({cols}) values ({ph}) "
                f"on conflict (url) do nothing returning id",
                tuple(a.get(f) for f in ARTICLE_FIELDS))
            row = cur.fetchone()
            if row is None:
                cur.execute("select id from source_article where url=%s", (a["url"],))
                row = cur.fetchone()
            idmap[a["id"]] = row[0]

        n_inc = 0
        for i in incs:
            src_old = (i.get("source_article_ids") or [None])[0]
            src_new = idmap.get(src_old)
            i = coerce_incident(i)
            i["primary_source_id"] = src_new
            cols = ", ".join(INCIDENT_FIELDS)
            ph = ", ".join(["%s"] * len(INCIDENT_FIELDS))
            cur.execute(
                f"insert into incident ({cols}) values ({ph}) returning id",
                tuple(i.get(f) for f in INCIDENT_FIELDS))
            iid = cur.fetchone()[0]
            if i.get("lat") is not None:
                cur.execute(
                    """update incident set geom=ST_GeogFromText(%s),
                       geocode_confidence=%s, geocode_method=%s where id=%s""",
                    (f"POINT({i['lon']} {i['lat']})", i.get("geocode_confidence"),
                     i.get("geocode_method"), iid))
            for d in i.get("defects", []):
                cur.execute(
                    """insert into incident_defect
                       (incident_id, defect_type, defect_confidence, evidence_snippet, evidence_source_id)
                       values (%s,%s,%s,%s,%s)""",
                    (iid, d["defect_type"], d["confidence"], d["evidence_snippet"], src_new))
            if src_new:
                cur.execute(
                    """insert into incident_source (incident_id, source_article_id, match_confidence)
                       values (%s,%s,1.0) on conflict do nothing""", (iid, src_new))
            n_inc += 1
        conn.commit()

        cur.execute("select count(*) from source_article")
        na = cur.fetchone()[0]
        cur.execute("select count(*) from incident")
        ni = cur.fetchone()[0]
        cur.execute("select count(*) from incident_defect")
        nd = cur.fetchone()[0]
        cur.execute("select count(*) from incident where geom is not null")
        ng = cur.fetchone()[0]
    print(f"DB now: {na} articles · {ni} incidents ({ng} geocoded) · {nd} defects")


if __name__ == "__main__":
    main()
