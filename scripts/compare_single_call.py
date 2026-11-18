"""Side-by-side check for one call per article (plan item 1.1's acceptance test).

Takes articles the two-call path has ALREADY judged (their verdicts are in the
database), reads each once more with extract.read_article, and compares. Read-only:
nothing is written to the database. Calls go through pipeline.llm, so they run on
whatever backend the environment selects — use the subscription:

    LLM_BACKEND=cli LLM_CLI_MODEL=claude-opus-5 \
        python -m scripts.compare_single_call --out <dir>

Sample (seeded, so a re-run reads the same articles): model-judged irrelevant
articles, infra-implicated extractions and crash-only extractions. An 'irrelevant'
article counts as model-judged when it passes the free prefilter — the prefilter
retires everything else without a call.

PASS needs all four: scope agreement, infra agreement among articles both paths
extract, casualty agreement, and at least one shared defect type where both paths
implicate the road. Thresholds are arguments; the defaults were fixed before the
first run. Every disagreement is written out in full for a human to read.
"""
import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import llm  # noqa: E402
from pipeline.db import connect  # noqa: E402
from pipeline.processing import extract as ex  # noqa: E402
from pipeline.processing import prefilter  # noqa: E402
from pipeline.store import coerce_incident  # noqa: E402

EXTRACTED_SQL = """
    select a.id, a.clean_text, a.published_at::text, i.id, i.infra_implicated,
           i.fatalities, i.injuries, i.admin_district, i.crash_date::text,
           i.location_text_raw, i.extraction_confidence,
           coalesce((select array_agg(d.defect_type order by d.defect_type)
                     from incident_defect d where d.incident_id = i.id), '{}')
    from incident i join source_article a on a.id = i.primary_source_id
    where i.created_at > now() - make_interval(days => %s)
      and i.infra_implicated = %s and a.clean_text is not null
      and a.processing_status = 'extracted'
    order by i.id desc limit %s"""

# The daily run reads the queue oldest-first and was about three weeks behind when
# this was written, so the articles it judged recently were FETCHED 18-40 days ago;
# newer 'irrelevant' rows are nearly all prefilter retirements, which no model saw.
IRRELEVANT_SQL = """
    select id, clean_text, published_at::text from source_article
    where processing_status = 'irrelevant' and clean_text is not null
      and fetched_at between now() - make_interval(days => %s)
                         and now() - make_interval(days => %s)
    order by id desc limit 2500"""

NO_DEFECT = "no_infrastructure_defect_identified"


def sample(days: int, n_irrelevant: int, n_infra: int, n_crash: int, seed: int,
           irrelevant_window: tuple[int, int] = (18, 40)) -> list[dict]:
    rng = random.Random(seed)
    out = []
    with connect() as conn, conn.cursor() as cur:
        for infra, n in ((True, n_infra), (False, n_crash)):
            cur.execute(EXTRACTED_SQL, (days, infra, n * 8))
            rows = cur.fetchall()
            for r in rng.sample(rows, min(n, len(rows))):
                out.append({"article_id": r[0], "text": r[1], "published_at": r[2],
                            "old": {"in_scope": True, "incident_id": r[3],
                                    "infra_implicated": r[4], "fatalities": r[5],
                                    "injuries": r[6], "admin_district": r[7],
                                    "crash_date": r[8], "location": r[9],
                                    "extraction_confidence": float(r[10] or 0),
                                    "defects": [d for d in r[11] if d != NO_DEFECT]}})
        cur.execute(IRRELEVANT_SQL, (irrelevant_window[1], irrelevant_window[0]))
        judged = [r for r in cur.fetchall() if prefilter.passes(r[1])]
        for r in rng.sample(judged, min(n_irrelevant, len(judged))):
            out.append({"article_id": r[0], "text": r[1], "published_at": r[2],
                        "old": {"in_scope": False}})
    return out


def read_one(item: dict) -> dict:
    title = item["text"].split("\n", 1)[0][:140]
    t0 = time.time()
    try:
        scope, inc, dropped = ex.read_article(title, item["text"],
                                              published_at=item["published_at"])
    except Exception as e:                                  # reported, never hidden
        return {**item, "new": {"error": f"{type(e).__name__}: {str(e)[:200]}"},
                "seconds": round(time.time() - t0, 1)}
    new = {"in_scope": scope["in_scope"], "kind": scope["kind"], "reason": scope["reason"]}
    if inc is not None:
        defects = inc.pop("defects", [])
        inc = coerce_incident(inc)                # the same clean-up a stored record got
        new.update({"infra_implicated": inc["infra_implicated"],
                    "fatalities": inc["fatalities"], "injuries": inc["injuries"],
                    "admin_district": inc.get("admin_district"),
                    "crash_date": inc.get("crash_date"),
                    "location": inc.get("location_text_best"),
                    "extraction_confidence": inc["extraction_confidence"],
                    "defects": sorted(d["defect_type"] for d in defects
                                      if d["defect_type"] != NO_DEFECT),
                    "snippets_dropped": dropped})
    return {**item, "new": new, "seconds": round(time.time() - t0, 1)}


def _same_district(a, b) -> bool:
    norm = lambda s: "".join(ch for ch in (s or "").lower() if ch.isalnum())  # noqa: E731
    return norm(a) == norm(b)


def compare(results: list[dict]) -> dict:
    """Pure: turn the paired verdicts into rates and a list of disagreements."""
    errors = [r for r in results if "error" in r["new"]]
    ok = [r for r in results if "error" not in r["new"]]
    scope_agree = [r for r in ok if r["old"]["in_scope"] == r["new"]["in_scope"]]
    both_in = [r for r in ok if r["old"]["in_scope"] and r["new"]["in_scope"]]
    infra_agree = [r for r in both_in
                   if r["old"]["infra_implicated"] == r["new"]["infra_implicated"]]
    cas_agree = [r for r in both_in
                 if (r["old"]["fatalities"], r["old"]["injuries"])
                 == (r["new"]["fatalities"], r["new"]["injuries"])]
    both_infra = [r for r in both_in
                  if r["old"]["infra_implicated"] and r["new"]["infra_implicated"]]
    defect_share = [r for r in both_infra
                    if set(r["old"]["defects"]) & set(r["new"]["defects"])]
    district_agree = [r for r in both_in
                      if _same_district(r["old"]["admin_district"], r["new"]["admin_district"])]

    def rate(part, whole):
        return None if not whole else round(len(part) / len(whole), 3)

    disagreements = []
    for r in ok:
        why = []
        if r["old"]["in_scope"] != r["new"]["in_scope"]:
            why.append("scope")
        elif r["old"]["in_scope"]:
            if r["old"]["infra_implicated"] != r["new"]["infra_implicated"]:
                why.append("infra")
            if (r["old"]["fatalities"], r["old"]["injuries"]) != (
                    r["new"]["fatalities"], r["new"]["injuries"]):
                why.append("casualties")
            if (r["old"]["infra_implicated"] and r["new"]["infra_implicated"]
                    and not set(r["old"]["defects"]) & set(r["new"]["defects"])):
                why.append("defects")
        if why:
            disagreements.append({"article_id": r["article_id"], "why": why,
                                  "old": r["old"], "new": r["new"],
                                  "text_head": r["text"][:600]})
    return {
        "n": len(results), "errors": len(errors),
        "scope_agreement": rate(scope_agree, ok), "n_scope": len(ok),
        "infra_agreement": rate(infra_agree, both_in), "n_both_in_scope": len(both_in),
        "casualty_agreement": rate(cas_agree, both_in),
        "district_agreement": rate(district_agree, both_in),
        "defect_overlap": rate(defect_share, both_infra), "n_both_infra": len(both_infra),
        "old_in_new_out": sum(1 for r in ok if r["old"]["in_scope"] and not r["new"]["in_scope"]),
        "old_out_new_in": sum(1 for r in ok if not r["old"]["in_scope"] and r["new"]["in_scope"]),
        "disagreements": disagreements,
        "error_detail": [{"article_id": r["article_id"], **r["new"]} for r in errors],
    }


def verdict(summary: dict, th: dict) -> tuple[bool, list[str]]:
    """Pure: PASS only when every rate clears its bar and no call errored out."""
    fails = []
    for key, bar in th.items():
        got = summary.get(key)
        if got is None or got < bar:
            fails.append(f"{key} {got} < {bar}")
    if summary["errors"] > th.get("max_errors", 1):
        fails.append(f"{summary['errors']} call(s) failed")
    return not fails, fails


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="directory for report.json")
    ap.add_argument("--days", type=int, default=8, help="how far back to sample extractions")
    ap.add_argument("--irrelevant", type=int, default=15)
    ap.add_argument("--infra", type=int, default=25)
    ap.add_argument("--crash", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1811)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-scope", type=float, default=0.90)
    ap.add_argument("--min-infra", type=float, default=0.90)
    ap.add_argument("--min-casualty", type=float, default=0.90)
    ap.add_argument("--min-defect-overlap", type=float, default=0.85)
    args = ap.parse_args()

    if llm.backend() != "cli":
        sys.exit("refusing to run: set LLM_BACKEND=cli — this check must not spend API credit")
    items = sample(args.days, args.irrelevant, args.infra, args.crash, args.seed)
    print(f"sampled {len(items)} article(s): "
          f"{sum(1 for i in items if not i['old']['in_scope'])} judged irrelevant, "
          f"{sum(1 for i in items if i['old'].get('infra_implicated'))} infra-implicated, "
          f"{sum(1 for i in items if i['old'].get('infra_implicated') is False)} crash-only "
          f"· model {llm.model_for('extraction')}")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = []
        for n, r in enumerate(pool.map(read_one, items), 1):
            results.append(r)
            new = r["new"]
            print(f"  {n:>2}/{len(items)} art #{r['article_id']} "
                  f"old={'in' if r['old']['in_scope'] else 'out'} "
                  f"new={'ERR' if 'error' in new else ('in' if new['in_scope'] else 'out')} "
                  f"({r['seconds']}s)", flush=True)
    summary = compare(results)
    th = {"scope_agreement": args.min_scope, "infra_agreement": args.min_infra,
          "casualty_agreement": args.min_casualty, "defect_overlap": args.min_defect_overlap}
    passed, fails = verdict(summary, th)
    usage = llm.usage_snapshot() if hasattr(llm, "usage_snapshot") else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(
        {"passed": passed, "fails": fails, "thresholds": th, "usage": usage,
         "calls": len(items), "summary": summary,
         "rows": [{k: v for k, v in r.items() if k != "text"} for r in results]},
        ensure_ascii=False, indent=1, default=str))
    print("\n" + json.dumps({k: v for k, v in summary.items()
                             if k not in ("disagreements", "error_detail")}, indent=1))
    if usage:
        print(f"usage: {usage}")
    print(f"\n{'PASS' if passed else 'FAIL'}"
          + ("" if passed else " — " + "; ".join(fails))
          + f" · {len(summary['disagreements'])} disagreement(s) written to {out / 'report.json'}")


if __name__ == "__main__":
    main()
