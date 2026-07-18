"""Named-site watch (owner feature, 2026-07-20): standing surveillance of CFI's
audited junctions from the Rakshak authority-complaint sheet.

Three parts, all cheap:
  collect_sites(store) — 2 targeted Google News RSS queries per enabled site
                         (free; articles ride the normal pipeline)
  run_matches()        — name-match new articles / geo-match new incidents (<=500m)
                         against the watch list; records watch_hit rows
  notify()             — one batched Slack message for unnotified hits via
                         SLACK_WEBHOOK_URL (graceful no-op when the secret is absent)

Name matching is conjunctive to survive generic names ("Medical College Junction"):
a variant must appear in the text AND the site's city/district must be corroborated
by the article (its own text or the collector's district tag).
"""
import os

import httpx

from pipeline.db import connect

GEO_RADIUS_M = 500
LOOKBACK_DAYS = 3          # articles/incidents newer than this are match candidates


# ---------------------------------------------------------------- pure match logic
def name_matches(text: str, article_district: str | None, variants: list[str],
                 city: str | None, district: str | None) -> bool:
    """Variant substring + locality corroboration (city in text OR same district)."""
    t = (text or "").lower()
    if not t or not any((v or "").lower() in t for v in variants):
        return False
    if city and city.lower() in t:
        return True
    if district and (district.lower() in t
                     or district.lower() == (article_district or "").lower()):
        return True
    return not city and not district      # site with no locality: variant is enough


# ---------------------------------------------------------------- collection
def collect_sites(store, days: int = 3, max_per_query: int = 10) -> dict:
    from pipeline.collectors import rss
    from pipeline.fetch import fetch_article
    from pipeline.run import tier_for
    stats = {"queries": 0, "new": 0}
    with connect() as conn:
        sites = conn.execute("""select name, city, district, state from watch_site
                                where enabled""").fetchall()
    for name, city, district, state in sites:
        loc = city or district or ""
        for query, lang in ((f'"{name}" {loc} accident OR crash OR pothole', "en"),
                            (f'"{name}" हादसा OR दुर्घटना OR गड्ढा', "hi")):
            stats["queries"] += 1
            try:
                items = rss.collect(query, lang, days=days, max_items=max_per_query,
                                    resolve=True)
            except Exception as e:
                print(f"  WARN watch rss failed [{query[:40]}]: {e}")
                continue
            for it in items:
                if store.seen_url(it.url):
                    continue
                try:
                    f = fetch_article(it.url)
                except Exception:
                    continue
                if f.blocked_by_robots or not f.clean_text:
                    continue
                dup = store.near_duplicate(f.dedup_hash, district=district, state=state)
                if store.insert_article({
                        "url": f.url, "outlet_name": it.source_name,
                        "outlet_tier": tier_for(it.source_domain),
                        "language": it.language, "state": state, "district": district,
                        "published_at": f.published_at or it.published_at,
                        "raw_html": f.raw_html, "clean_text": f.clean_text,
                        "dedup_hash": f.dedup_hash,
                        "processing_status": "near_duplicate" if dup else "fetched",
                        }) is not None:
                    stats["new"] += 1
    return stats


# ---------------------------------------------------------------- matching
def run_matches() -> dict:
    stats = {"name_hits": 0, "geo_hits": 0}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""select id, name, name_variants, city, district from watch_site
                       where enabled""")
        sites = cur.fetchall()
        # name pass over recent articles
        cur.execute("""select id, clean_text, district from source_article
                       where created_at > now() - make_interval(days => %s)
                         and clean_text is not null""", (LOOKBACK_DAYS,))
        articles = cur.fetchall()
        for sid, name, variants, city, district in sites:
            vv = list(variants or []) + [name]
            for aid, text, adistrict in articles:
                if name_matches(text, adistrict, vv, city, district):
                    cur.execute("""insert into watch_hit (site_id, article_id, match_kind, note)
                                   values (%s,%s,'name',%s) on conflict do nothing
                                   returning id""",
                                (sid, aid, f"article mentions {name}"))
                    if cur.fetchone():
                        stats["name_hits"] += 1
        # geo pass over recent incidents
        cur.execute("""
          insert into watch_hit (site_id, incident_id, match_kind, note)
          select s.id, i.id, 'geo',
                 'incident within ' || round(ST_Distance(i.geom, s.geom)) || 'm of ' || s.name
          from watch_site s
          join incident i on i.geom is not null
            and i.created_at > now() - make_interval(days => %s)
            and ST_DWithin(i.geom, s.geom, %s)
          where s.enabled and s.geom is not null
          on conflict do nothing""", (LOOKBACK_DAYS, GEO_RADIUS_M))
        stats["geo_hits"] = cur.rowcount
        conn.commit()
    return stats


# ---------------------------------------------------------------- notification
def notify() -> dict:
    hook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
          select h.id, s.name, s.city, s.authority_status, s.authority, h.match_kind,
                 h.note, coalesce(a.url, ''), coalesce(i.narrative_summary, '')
          from watch_hit h join watch_site s on s.id = h.site_id
          left join source_article a on a.id = h.article_id
          left join incident i on i.id = h.incident_id
          where not h.notified order by h.id""")
        hits = cur.fetchall()
        if not hits:
            return {"pending": 0, "sent": 0}
        if not hook:
            print(f"  watch: {len(hits)} hit(s) pending — SLACK_WEBHOOK_URL not set, "
                  "shown on review console only")
            return {"pending": len(hits), "sent": 0}
        lines = ["🚨 *Site Watch — activity at audited junctions*"]
        for _, name, city, status, auth, kind, note, url, summ in hits[:10]:
            lines.append(f"• *{name}* ({city}) — {kind} match. Complaint status: "
                         f"_{status or '?'}_ ({auth or '?'})\n  {note}"
                         + (f"\n  {summ[:160]}" if summ else "")
                         + (f"\n  {url}" if url else ""))
        if len(hits) > 10:
            lines.append(f"…and {len(hits) - 10} more on the review console.")
        r = httpx.post(hook, json={"text": "\n".join(lines)}, timeout=20)
        if r.status_code // 100 == 2:
            cur.execute("update watch_hit set notified=true where id = any(%s)",
                        ([h[0] for h in hits],))
            conn.commit()
            return {"pending": len(hits), "sent": len(hits)}
        print(f"  WARN slack webhook returned {r.status_code}")
        return {"pending": len(hits), "sent": 0}


def run(store) -> dict:
    """Daily stage: collect -> match -> notify."""
    out = {"collect": collect_sites(store)}
    out["match"] = run_matches()
    out["notify"] = notify()
    return out
