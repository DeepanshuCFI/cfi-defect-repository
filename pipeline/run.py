#!/usr/bin/env python3
"""Pipeline CLI (Phase 2: collect).

Examples:
  python -m pipeline.run collect --district Supaul
  python -m pipeline.run collect --state Bihar --days 3 --max-per-query 10
  python -m pipeline.run collect --district Coimbatore --no-fetch     # list URLs only
  python -m pipeline.run collect --gdelt hi,en --timespan 1d
Storage: Postgres if DATABASE_URL is set, else data/source_article.jsonl (--jsonl forces file mode).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import configload                       # noqa: E402
from pipeline.collectors import gdelt as gdelt_mod    # noqa: E402
from pipeline.collectors import rss                   # noqa: E402
from pipeline.fetch import fetch_article              # noqa: E402
from pipeline.store import get_store                  # noqa: E402


def tier_for(domain: str | None) -> str:
    if not domain:
        return "aggregator"
    dom = domain.lower().removeprefix("www.")
    if dom in configload.NATIONAL_DOMAINS:
        return "national"
    return configload.outlet_tiers().get(dom, "regional")


def collect_district(d: dict, store, args) -> dict:
    kw = configload.keywords()
    stats = {"queries": 0, "items": 0, "new": 0, "dup_url": 0, "near_dup": 0,
             "robots": 0, "errors": 0}
    for query, lang in rss.build_queries(d, kw, max_lang_terms=args.lang_terms):
        stats["queries"] += 1
        try:
            items = rss.collect(query, lang, days=args.days,
                                max_items=args.max_per_query, resolve=True)
        except Exception as e:
            print(f"  WARN rss failed [{query}]: {e}")
            stats["errors"] += 1
            continue
        stats["items"] += len(items)
        for it in items:
            if store.seen_url(it.url):
                stats["dup_url"] += 1
                continue
            row = {"url": it.url, "outlet_name": it.source_name,
                   "outlet_tier": tier_for(it.source_domain), "language": it.language,
                   "state": d["state"], "district": d["district"],
                   "published_at": it.published_at, "raw_html": None,
                   "clean_text": None, "dedup_hash": None, "processing_status": "new"}
            if not args.no_fetch and it.resolved:
                try:
                    f = fetch_article(it.url, delay_s=args.delay)
                    if f.blocked_by_robots:
                        stats["robots"] += 1
                        continue
                    if f.clean_text and store.near_duplicate(f.dedup_hash):
                        row["processing_status"] = "near_duplicate"
                        stats["near_dup"] += 1
                    else:
                        row["processing_status"] = "fetched" if f.clean_text else "new"
                    row.update({"url": f.url, "raw_html": f.raw_html,
                                "clean_text": f.clean_text, "dedup_hash": f.dedup_hash,
                                "published_at": f.published_at or it.published_at})
                except Exception as e:
                    print(f"  WARN fetch failed {it.url[:60]}: {e}")
                    stats["errors"] += 1
            if store.insert_article(row) is not None:
                stats["new"] += 1
                mark = row["processing_status"]
                print(f"  + [{mark}] ({it.language}) {(f.title if not args.no_fetch and it.resolved and f.title else it.title)[:78]}")
    return stats


def cmd_collect(args) -> None:
    store = get_store(force_jsonl=args.jsonl)
    try:
        if args.gdelt:
            for lang in args.gdelt.split(","):
                items = gdelt_mod.collect(lang.strip(), timespan=args.timespan)
                fresh = [i for i in items if i.url and not store.seen_url(i.url)]
                print(f"GDELT [{lang}]: {len(items)} items, {len(fresh)} new")
                for it in fresh[:args.max_per_query]:
                    store.insert_article({
                        "url": it.url, "outlet_name": it.source_domain,
                        "outlet_tier": tier_for(it.source_domain),
                        "language": it.language, "state": None, "district": None,
                        "published_at": it.published_at, "raw_html": None,
                        "clean_text": None, "dedup_hash": None,
                        "processing_status": "new"})
            print("counts:", store.counts())
            return

        rows = configload.districts()
        if args.district:
            rows = [r for r in rows if r["district"].lower() == args.district.lower()]
        elif args.state:
            rows = [r for r in rows if r["state"].lower() == args.state.lower()]
        if not rows:
            raise SystemExit("no matching district/state in config_districts_master")
        print(f"collecting {len(rows)} district(s), window {args.days}d…")
        total: dict = {}
        for d in rows:
            print(f"\n== {d['district']}, {d['state']} ({d['primary_language']})")
            st = collect_district(d, store, args)
            print(f"   {st}")
            for k, v in st.items():
                total[k] = total.get(k, 0) + v
        print(f"\nTOTAL {total}")
        print("store counts:", store.counts())
    finally:
        store.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="pipeline.run")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="Phase 2 ingestion")
    c.add_argument("--district")
    c.add_argument("--state")
    c.add_argument("--gdelt", help="comma-separated langs, e.g. en,hi")
    c.add_argument("--timespan", default="1d", help="GDELT timespan (e.g. 1d, 12h)")
    c.add_argument("--days", type=int, default=7, help="RSS recency window")
    c.add_argument("--max-per-query", type=int, default=30)
    c.add_argument("--lang-terms", type=int, default=3)
    c.add_argument("--delay", type=float, default=2.0, help="per-domain fetch delay (s)")
    c.add_argument("--no-fetch", action="store_true", help="list/insert URLs, skip body fetch")
    c.add_argument("--jsonl", action="store_true", help="force file storage (no DB)")
    c.set_defaults(func=cmd_collect)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
