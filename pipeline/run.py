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
    dcfg = configload.settings().get("dedup", {})
    ham_max = dcfg.get("simhash_hamming_max", 3)
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
                    if f.clean_text and store.near_duplicate(
                            f.dedup_hash, district=d["district"], state=d["state"],
                            hamming_max=ham_max):
                        row["processing_status"] = "near_duplicate"
                        stats["near_dup"] += 1
                    else:
                        row["processing_status"] = "fetched" if f.clean_text else "new"
                    # raw_html is NEVER stored: nothing in the pipeline reads it back
                    # (extraction/review/watch all use clean_text), and hoarding it blew
                    # the DB to 1.5GB and a disk-full outage (18 Jul). URLs allow
                    # re-fetching if a page is ever needed again.
                    row.update({"url": f.url, "raw_html": None,
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
        return total
    finally:
        store.close()


def _api_limit_hit(e: Exception) -> bool:
    """The Anthropic monthly spend-limit refusal — treat as 'stop for today', never as
    per-article failure (2026-07-13: 424 good articles were WARN'd through it)."""
    return "reached your specified api usage limits" in str(e).lower()


class ApiCredentialError(RuntimeError):
    """A dead/revoked/forbidden API key. Must be RUN-FATAL, never per-article: a
    silently-invalid key produced green runs that collected articles and extracted
    nothing (audit finding, 19 Jul)."""


def _raise_if_credential_error(e: Exception) -> None:
    name = type(e).__name__
    if name in ("AuthenticationError", "PermissionDeniedError"):
        raise ApiCredentialError(f"{name}: {str(e)[:200]}") from e
    if getattr(e, "status_code", None) in (401, 403):
        raise ApiCredentialError(f"HTTP {e.status_code}: {str(e)[:200]}") from e


def cmd_process(args) -> None:
    """Phase 3: fetched -> relevance -> extraction -> incident."""
    from pipeline import configload
    from pipeline.processing import extract as ex
    from pipeline.processing import prefilter
    from pipeline.processing import relevance as rel
    proc_cfg = configload.settings().get("processing", {})
    store = get_store(force_jsonl=args.jsonl)
    try:
        arts = store.articles_by_status("fetched", limit=args.limit)
        # budget goes to the mission-critical class first: defect-vocabulary stories are
        # processed before behaviour-only crash reports, so a budget stop cuts the
        # lowest-value tail (owner: ok to miss a few entries to stay under $2/day).
        defect_terms = [t for lang in configload.keywords().values()
                        for t in lang.get("infra_defect", [])]
        arts.sort(key=lambda a: 0 if any(
            t in (a.get("clean_text") or "") for t in defect_terms) else 1)
        print(f"processing {len(arts)} fetched article(s)…")
        stats = {"prefiltered": 0, "irrelevant": 0, "extracted": 0, "extracted_light": 0,
                 "skipped_pure_crash": 0, "failed": 0, "snippets_dropped": 0}
        from pipeline import llmcost
        # Extraction may spend only its SHARE of the daily budget; the rest is reserved
        # for adjudication (which gates publication) — without a reserve, extraction ate
        # 100% and nothing published (11-14 Jul map freeze). The reserve is now SIZED TO
        # DEMAND rather than a flat 50%, which was leaving ~35% of the budget unspent
        # while the extraction queue grew (measured 19-20 Jul).
        pending_adj = 0
        try:
            from pipeline.db import connect as _c
            with _c() as _conn, _conn.cursor() as _cur:
                _cur.execute("""select count(*) from incident i
                                join source_article a on a.id = i.primary_source_id
                                where i.verification_status = 'auto'
                                  and a.clean_text is not null""")
                pending_adj = int(_cur.fetchone()[0])
        except Exception as e:            # jsonl/no-DB mode: fall back to the flat split
            print(f"  (reserve sizing unavailable: {e}; using configured share)")
            pending_adj = -1
        if pending_adj >= 0:
            ext_share = llmcost.extraction_share(
                pending_adj + int(proc_cfg.get("adjudication_new_allowance", 30)),
                llmcost.budget(),
                float(proc_cfg.get("adjudication_cost_per_item_usd", 0.015)),
                float(proc_cfg.get("adjudication_reserve_floor", 0.15)),
                float(proc_cfg.get("adjudication_reserve_cap", 0.5)))
            print(f"  budget split: {pending_adj} adjudications pending -> extraction "
                  f"gets {ext_share*100:.0f}% (${llmcost.budget()*ext_share:.2f})")
        else:
            ext_share = float(proc_cfg.get("extraction_budget_share", 0.5))
        for n_done, a in enumerate(arts):
            if llmcost.over(ext_share):
                print(f"  EXTRACTION BUDGET STOP: ${llmcost.spent():.2f} >= "
                      f"${llmcost.budget() * ext_share:.2f} (adjudication reserve kept) — "
                      f"{len(arts) - n_done} article(s) stay queued for tomorrow")
                break
            text = a.get("clean_text") or ""
            if not text.strip():
                store.set_article_status(a["id"], "failed")
                stats["failed"] += 1
                continue
            if proc_cfg.get("body_prefilter", True) and not prefilter.passes(text):
                store.set_article_status(a["id"], "irrelevant")
                stats["prefiltered"] += 1
                continue                      # zero tokens spent
            title = text.split("\n", 1)[0][:140]
            try:
                cls = rel.classify(title, text)
            except Exception as e:
                _raise_if_credential_error(e)
                if _api_limit_hit(e):
                    print("  API MONTHLY LIMIT hit — stopping; queue waits for tomorrow")
                    stats["api_limit_hit"] = True
                    break
                print(f"  WARN relevance failed #{a['id']}: {e}")
                stats["failed"] += 1
                continue
            if not cls.get("in_scope"):
                store.set_article_status(a["id"], "irrelevant")
                stats["irrelevant"] += 1
                print(f"  - irrelevant #{a['id']} ({cls.get('kind')}): {cls.get('reason','')[:60]}")
                continue
            pure_crash = cls.get("kind") == "crash"
            if pure_crash and proc_cfg.get("skip_pure_crashes", False):
                # defect-focus max-savings mode: don't extract behaviour-only crashes.
                # NOTE: weakens the >=3-in-6mo escalation counter — off by default.
                store.set_article_status(a["id"], "irrelevant")
                stats["skipped_pure_crash"] += 1
                continue
            light = pure_crash and proc_cfg.get("tiered_extraction", True)
            try:
                inc, dropped = ex.extract(title, text,
                                          published_at=str(a.get("published_at") or "") or None,
                                          light=light)
            except Exception as e:
                _raise_if_credential_error(e)
                if _api_limit_hit(e):
                    print("  API MONTHLY LIMIT hit — stopping; queue waits for tomorrow")
                    stats["api_limit_hit"] = True
                    break
                print(f"  WARN extraction failed #{a['id']}: {e}")
                store.set_article_status(a["id"], "failed")
                stats["failed"] += 1
                continue
            stats["snippets_dropped"] += len(dropped)
            if dropped:
                print(f"    ! dropped non-verbatim snippets: {dropped}")
            inc["location_text_raw"] = inc.get("location_text_best")
            inc["primary_source_id"] = a["id"]
            defects = inc.pop("defects", [])
            try:
                iid = store.insert_incident(inc, defects, a["id"])
            except Exception as e:
                # one poisoned extraction must never kill the whole daily run
                print(f"  WARN insert failed #{a['id']}: {e}")
                store.set_article_status(a["id"], "failed")
                stats["failed"] += 1
                continue
            store.set_article_status(a["id"], "extracted")
            if not inc["infra_implicated"]:
                # crash-only record: keep for the >=3-in-6mo frequency counter, but it
                # can never publish — no human queue, no paid second-pass adjudication.
                store.set_incident_status(iid, "machine_ok",
                    "crash-only at extraction (infra_implicated=false) -> machine_ok; "
                    "kept for crash-frequency counts")
            stats["extracted_light" if light else "extracted"] += 1
            dstr = ",".join(d["defect_type"] for d in defects) or "-"
            print(f"  + incident #{iid} <- art #{a['id']} [{cls.get('kind')}] "
                  f"F{inc['fatalities']}/I{inc['injuries']} conf={inc['extraction_confidence']:.2f} "
                  f"infra={inc['infra_implicated']} defects={dstr}")
            print(f"      loc: {inc.get('location_text_best','')[:90]}")
        print(f"\nDONE {stats} · llm spend so far ${llmcost.spent():.2f}"
              f" (budget ${llmcost.budget():.2f})")
    finally:
        store.close()


def cmd_geocode(args) -> None:
    """Phase 4: resolve location_text_best -> lat/lon + confidence + method."""
    import json as _json

    from pipeline.processing.geocode import geocode as geo
    from pipeline.settings import DATABASE_URL, ROOT
    use_db = not args.jsonl and DATABASE_URL and "REPLACE_ME" not in DATABASE_URL
    dist: dict[str, int] = {}
    if use_db:
        from pipeline.db import connect
        with connect() as conn, conn.cursor() as cur:
            # Fall back to the SOURCE ARTICLE's state/district when extraction returned
            # null: the collector always knows them (the district query that found the
            # article). Without this the geocoder is unanchored and can match a village
            # homonym in any state (#276: Hathras UP -> Odisha).
            cur.execute("""select i.id, i.location_text_best, i.road_name, i.admin_city,
                                  coalesce(i.admin_district, a.district),
                                  coalesce(i.admin_state, a.state),
                                  i.admin_state is null and a.state is not null
                           from incident i
                           left join source_article a on a.id = i.primary_source_id
                           where i.geom is null limit %s""", (args.limit,))
            rows = cur.fetchall()
            print(f"geocoding {len(rows)} incident(s)…")
            for iid, loc, road, city, distr, state, used_fallback in rows:
                g = geo(loc or "", road, city, distr, state)
                if g["lat"] is not None:
                    cur.execute(
                        """update incident set geom=ST_GeogFromText(%s),
                           geocode_confidence=%s, geocode_method=%s,
                           geocode_qualifier=%s where id=%s""",
                        (f"POINT({g['lon']} {g['lat']})", g["geocode_confidence"],
                         g["geocode_method"], g.get("geocode_qualifier"), iid))
                    if used_fallback:
                        # the hit passed _state_ok against this state, so labelling the
                        # record with it is consistent with where it was actually placed
                        cur.execute("""update incident set admin_state=coalesce(admin_state,%s),
                                       admin_district=coalesce(admin_district,%s) where id=%s""",
                                    (state, distr, iid))
                        cur.execute("""insert into review_action (entity_type, entity_id,
                                       reviewer, action, note) values
                                       ('incident',%s,'pipeline:rule','edit',%s)""",
                                    (iid, f"admin_state/district backfilled from source "
                                          f"article ({state}/{distr}) — extraction returned null"))
                dist[g["geocode_method"] or "unresolved"] = dist.get(g["geocode_method"] or "unresolved", 0) + 1
                print(f"  #{iid} {g['geocode_method'] or 'UNRESOLVED'} "
                      f"conf={g['geocode_confidence']} {(g['display_name'] or '')[:70]}")
            conn.commit()

            # RESCUE PASS (Mapbox only): re-geocode queue items stuck below the 0.6
            # publish bar. Improvements update in place; items crossing the bar flip
            # needs_human -> auto so the auto-reviewer re-judges them once — this is
            # how the geocode-blocked queue melts without humans.
            from pipeline.settings import MAPBOX_TOKEN
            if MAPBOX_TOKEN:
                cur.execute("""
                  select i.id, i.location_text_best, i.road_name, i.admin_city,
                         coalesce(i.admin_district, a.district),
                         coalesce(i.admin_state, a.state), i.geocode_confidence,
                         i.verification_status
                  from incident i
                  left join source_article a on a.id = i.primary_source_id
                  where i.verification_status in ('auto','needs_human')
                    and (i.geocode_confidence is null or i.geocode_confidence < 0.6)
                  limit %s""", (args.limit,))
                rescue = cur.fetchall()
                print(f"\nrescue pass: re-geocoding {len(rescue)} low-confidence item(s)…")
                saved = 0
                for iid, loc, road, city, distr, state, old_conf, status in rescue:
                    g = geo(loc or "", road, city, distr, state)
                    if g["lat"] is None or (g["geocode_confidence"] or 0) <= (old_conf or 0):
                        continue
                    cur.execute(
                        """update incident set geom=ST_GeogFromText(%s),
                           geocode_confidence=%s, geocode_method=%s,
                           geocode_qualifier=%s where id=%s""",
                        (f"POINT({g['lon']} {g['lat']})", g["geocode_confidence"],
                         g["geocode_method"], g.get("geocode_qualifier"), iid))
                    if g["geocode_confidence"] >= 0.6 and status == "needs_human":
                        cur.execute("update incident set verification_status='auto' "
                                    "where id=%s", (iid,))
                        cur.execute("""insert into review_action (entity_type, entity_id,
                                       reviewer, action, note) values
                                       ('incident',%s,'pipeline:rule','edit',%s)""",
                                    (iid, f"geocode improved {old_conf} -> "
                                          f"{g['geocode_confidence']} (mapbox rescue); "
                                          "requeued for auto-review"))
                    saved += 1
                    print(f"  rescued #{iid}: {old_conf} -> {g['geocode_confidence']} "
                          f"({g['geocode_method']})")
                conn.commit()
                print(f"rescue pass: {saved} improved")
    else:
        path = ROOT / "data" / "incident.jsonl"
        rows = [_json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
        todo = [r for r in rows if r.get("lat") is None][:args.limit]
        print(f"geocoding {len(todo)} incident(s)…")
        for r in todo:
            g = geo(r.get("location_text_best") or "", r.get("road_name"),
                    r.get("admin_city"), r.get("admin_district"), r.get("admin_state"))
            r.update(g)
            dist[g["geocode_method"] or "unresolved"] = dist.get(g["geocode_method"] or "unresolved", 0) + 1
            print(f"  #{r['id']} {g['geocode_method'] or 'UNRESOLVED'} conf={g['geocode_confidence']}")
            print(f"     loc: {(r.get('location_text_best') or '')[:76]}")
            print(f"     got: {(g['display_name'] or '-')[:76]}  ({g['lat']}, {g['lon']})")
        with open(path, "w") as f:
            for r in rows:
                f.write(_json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nmethod distribution: {dist}")


def cmd_recompute(args) -> None:
    """Phase 5+6 nightly job: dedup incidents, rebuild hotspots, rescore."""
    from pipeline.processing import cluster as cl
    from pipeline.processing import dedup as dd
    from pipeline.processing import score as sc
    print("1/3 dedup…")
    print("   ", dd.run())
    print("2/3 cluster…")
    print("   ", cl.run())
    print("3/3 score…")
    print("   ", sc.run())
    from pipeline.db import connect
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select h.id, h.priority_score, h.score_breakdown->>'tier', h.escalation_candidate,
                 h.incident_count, h.fatality_count, h.injury_count,
                 coalesce(h.road_name,'?'), coalesce(h.admin_district,'?'), coalesce(h.admin_state,'?'),
                 array_to_string(h.dominant_defects,',')
          from hotspot h order by h.priority_score desc nulls last limit 15""")
        print("\nTOP HOTSPOTS")
        for (hid, score, t, esc, n, f, i, road, dist, st, dom) in cur.fetchall():
            flag = " ⚑ESCALATE" if esc else ""
            print(f"  #{hid:>3} {score:5.1f} [{t:^8}]{flag} n={n} F{f}/I{i}  "
                  f"{road[:34]} · {dist}, {st}  [{dom or '-'}]")


def cmd_daily(args) -> None:
    """The cron entrypoint: collect -> process -> geocode -> recompute -> export,
    with telemetry to pipeline_run (surfaced on /qa). States from config
    ingestion.daily_states (default ['Bihar'])."""
    import json as _json
    import traceback

    from pipeline.db import connect
    states = configload.settings().get("ingestion", {}).get("daily_states", ["Bihar"])
    with connect() as conn, conn.cursor() as cur:
        cur.execute("insert into pipeline_run (note) values (%s) returning id",
                    (f"daily: {', '.join(states)}",))
        run_id = cur.fetchone()[0]
        conn.commit()
    stats, ok = {}, True

    # Each stage is independently guarded: a failure is recorded but never blocks the
    # later stages — in particular export ALWAYS runs, so the site deploys whatever is
    # committed instead of going stale (run #9 postmortem, 2026-07-10).
    def stage(name, fn):
        nonlocal ok
        try:
            stats[name] = fn() or "done"
        except Exception:
            ok = False
            stats[name] = "FAILED"
            err = f"{name}: " + traceback.format_exc()[-800:]
            stats.setdefault("errors", []).append(err)
            print(f"STAGE FAILED ({err})")

    def _collect():
        agg: dict = {}
        for st in states:
            t = cmd_collect(argparse.Namespace(
                district=None, state=st, gdelt=None, timespan="1d",
                days=args.days, max_per_query=15, lang_terms=3,
                delay=2.0, no_fetch=False, jsonl=False)) or {}
            for k, v in t.items():
                agg[k] = agg.get(k, 0) + v
        return agg

    from pipeline import llmcost
    from pipeline.processing import auto_review as ar
    from scripts.export_public import main as export_main

    # per-DAY budget: seed the meter with what earlier runs already spent today,
    # so a retry run can never double the daily cap.
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""select coalesce(sum((stage_stats->>'llm_spend_usd')::numeric), 0)
                       from pipeline_run
                       where started_at::date = current_date and id <> %s""", (run_id,))
        prior = float(cur.fetchone()[0])
    if prior:
        llmcost.set_baseline(prior)
        print(f"budget: ${prior:.2f} already spent today by earlier runs "
              f"(daily cap ${llmcost.budget():.2f})")

    from pipeline.processing import watch

    def _watch():
        store = get_store()
        try:
            return watch.run(store)
        finally:
            store.close()

    stage("collect", _collect)
    stage("process", lambda: cmd_process(argparse.Namespace(limit=1000, jsonl=False)))
    stage("geocode", lambda: cmd_geocode(argparse.Namespace(limit=1000, jsonl=False)))
    stage("watch", _watch)
    stage("auto_review", ar.run)
    def _hygiene():
        # raw_html has no purpose once an article is terminal; keeping it blew the DB
        # past the Supabase free quota (1.5GB, 20 Jul). Plain VACUUM keeps the freed
        # space reusable so the table stops growing.
        with connect() as conn, conn.cursor() as cur:
            # belt-and-suspenders: raw_html should never be stored at all now
            cur.execute("""update source_article set raw_html = null
                           where raw_html is not null""")
            purged = cur.rowcount
            conn.commit()
        vc = connect()
        vc.autocommit = True
        vc.execute("vacuum source_article")
        vc.close()
        return {"raw_html_purged": purged}

    from pipeline.processing import health
    stage("recompute", lambda: cmd_recompute(argparse.Namespace()))
    stage("export", export_main)
    stage("hygiene", _hygiene)
    stage("health", health.run)
    stats["llm_spend_usd"] = round(llmcost.spent(), 2)

    # CANARIES (audit finding, 19 Jul): a run that collects nothing, extracts nothing,
    # or exports nothing used to exit 0 with a green check — the failure mode that hid
    # the 13 Jul API-limit trip and would hide blocked RSS or a dead decoder. Any
    # canary tripping flips ok=False, which raises SystemExit(1) -> GitHub failure email.
    canaries = []
    c_stats = stats.get("collect")
    if isinstance(c_stats, dict) and c_stats.get("new", 0) == 0:
        canaries.append("collect produced 0 new articles (RSS blocked / decoder dead?)")

    p_stats = stats.get("process")
    if isinstance(p_stats, dict):
        did_work = (p_stats.get("extracted", 0) + p_stats.get("extracted_light", 0)
                    + p_stats.get("irrelevant", 0) + p_stats.get("prefiltered", 0))
        budget_stopped = llmcost.over(0.9) or p_stats.get("api_limit_hit")
        if did_work == 0 and not budget_stopped:
            canaries.append("process handled 0 articles with budget remaining "
                            "(queue empty, or extraction is broken)")
        if p_stats.get("api_limit_hit"):
            canaries.append("Anthropic monthly spend limit was hit — raise the console "
                            "limit or the pipeline stalls until month end")

    if stats.get("export") == "FAILED":
        canaries.append("export failed — the deployed site may be stale or empty")

    if canaries:
        ok = False
        stats["canaries"] = canaries
        for c in canaries:
            print(f"CANARY: {c}")
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""update pipeline_run set finished_at=now(), ok=%s,
                       stage_stats=%s::jsonb where id=%s""",
                    (ok, _json.dumps(stats), run_id))
        conn.commit()
    print(f"daily run #{run_id} ok={ok}")
    if not ok:
        raise SystemExit(1)


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

    pr = sub.add_parser("process", help="Phase 3: relevance + extraction")
    pr.add_argument("--limit", type=int, default=50)
    pr.add_argument("--jsonl", action="store_true", help="force file storage (no DB)")
    pr.set_defaults(func=cmd_process)

    g = sub.add_parser("geocode", help="Phase 4: geocode incidents lacking geom")
    g.add_argument("--limit", type=int, default=100)
    g.add_argument("--jsonl", action="store_true", help="force file storage (no DB)")
    g.set_defaults(func=cmd_geocode)

    rc = sub.add_parser("recompute",
                        help="Phase 5+6: dedup -> cluster -> score (the nightly job)")
    rc.set_defaults(func=cmd_recompute)

    arv = sub.add_parser("autoreview", help="2nd-pass AI adjudication of the review queue")
    arv.add_argument("--limit", type=int, default=200)
    arv.set_defaults(func=lambda a: print(__import__('pipeline.processing.auto_review',
        fromlist=['run']).run(a.limit)))

    dl = sub.add_parser("daily", help="cron: collect->process->geocode->autoreview->recompute->export")
    dl.add_argument("--days", type=int, default=2, help="RSS window (steady-state)")
    dl.set_defaults(func=cmd_daily)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
