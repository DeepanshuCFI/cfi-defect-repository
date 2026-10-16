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
    "queue_depth_alert": 4000,        # 'fetched' backlog diverging = budget too low
    "new_stuck_alert": 15000,         # unresolvable 'new' articles piling up
    # Real-URL 'new' rows: fetched once, no text, then stranded. Its own threshold
    # because it hid inside new_stuck for 11 weeks — 4,512 rows sat far below the
    # 15,000 alert while the Google backlog they shared a bucket with drained to 0.
    # retry_stranded terminalises 300/run, so a sustained figure here means that
    # stage is failing, not that intake is high.
    "stranded_new_alert": 1500,
    "min_extracted_last_run": 1,      # 0 extractions = budget/API/collect silently dead
    "min_published_total_growth": 0,  # reserved for future use
    # Yield floors (16 Oct 2026 funnel): pins per 1,000 articles ran at 40 over 3.5
    # months (43 Hindi, 12 English) and 13% of fetched articles carried defect
    # vocabulary while producing 68% of pins. These are drift detectors, set well
    # below baseline — crossing one means collection is no longer aimed at the
    # publishable class (a feed change, a keyword regression, a blocked outlet).
    "min_yield_per_1000_7d": 10,
    "min_defect_vocab_share_7d": 0.05,
}


def yield_per_1000(pins: int, articles: int) -> float | None:
    """Published pins per 1,000 collected articles — None when nothing was collected."""
    return None if not articles else round(1000.0 * pins / articles, 1)


def defect_vocab_pattern(keywords: dict) -> str:
    """Case-insensitive regex alternation of every infra_defect term, all languages —
    the same vocabulary cmd_process uses to put defect stories first in line."""
    terms = sorted({t.lower() for lang in keywords.values() for t in lang.get("infra_defect", [])})
    return "|".join(re.escape(t) for t in terms)


def _cfg() -> dict:
    return {**DEFAULTS, **(configload.settings().get("health") or {})}


def collect_checks() -> list[tuple[str, bool, str]]:
    """[(name, ok, detail)] — pure data; decision thresholds from config."""
    cfg = _cfg()
    out = []
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
        out.append(("queue_depth", fetched < cfg["queue_depth_alert"],
                    f"extraction queue {fetched} (alert at {cfg['queue_depth_alert']}; "
                    "diverging queue means the daily budget can't keep up)"))
        out.append(("new_stuck", new < cfg["new_stuck_alert"],
                    f"unprocessed 'new' articles {new} (alert at {cfg['new_stuck_alert']}; "
                    f"{new - stranded} awaiting URL resolution, {stranded} stranded)"))
        out.append(("stranded_new", stranded < cfg["stranded_new_alert"],
                    f"fetched-but-textless 'new' articles {stranded} "
                    f"(alert at {cfg['stranded_new_alert']}; retry_stranded should be "
                    "draining these to a terminal status)"))

        cur.execute("""select coalesce((stage_stats->'auto_review'->>'auto_published')::int, 0),
                              coalesce((stage_stats->>'llm_spend_usd')::numeric, 0),
                              coalesce((stage_stats->'process'->>'extracted')::int, 0)
                            + coalesce((stage_stats->'process'->>'extracted_light')::int, 0)
                       from pipeline_run where ok is not null
                       order by id desc limit 1""")
        row = cur.fetchone()
        if row:
            pub, spend, extracted = int(row[0]), float(row[1]), int(row[2])
            # the 11-14 Jul freeze: runs "succeeded" while publishing nothing.
            out.append(("publishing_alive", pub > 0 or spend < 0.10,
                        f"last run auto-published {pub} (spend ${spend:.2f}) — "
                        "0 published at real spend means the adjudicator is starved"))
            # declared in DEFAULTS but never implemented until now (audit finding):
            # 0 extractions while a queue exists = dead key / blocked API / broken stage
            out.append(("extraction_alive", extracted >= cfg["min_extracted_last_run"]
                        or fetched == 0,
                        f"last run extracted {extracted} with {fetched} queued "
                        f"(alert below {cfg['min_extracted_last_run']})"))

        # ---- yield: is collection aimed at the publishable class? (16 Oct 2026 funnel)
        # Denominator = this week's articles that have text (near-dups and fetch
        # failures excluded); pins are counted by the week their ARTICLE arrived, so
        # the figure lags publication by a day or two and is a trend, not a tally.
        pat = defect_vocab_pattern(configload.keywords())
        cur.execute("""select count(*),
                              count(*) filter (where clean_text ~* %s)
                       from source_article
                       where created_at >= now() - interval '7 days'
                         and processing_status in ('fetched', 'extracted', 'irrelevant')""",
                    (pat,))
        arts7, vocab7 = cur.fetchone()
        cur.execute("""select count(*)
                       from incident i join source_article sa on sa.id = i.primary_source_id
                       where i.verification_status in ('auto_published', 'reviewed', 'verified')
                         and sa.created_at >= now() - interval '7 days'""")
        pins7 = cur.fetchone()[0]
        y = yield_per_1000(pins7, arts7)
        out.append(("yield_7d", y is None or y >= cfg["min_yield_per_1000_7d"],
                    f"{y} pins per 1,000 articles this week ({pins7} pins / {arts7} articles; "
                    f"alert below {cfg['min_yield_per_1000_7d']}; 3.5-month baseline 40)"))
        share = None if not arts7 else vocab7 / arts7
        out.append(("defect_vocab_share_7d",
                    share is None or share >= cfg["min_defect_vocab_share_7d"],
                    f"{'n/a' if share is None else f'{share:.0%}'} of this week's articles carry "
                    f"defect vocabulary (alert below {cfg['min_defect_vocab_share_7d']:.0%}; "
                    "baseline 13%, which produced 68% of all pins)"))
    return out


def run() -> dict:
    checks = collect_checks()
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
