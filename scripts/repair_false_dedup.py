#!/usr/bin/env python3
"""One-off repair: requeue articles wrongly dropped as near-duplicates by the pre-fix
ingest dedup (global Hamming<=6, no locality scope — see pipeline/store.py history).

An article dropped as 'near_duplicate' is a FALSE positive if, under the corrected rule,
no KEPT article (status != near_duplicate) in the SAME district sits within Hamming<=3 of
it. Those are reset to 'fetched' so the normal process/extract step picks them up.

Usage:  python3 scripts/repair_false_dedup.py [--since YYYY-MM-DD] [--apply]
Default is a dry run (prints counts, changes nothing). --apply performs the reset.
"""
import argparse
import os
import sys
from collections import Counter

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.settings import DATABASE_URL  # noqa: E402

# Set-based: a near_duplicate is a FALSE positive if no KEPT article in the SAME district
# is within Hamming<=3. bit_count(a # b) on bit(64) is Postgres 14+ (Supabase = 17).
FALSE_POS_CTE = """
with false_pos as (
  select nd.id, nd.district
  from source_article nd
  where nd.created_at >= %s and nd.processing_status = 'near_duplicate'
    and nd.dedup_hash is not null and nd.dedup_hash <> ''
    and not exists (
      select 1 from source_article kp
      where kp.processing_status <> 'near_duplicate'
        and kp.dedup_hash is not null and kp.dedup_hash <> ''
        and kp.district = nd.district and kp.id <> nd.id
        and kp.fetched_at > now() - make_interval(days => 8)
        and bit_count(('x'||nd.dedup_hash)::bit(64) # ('x'||kp.dedup_hash)::bit(64)) <= 3
    )
    {defect_clause}
)
"""

# Leading-indicator infra-defect vocabulary (Hindi + English). High precision — targets
# the defect class this repository exists for, not generic behaviour-only crashes.
DEFECT_TERMS = ["गड्ढ", "जर्जर", "धंस", "स्ट्रीट लाइट", "लाइट खराब", "अंधेर", "जलभराव",
                "जल भराव", "पानी भर", "डिवाइडर", "गार्ड रेल", "क्रैश बैरियर", "रेलिंग",
                "अंधा मोड़", "पुलिया", "स्पीड ब्रेकर", "फुटपाथ", "संकेतक",
                "pothole", "dilapidated", "waterlog", "street light", "streetlight",
                "blind curve", "crash barrier", "guard rail", "footpath", "signage"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-09")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--estimate", action="store_true",
                    help="run the free prefilter over the set to estimate paid-extraction load")
    ap.add_argument("--defect-only", action="store_true",
                    help="restrict to leading-indicator infra-defect stories (bounded recovery)")
    args = ap.parse_args()

    if args.defect_only:
        ilike = " or ".join(["nd.clean_text ilike %s"] * len(DEFECT_TERMS))
        cte = FALSE_POS_CTE.format(defect_clause=f"and ({ilike})")
        params = tuple([args.since] + [f"%{t}%" for t in DEFECT_TERMS])
    else:
        cte = FALSE_POS_CTE.format(defect_clause="")
        params = (args.since,)

    with psycopg.connect(DATABASE_URL, connect_timeout=15,
                         options="-c statement_timeout=120000") as conn:
        rows = conn.execute(cte + "select id, district from false_pos", params).fetchall()
        print(f"false positives to requeue since {args.since}: {len(rows)}")
        print(f"  includes id=7943 (Marauri): {any(i == 7943 for i, _ in rows)}")
        print(f"  top districts: {Counter(d for _, d in rows).most_common(8)}")

        if args.estimate:
            from pipeline.processing import prefilter
            texts = conn.execute(
                cte + "select clean_text from false_pos fp join source_article a on a.id=fp.id",
                params).fetchall()
            passed = sum(1 for (t,) in texts if t and prefilter.passes(t))
            print(f"  free-prefilter PASS (reach paid extraction): {passed} / {len(texts)}")

        if args.apply and rows:
            conn.execute(
                cte + "update source_article set processing_status='fetched' "
                "where id in (select id from false_pos)", params)
            conn.commit()
            print(f"APPLIED: {len(rows)} articles reset to 'fetched'.")
        elif not args.apply:
            print("DRY RUN — rerun with --apply to reset these to 'fetched'.")


if __name__ == "__main__":
    main()
