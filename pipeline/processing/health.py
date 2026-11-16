"""Resource-health watchdog (born from the 18 Jul Supabase disk-full outage).

Runs as a daily stage. Checks every measurable resource against config thresholds
and posts ONE batched Slack alert (same SLACK_WEBHOOK_URL as the site watch) when
anything crosses a line — so cliffs announce themselves weeks early instead of
arriving as provider enforcement emails. Silent when all green.

Thresholds live in config settings under "health" (config/config_settings.json).
"""
import os
import re

import httpx

from pipeline import configload
from pipeline.db import connect
from pipeline.store import GNEWS_LIKE

DEFAULTS = {
    "db_size_alert_mb": 2000,         # Pro plan, 8 GB disk; was 400 from the free-tier era and
                                      # fired daily once the 12 Oct burn took the DB past 476 MB
    # How far behind extraction is on the articles that matter, in days: the age of
    # the oldest unread defect-vocabulary article. Replaces queue_depth (a count over
    # 4,000), which had fired on every run for weeks and so told nobody anything; on
    # 14 Nov 2026 the count was 13,134 and the fact that mattered — the run was
    # reading 4 Nov arrivals — was nowhere on the alert.
    "defect_queue_max_age_days": 3,
    # 'new' articles awaiting URL resolution. Steady state since the Oct resolver fix
    # is 0, so 15,000 could not fire before weeks of total loss.
    "new_stuck_alert": 1500,
    # Real-URL 'new' rows: fetched once, no text, then stranded. Its own threshold
    # because it hid inside new_stuck for 11 weeks — 4,512 rows sat far below the
    # 15,000 alert while the Google backlog they shared a bucket with drained to 0.
    # retry_stranded terminalises 300/run, so a sustained figure here means that
    # stage is failing, not that intake is high.
    "stranded_new_alert": 600,
    "min_extracted_last_run": 1,      # 0 extractions = budget/API/collect silently dead
    "min_published_total_growth": 0,  # reserved for future use
    # Published reports per 1,000 articles the model actually READ in the last 7 days.
    # The earlier figure divided by articles that ARRIVED in the week; once the queue
    # lag passed 7 days none of them had been read, and it reported 0.0 daily while
    # real yield was at a record (about 440 per 1,000 read, 14 Nov 2026). The floor is
    # a breakage detector: even the non-defect tail of the queue yields about 70.
    "min_yield_per_1000_read_7d": 40,
    "min_read_for_yield_7d": 100,     # below this the ratio is noise — no verdict
    # Share of the week's arrivals that carry defect vocabulary: is collection still
    # aimed at the publishable class? 13% at the 16 Oct baseline, 15-22% since the
    # defect-term queries; 5% could not catch a real regression.
    "min_defect_vocab_share_7d": 0.08,
    # An adjudicator that judges this many records and publishes none is rejecting
    # everything (the 11-14 Jul freeze); fewer than this is just a small day.
    "min_reviewed_for_publish_check": 20,
}

# Keys of the current run's stage counters that the liveness verdicts read. A shape
# test holds each list against what the stage really records — the 21 Sep 2026 false
# alarm was a reader and a writer of one shape drifting apart.
PROCESS_KEYS_READ = ("extracted", "extracted_light", "irrelevant", "failed", "loaded",
                     "stopped_reason")
AUTO_REVIEW_KEYS_READ = ("reviewed", "auto_published", "errors")


def yield_per_1000(pins: int, articles: int) -> float | None:
    """Published pins per 1,000 articles — None when the denominator is empty."""
    return None if not articles else round(1000.0 * pins / articles, 1)


def lag_days(oldest, now) -> float | None:
    """Age in days of the oldest waiting article; None when nothing is waiting."""
    if oldest is None:
        return None
    return round((now - oldest).total_seconds() / 86400.0, 1)


def extraction_verdict(p, queued: int, min_extracted: int = 1) -> tuple[bool, str]:
    """Did THIS run's process stage do its job? (Pure.)

    Judged on the current run's own counters. The earlier check read the previous
    finished run from the database, so it described yesterday, and read 0 for a
    stage that had raised."""
    if not isinstance(p, dict):
        return False, f"process stage did not complete this run ({p!r}); {queued} queued"
    done = int(p.get("extracted") or 0) + int(p.get("extracted_light") or 0)
    failed, irrelevant = int(p.get("failed") or 0), int(p.get("irrelevant") or 0)
    if p.get("api_limit_hit"):
        return False, (f"process stopped on a usage limit or network block after "
                       f"{done} extraction(s); {queued} queued")
    if failed >= 5 and failed > done + irrelevant:
        return False, (f"process failed on {failed} article(s) against {done} extracted "
                       f"and {irrelevant} judged irrelevant — the model calls are erroring")
    if (done < min_extracted and int(p.get("loaded") or 0) > 0
            and p.get("stopped_reason") != "extraction_budget"):
        return False, (f"process loaded {p.get('loaded')} article(s) and extracted {done} "
                       f"(alert below {min_extracted}); {queued} queued")
    return True, f"this run extracted {done} ({failed} failed) with {queued} queued"


def publishing_verdict(ar, waiting: int, min_reviewed: int = 20) -> tuple[bool, str]:
    """Did THIS run's adjudicator work, and did it publish anything? (Pure.)

    `waiting` = publishable records still queued after the stage. The earlier check
    passed whenever spend was under $0.10 — which is exactly what a day of refused
    calls looks like (25, 28 and 30 Oct 2026)."""
    if not isinstance(ar, dict):
        return False, (f"auto_review did not complete this run ({ar!r}); "
                       f"{waiting} publishable record(s) waiting")
    reviewed, pub = int(ar.get("reviewed") or 0), int(ar.get("auto_published") or 0)
    errors = int(ar.get("errors") or 0)
    if ar.get("api_limit_hit"):
        return False, (f"auto_review stopped on a usage limit or network block after "
                       f"{reviewed} verdict(s); {waiting} publishable record(s) waiting")
    if errors >= 5 and errors > reviewed:
        return False, (f"auto_review errored on {errors} record(s) against {reviewed} "
                       "judged — the model calls are failing")
    if reviewed == 0 and waiting > 0:
        return False, (f"auto_review judged 0 records with {waiting} publishable "
                       "record(s) waiting — the adjudicator is starved or broken")
    if reviewed >= min_reviewed and pub == 0:
        return False, (f"auto_review judged {reviewed} records and published none — "
                       "it is rejecting everything")
    return True, f"this run judged {reviewed} and published {pub}; {waiting} still waiting"


def defect_vocab_pattern(keywords: dict) -> str:
    """Case-insensitive regex alternation of every infra_defect term, all languages —
    the same vocabulary cmd_process uses to put defect stories first in line."""
    terms = sorted({t.lower() for lang in keywords.values() for t in lang.get("infra_defect", [])})
    return "|".join(re.escape(t) for t in terms)


def _cfg() -> dict:
    return {**DEFAULTS, **(configload.settings().get("health") or {})}


def collect_checks(current: dict | None = None) -> list[tuple[str, bool, str]]:
    """[(name, ok, detail)] — pure data; decision thresholds from config.

    `current` is the running pipeline's stage counters (cmd_daily's stats dict). The
    two liveness checks judge it; without it (health run on its own) they are skipped,
    because the only alternative is the previous run, which is yesterday's answer."""
    cfg = _cfg()
    out = []
    pat = defect_vocab_pattern(configload.keywords())
    with connect() as conn, conn.cursor() as cur:
        cur.execute("select pg_database_size(current_database()) / 1024 / 1024")
        mb = int(cur.fetchone()[0])
        out.append(("db_size", mb < cfg["db_size_alert_mb"],
                    f"database {mb}MB (alert at {cfg['db_size_alert_mb']}MB)"))

        cur.execute("""select
             count(*) filter (where processing_status = 'fetched'),
             count(*) filter (where processing_status = 'new'),
             count(*) filter (where processing_status = 'new'
                              and url not like %s)
             from source_article""", (GNEWS_LIKE,))
        fetched, new, stranded = cur.fetchone()

        # ---- extraction lag: how old is the oldest unread article that matters?
        cur.execute("""select min(created_at), count(*), now()
                       from source_article
                       where processing_status = 'fetched' and clean_text ~* %s""", (pat,))
        oldest, defect_queued, db_now = cur.fetchone()
        lag = lag_days(oldest, db_now)
        out.append(("extraction_lag",
                    lag is None or lag <= cfg["defect_queue_max_age_days"],
                    f"oldest unread defect-vocabulary article is "
                    f"{'n/a' if lag is None else f'{lag} days'} old (alert above "
                    f"{cfg['defect_queue_max_age_days']}); {defect_queued} such article(s) "
                    f"waiting, {fetched} in the queue in all"))
        out.append(("new_stuck", new < cfg["new_stuck_alert"],
                    f"unprocessed 'new' articles {new} (alert at {cfg['new_stuck_alert']}; "
                    f"{new - stranded} awaiting URL resolution, {stranded} stranded)"))
        out.append(("stranded_new", stranded < cfg["stranded_new_alert"],
                    f"fetched-but-textless 'new' articles {stranded} "
                    f"(alert at {cfg['stranded_new_alert']}; retry_stranded should be "
                    "draining these to a terminal status)"))

        # ---- liveness: judged on THIS run's counters, not the last finished run's
        p_now = (current or {}).get("process")
        if current is not None:
            from pipeline.processing import auto_review
            floor = getattr(auto_review, "GEOCODE_FLOOR", 0.6)   # decide()'s location bar
            cur.execute("""select count(*) from incident i
                           join source_article a on a.id = i.primary_source_id
                           where i.verification_status = 'auto'
                             and a.clean_text is not null and i.geom is not null
                             and i.geocode_confidence >= %s""", (floor,))
            waiting = int(cur.fetchone()[0])
            ok, detail = publishing_verdict(current.get("auto_review"), waiting,
                                            cfg["min_reviewed_for_publish_check"])
            out.append(("publishing_alive", ok, detail))
            ok, detail = extraction_verdict(p_now, fetched, cfg["min_extracted_last_run"])
            out.append(("extraction_alive", ok, detail))

        # ---- yield: of the articles the model READ this week, how many became
        # published reports? Denominator = the daily runs' own process counters
        # (finished runs from pipeline_run, plus this run's). Numerator = incidents
        # created inside those runs' time windows and since published — so a one-off
        # backlog burn outside the daily run inflates neither side.
        cur.execute("""select coalesce(sum(
                                coalesce((stage_stats->'process'->>'extracted')::int, 0)
                              + coalesce((stage_stats->'process'->>'extracted_light')::int, 0)
                              + coalesce((stage_stats->'process'->>'irrelevant')::int, 0)), 0)
                       from pipeline_run
                       where finished_at is not null
                         and started_at >= now() - interval '7 days'
                         and jsonb_typeof(stage_stats->'process') = 'object'""")
        read7 = int(cur.fetchone()[0])
        if isinstance(p_now, dict):
            read7 += sum(int(p_now.get(k) or 0)
                         for k in ("extracted", "extracted_light", "irrelevant"))
        cur.execute("""select count(*) from incident i
                       where i.verification_status in ('auto_published', 'reviewed', 'verified')
                         and i.created_at >= now() - interval '7 days'
                         and exists (select 1 from pipeline_run pr
                                     where pr.started_at >= now() - interval '7 days'
                                       and i.created_at >= pr.started_at
                                       and i.created_at <= coalesce(pr.finished_at, now()))""")
        pins7 = int(cur.fetchone()[0])
        y = yield_per_1000(pins7, read7) if read7 >= cfg["min_read_for_yield_7d"] else None
        out.append(("yield_7d", y is None or y >= cfg["min_yield_per_1000_read_7d"],
                    f"{'n/a' if y is None else y} published reports per 1,000 articles read "
                    f"this week ({pins7} published / {read7} read; alert below "
                    f"{cfg['min_yield_per_1000_read_7d']})"))

        # ---- is collection aimed at the publishable class? (16 Oct 2026 funnel)
        cur.execute("""select count(*),
                              count(*) filter (where clean_text ~* %s)
                       from source_article
                       where created_at >= now() - interval '7 days'
                         and processing_status in ('fetched', 'extracted', 'irrelevant')""",
                    (pat,))
        arts7, vocab7 = cur.fetchone()
        share = None if not arts7 else vocab7 / arts7
        out.append(("defect_vocab_share_7d",
                    share is None or share >= cfg["min_defect_vocab_share_7d"],
                    f"{'n/a' if share is None else f'{share:.0%}'} of this week's {arts7} "
                    f"articles carry defect vocabulary (alert below "
                    f"{cfg['min_defect_vocab_share_7d']:.0%}; 13% at the 16 Oct baseline)"))
    return out


def run(current: dict | None = None) -> dict:
    checks = collect_checks(current)
    bad = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'OK  ' if ok else 'ALERT'} {name}: {detail}")
    if bad:
        hook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        if hook:
            lines = ["⚠️ *Pipeline health — thresholds crossed*"] + [
                f"• *{n}*: {d}" for n, _, d in bad]
            lines.append("_Early warning — act before the provider enforces it._")
            try:
                httpx.post(hook, json={"text": "\n".join(lines)}, timeout=20)
            except Exception as e:
                print(f"  WARN health alert failed to send: {e}")
        else:
            print("  WARN health alerts have no SLACK_WEBHOOK_URL — console only")
    return {"checks": len(checks), "alerts": len(bad),
            "alert_names": [n for n, _, _ in bad]}
