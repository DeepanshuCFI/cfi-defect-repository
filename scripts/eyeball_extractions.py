#!/usr/bin/env python3
"""Manual QA (BUILD_SPEC §11.3): print extractions side by side with their article text
so a human can eyeball accuracy. Sample-check ≥50 before scaling ingestion (CLAUDE.md).

Usage:
  python -m scripts.eyeball_extractions [--n 50] [--infra-only] [--jsonl]
"""
import argparse
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.settings import ROOT  # noqa: E402


def load_jsonl():
    inc_p = ROOT / "data" / "incident.jsonl"
    art_p = ROOT / "data" / "source_article.jsonl"
    incidents = [json.loads(l) for l in inc_p.read_text().splitlines()] if inc_p.exists() else []
    arts = {r["id"]: r for r in
            (json.loads(l) for l in art_p.read_text().splitlines())} if art_p.exists() else {}
    for i in incidents:
        i["_article"] = arts.get((i.get("source_article_ids") or [None])[0], {})
        i["_defects"] = i.get("defects", [])
    return incidents


def load_db():
    from pipeline.db import connect
    out = []
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select i.id, i.crash_date, i.location_text_best, i.road_name, i.road_type,
                 i.admin_state, i.admin_district, i.fatalities, i.injuries,
                 i.narrative_summary, i.infra_implicated, i.extraction_confidence,
                 a.id, a.url, a.clean_text
          from incident i join source_article a on a.id = i.primary_source_id
          order by i.id""")
        rows = cur.fetchall()
        for r in rows:
            inc = {"id": r[0], "crash_date": str(r[1] or ""), "location_text_best": r[2],
                   "road_name": r[3], "road_type": r[4], "admin_state": r[5],
                   "admin_district": r[6], "fatalities": r[7], "injuries": r[8],
                   "narrative_summary": r[9], "infra_implicated": r[10],
                   "extraction_confidence": r[11],
                   "_article": {"id": r[12], "url": r[13], "clean_text": r[14]}}
            cur.execute("""select defect_type, defect_confidence, evidence_snippet
                           from incident_defect where incident_id=%s""", (inc["id"],))
            inc["_defects"] = [{"defect_type": d, "confidence": c, "evidence_snippet": s}
                               for d, c, s in cur.fetchall()]
            out.append(inc)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--infra-only", action="store_true")
    ap.add_argument("--jsonl", action="store_true")
    args = ap.parse_args()

    from pipeline.settings import DATABASE_URL
    use_db = not args.jsonl and DATABASE_URL and "REPLACE_ME" not in DATABASE_URL
    incidents = load_db() if use_db else load_jsonl()
    if args.infra_only:
        incidents = [i for i in incidents if i.get("infra_implicated")]
    incidents = incidents[-args.n:]

    W = 100
    for i in incidents:
        a = i["_article"]
        print("=" * W)
        print(f"INCIDENT #{i['id']}  conf={i.get('extraction_confidence')}  "
              f"F{i.get('fatalities')}/I{i.get('injuries')}  infra={i.get('infra_implicated')}")
        print(f"  date: {i.get('crash_date')}   road: {i.get('road_name')} [{i.get('road_type')}]")
        print(f"  loc : {i.get('location_text_best')}")
        print(f"  admin: {i.get('admin_district')}, {i.get('admin_state')}")
        print(f"  summary: {textwrap.fill(str(i.get('narrative_summary')), W-4)}")
        for d in i["_defects"]:
            print(f"  DEFECT {d['defect_type']} (conf {d['confidence']})")
            print(textwrap.indent(textwrap.fill('“' + d['evidence_snippet'] + '”', W - 8), " " * 8))
        print(f"  source: {a.get('url','?')}")
        body = (a.get("clean_text") or "")[:600]
        print(textwrap.indent(textwrap.fill("ARTICLE: " + body, W - 4), "    "))
        print()
    print(f"{len(incidents)} extraction(s) shown. Judge: location right? casualties right? "
          f"defects supported by the quoted evidence? summary faithful?")


if __name__ == "__main__":
    main()
