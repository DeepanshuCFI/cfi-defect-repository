#!/usr/bin/env python3
"""Run the auto-reviewer over the live review queue in SHADOW mode: print what it
WOULD do, write nothing. Usage: python -m scripts.auto_review_shadow [--limit N]"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import connect                          # noqa: E402
from pipeline.processing.auto_review import adjudicate   # noqa: E402


def main() -> None:
    limit = 100
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select r.id, r.queue_reason, r.location_text_best, r.fatalities, r.injuries,
                 r.infra_implicated, r.extraction_confidence, r.geocode_confidence,
                 r.narrative_summary, a.clean_text
          from review_queue r
          join source_article a on a.id = r.primary_source_id
          where a.clean_text is not null
          order by r.id limit %s""", (limit,))
        rows = cur.fetchall()
        defect_map: dict[int, list] = {}
        cur.execute("""select incident_id, defect_type, evidence_snippet
                       from incident_defect where incident_id = any(%s)""",
                    ([r[0] for r in rows],))
        for iid, dt, ev in cur.fetchall():
            defect_map.setdefault(iid, []).append(f"{dt}: “{ev[:100]}”")

    print(f"shadow-reviewing {len(rows)} queued incident(s)…\n")
    tally: dict[str, int] = {}
    out = []
    for (iid, reason, loc, f, i, infra, ec, gc, summary, text) in rows:
        ext = (f"location: {loc}\ncasualties: {f} dead / {i} injured\n"
               f"infra_implicated: {infra} (extraction conf {ec}, geocode conf {gc})\n"
               f"summary: {summary}\n"
               f"defects: {'; '.join(defect_map.get(iid, ['(none)']))}")
        try:
            v = adjudicate(text, ext)
        except Exception as e:
            v = {"verdict": "ERROR", "reason": str(e)[:60], "confidence": 0}
        # the location-gate rail: publish verdicts can't act below geocode 0.6
        actionable = v["verdict"]
        if v["verdict"] == "confirm_publish" and (gc or 0) < 0.6:
            actionable = "confirm_publish→HELD (geocode gate)"
        tally[actionable] = tally.get(actionable, 0) + 1
        print(f"#{iid:>3} [{reason[:28]:<28}] -> {actionable:<34} "
              f"conf={v.get('confidence',0):.2f}  {v.get('reason','')[:70]}")
        out.append({"id": iid, "queue_reason": reason, **v, "actionable": actionable})
    print(f"\nVERDICT TALLY: {json.dumps(tally, indent=1)}")
    Path("data").mkdir(exist_ok=True)
    Path("data/auto_review_shadow.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("full verdicts -> data/auto_review_shadow.json (nothing was changed in the DB)")


if __name__ == "__main__":
    main()
