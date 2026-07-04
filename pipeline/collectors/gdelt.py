"""GDELT 2.0 DOC API collector (BUILD_SPEC §6.2) — wide net over Indian road-crash news.

API: https://api.gdeltproject.org/api/v2/doc/doc
We query per language (GDELT sourcelang) with crash keywords, restricted to India.
Free, no key; be polite (one query per language per run).
"""
import time
from dataclasses import dataclass

import httpx

API = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT sourcelang codes for our languages (subset GDELT actually indexes well)
GDELT_LANG = {"en": "english", "hi": "hindi", "mr": "marathi", "ta": "tamil",
              "te": "telugu", "bn": "bengali", "kn": "kannada", "gu": "gujarati",
              "ml": "malayalam", "pa": "punjabi", "ur": "urdu"}

QUERY_BY_LANG = {
    "en": '("road accident" OR "road crash" OR "hit and run")',
    "hi": '("सड़क हादसा" OR "सड़क दुर्घटना")',
    "mr": '("अपघात")',
    "ta": '("சாலை விபத்து")',
    "te": '("రోడ్డు ప్రమాదం")',
    "bn": '("সড়ক দুর্ঘটনা")',
    "kn": '("ರಸ್ತೆ ಅಪಘಾತ")',
    "gu": '("માર્ગ અકસ્માત")',
    "ml": '("വാഹനാപകടം")',
    "pa": '("ਸੜਕ ਹਾਦਸਾ")',
    "ur": '("سڑک حادثہ")',
}


@dataclass
class GdeltItem:
    url: str
    title: str
    published_at: str | None   # seendate
    source_domain: str | None
    language: str


def collect(lang: str = "en", timespan: str = "1d", max_records: int = 250,
            retries: int = 3) -> list[GdeltItem]:
    if lang not in QUERY_BY_LANG:
        return []
    q = f"{QUERY_BY_LANG[lang]} sourcecountry:IN sourcelang:{GDELT_LANG[lang]}"
    params = {"query": q, "mode": "ArtList", "format": "json",
              "maxrecords": str(max_records), "timespan": timespan, "sort": "DateDesc"}
    r = None
    for attempt in range(retries):
        r = httpx.get(API, params=params, timeout=45,
                      headers={"User-Agent": "CrashfreeIndia-DefectRepo/0.1 (research)"})
        if r.status_code == 429:               # GDELT free tier throttles hard
            wait = 10 * (attempt + 1)
            print(f"  GDELT 429, backing off {wait}s…")
            time.sleep(wait)
            continue
        break
    if r is None or r.status_code != 200:
        print(f"  WARN GDELT [{lang}] gave {r.status_code if r else '?'} — skipping this run")
        return []
    try:
        data = r.json()
    except ValueError:            # GDELT returns HTML on malformed queries
        return []
    out = []
    for a in data.get("articles", []):
        out.append(GdeltItem(
            url=a.get("url", ""), title=a.get("title", ""),
            published_at=a.get("seendate"), source_domain=a.get("domain"),
            language=lang))
    return out
