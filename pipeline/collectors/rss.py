"""Google News RSS collector (BUILD_SPEC §6.1).

Query matrix: per district — the English starter query + primary-language crash terms.
RSS URL shape:
  https://news.google.com/rss/search?q=<query>+when:<N>d&hl=<hl>&gl=IN&ceid=IN:<lang>

Google News article links are redirectors; resolve_url() tries (a) the base64-encoded
publisher URL embedded in /rss/articles/ ids, (b) an HTTP redirect follow. Unresolved
links keep the google URL and are flagged so fetch can retry later.
"""
import base64
import re
import urllib.parse
from dataclasses import dataclass

import feedparser
import httpx

HL = {"en": "en-IN", "hi": "hi-IN", "mr": "mr-IN", "ta": "ta-IN", "te": "te-IN",
      "bn": "bn-IN", "kn": "kn-IN", "gu": "gu-IN", "ml": "ml-IN", "pa": "pa-IN",
      "or": "or-IN", "as": "as-IN", "ur": "ur-IN"}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


@dataclass
class FeedItem:
    url: str            # resolved publisher URL (or google URL if unresolved)
    google_url: str
    title: str
    published_at: str | None
    source_name: str | None
    source_domain: str | None
    query: str
    language: str
    resolved: bool


def build_queries(district_row: dict, kw: dict[str, dict[str, list[str]]],
                  max_lang_terms: int = 3) -> list[tuple[str, str]]:
    """[(query, lang)] — English starter + top primary-language crash terms."""
    q: list[tuple[str, str]] = []
    starter = district_row["starter_query_en"].strip()
    if starter:
        q.append((starter, "en"))
    lang = district_row["primary_language"]
    name = district_row["query_name"]
    if lang != "en":
        for term in kw.get(lang, {}).get("crash", [])[:max_lang_terms]:
            q.append((f'"{name}" {term}', lang))
    return q


def rss_url(query: str, lang: str, days: int = 7) -> str:
    qq = urllib.parse.quote(f"{query} when:{days}d")
    hl = HL.get(lang, "en-IN")
    return f"https://news.google.com/rss/search?q={qq}&hl={hl}&gl=IN&ceid=IN:{lang}"


def _decode_gnews_id(url: str) -> str | None:
    """Publisher URL is often embedded base64 in /rss/articles/<id>."""
    m = re.search(r"/(?:rss/)?articles/([^?/]+)", url)
    if not m:
        return None
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return None
    # URL appears as a length-prefixed string inside the protobuf; grab http(s)://…
    m2 = re.search(rb"https?://[\x20-\x7e]+", raw)
    if not m2:
        return None
    cand = m2.group(0).decode("ascii", "ignore")
    # trim trailing protobuf noise
    cand = re.split(r"[\x00-\x1f\xd2\xc2]", cand)[0].rstrip("\\")
    return cand if re.match(r"https?://[\w.-]+\.\w{2,}/", cand + "/") else None


def resolve_url(google_url: str, client: httpx.Client | None = None) -> tuple[str, bool]:
    # Old-format ids sometimes carry the URL in base64 — cheap, try first.
    dec = _decode_gnews_id(google_url)
    if dec and "news.google.com" not in dec:
        return dec, True
    # Current format (AU_yq… ids) needs Google's batchexecute round-trip.
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(google_url, interval=1)
        if r.get("status") and r.get("decoded_url"):
            return r["decoded_url"], True
    except Exception:
        pass
    return google_url, False


def collect(query: str, lang: str, days: int = 7, max_items: int = 30,
            resolve: bool = True) -> list[FeedItem]:
    # Google blocks feedparser's own fetcher — fetch with a browser UA, parse the text.
    r = httpx.get(rss_url(query, lang, days), timeout=20, follow_redirects=True,
                  headers={"User-Agent": UA})
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    items: list[FeedItem] = []
    client = httpx.Client(follow_redirects=True, timeout=15,
                          headers={"User-Agent": UA}) if resolve else None
    try:
        for e in feed.entries[:max_items]:
            gurl = e.get("link", "")
            src = e.get("source", {}) or {}
            url, ok = resolve_url(gurl, client) if resolve else (gurl, False)
            items.append(FeedItem(
                url=url, google_url=gurl, title=e.get("title", ""),
                published_at=e.get("published"),
                source_name=src.get("title"),
                source_domain=urllib.parse.urlparse(src.get("href", "")).netloc or None,
                query=query, language=lang, resolved=ok))
    finally:
        if client is not None:
            client.close()
    return items
