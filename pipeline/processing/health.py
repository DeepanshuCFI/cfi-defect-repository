"""Resource-health watchdog (born from the 18 Jul Supabase disk-full outage).

Runs as a daily stage. Checks every measurable resource against config thresholds
and posts ONE batched Slack alert (same SLACK_WEBHOOK_URL as the site watch) when
anything crosses a line — so cliffs announce themselves weeks early instead of
arriving as provider enforcement emails. Silent when all green.

Thresholds live in config settings under "health" (config/config_settings.json).
"""
import os

import httpx

from pipeline import configload
from pipeline.db import connect

DEFAULTS = {
    "db_size_alert_mb": 400,          # free limit was 500; Pro is 8GB but drift is drift
    "queue_depth_alert": 4000,        # 'fetched' backlog diverging = budget too low
    "new_stuck_alert": 15000,         # unresolvable 'new' articles piling up
    "min_extracted_last_run": 1,      # 0 extractions = budget/API/collect silently dead
    "min_published_total_growth": 0,  # reserved for future use
}


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
             count(*) filter (where processing_status = 'new')
             from source_article""")
        fetched, new = cur.fetchone()
        out.append(("queue_depth", fetched < cfg["queue_depth_alert"],
                    f"extraction queue {fetched} (alert at {cfg['queue_depth_alert']}; "
                    "diverging queue means the daily budget can't keep up)"))
        out.append(("new_stuck", new < cfg["new_stuck_alert"],
                    f"unprocessed 'new' articles {new} (alert at {cfg['new_stuck_alert']})"))

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
