"""Read districts/keywords/outlets — from the DB when DATABASE_URL is live, else
directly from config/ files. Same shapes either way, so collectors don't care."""
import csv
import json
from functools import lru_cache

from pipeline.settings import CONFIG_DIR, DATABASE_URL


def _db_available() -> bool:
    return bool(DATABASE_URL) and "REPLACE_ME" not in DATABASE_URL


@lru_cache(maxsize=1)
def districts() -> list[dict]:
    if _db_available():
        from pipeline.db import connect
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""select district, state, primary_language, query_name, starter_query_en
                           from config_district where enabled order by state, district""")
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    return list(csv.DictReader(open(CONFIG_DIR / "config_districts_master.csv")))


@lru_cache(maxsize=1)
def keywords() -> dict[str, dict[str, list[str]]]:
    """{lang: {category: [terms]}}"""
    if _db_available():
        from pipeline.db import connect
        out: dict = {}
        with connect() as conn, conn.cursor() as cur:
            cur.execute("select language, category, term from config_keyword where enabled")
            for lang, cat, term in cur.fetchall():
                out.setdefault(lang, {}).setdefault(cat, []).append(term)
        return out
    pack = json.load(open(CONFIG_DIR / "config_keyword_pack.json"))["languages"]
    return {lang: {c: obj.get(c, []) for c in
                   ("crash", "fatality", "injury", "crash_type", "infra_defect")}
            for lang, obj in pack.items()}


@lru_cache(maxsize=1)
def settings() -> dict:
    if _db_available():
        from pipeline.db import connect
        with connect() as conn, conn.cursor() as cur:
            cur.execute("select key, value from config_setting")
            return dict(cur.fetchall())
    return json.load(open(CONFIG_DIR / "config_settings.json"))["settings"]


@lru_cache(maxsize=1)
def outlet_tiers() -> dict[str, str]:
    """domain -> tier, from config_outlet (for tagging outlet_tier at ingestion)."""
    rows: list[dict]
    if _db_available():
        from pipeline.db import connect
        with connect() as conn, conn.cursor() as cur:
            cur.execute("select website, tier from config_outlet where enabled")
            rows = [{"website": w, "tier": t} for w, t in cur.fetchall()]
    else:
        rows = list(csv.DictReader(open(CONFIG_DIR / "config_regional_outlets.csv")))
    out = {}
    for r in rows:
        dom = (r.get("website") or "").strip().lower().removeprefix("https://").removeprefix("http://")
        dom = dom.removeprefix("www.").split("/")[0]
        if dom:
            out[dom] = r["tier"]
    return out


# Big-national domains not in the regional outlet seed list — tier them national at ingestion.
NATIONAL_DOMAINS = {
    "timesofindia.indiatimes.com", "hindustantimes.com", "thehindu.com",
    "indianexpress.com", "ndtv.com", "indiatoday.in", "news18.com",
    "deccanherald.com", "telegraphindia.com", "tribuneindia.com",
    "newindianexpress.com", "livemint.com", "business-standard.com",
    "aajtak.in", "abplive.com", "zeenews.india.com", "indiatvnews.com",
}
