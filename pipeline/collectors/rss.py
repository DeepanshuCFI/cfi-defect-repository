"""Google News RSS collector (BUILD_SPEC §6.1).

Query matrix: per district — the English starter query + primary-language crash terms.
RSS URL shape:
  https://news.google.com/rss/search?q=<query>+when:<N>d&hl=<hl>&gl=IN&ceid=IN:<lang>

Google News article links are redirectors; resolve_url() tries (a) the base64-encoded
publisher URL embedded in /rss/articles/ ids, (b) an HTTP redirect follow. Unresolved
links keep the google URL and are flagged so fetch can retry later.
"""
import base64
import json
import re
import urllib.parse
from dataclasses import dataclass

import feedparser
import httpx

from pipeline.settings import ROOT

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


# ------------------------------------------------------------------ URL resolution
# Resolving a Google News link costs a batchexecute round-trip, and Google throttles
# that hard partway through a large run. Measured 31 Jul 2026: deciles 1-4 of the daily
# run resolved 100%, decile 5 partially, deciles 6-10 resolved 0% — so ~800-1300
# articles/day were stored unresolved as 'new', never fetched and never retried
# (19,831 had piled up, 94% of that pile). Two things made it that bad:
#   1. collect() resolves EVERY feed item, before the caller's dup-URL check, so ~73%
#      of the ~5,100 daily round-trips re-resolved articles already in the DB. The disk
#      cache below makes those free, leaving the throttle budget for genuinely new ones.
#   2. the failure was swallowed silently by a bare `except Exception: pass` — no
#      counter, no log — so a run losing over half its collection still reported green.
# Failures are NEVER written to the disk cache: a cached miss would permanently poison
# the entry (the same trap geocode.py documents for stale Nominatim Nones).
CACHE_PATH = ROOT / "data" / "gnews_url_cache.json"
CACHE_MAX = 100_000          # Google News `when:Nd` bounds the working set to ~a week
SAVE_EVERY = 50
FAILURE_STREAK_TRIP = 25     # consecutive failures before we stop asking for this run

STATS = {"hit_disk": 0, "hit_mem": 0, "decoded_b64": 0, "ok": 0,
         "failed": 0, "skipped_throttled": 0}

_resolve_cache: dict[str, tuple[str, bool]] = {}
_disk: dict[str, str] | None = None
_pending_writes = 0
_fail_streak = 0
_throttled = False


def _load_disk() -> dict[str, str]:
    global _disk
    if _disk is None:
        try:
            _disk = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
        except Exception:                     # corrupt cache must never kill a run
            _disk = {}
    return _disk


def save_cache() -> None:
    global _disk, _pending_writes
    d = _load_disk()
    if len(d) > CACHE_MAX:                    # dicts keep insertion order -> drop oldest
        d = _disk = dict(list(d.items())[-CACHE_MAX:])
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(d, ensure_ascii=False))
    _pending_writes = 0


def reset_breaker() -> None:
    """Give the resolver a fresh chance. Worth doing once between stages — minutes pass
    between collect and the retry sweep, and the cost of being wrong is bounded by
    FAILURE_STREAK_TRIP seconds before the breaker trips again."""
    global _fail_streak, _throttled
    _fail_streak, _throttled = 0, False


def reset_run_state() -> None:
    """Clear per-run counters, caches and the breaker (the disk cache survives)."""
    reset_breaker()
    _resolve_cache.clear()
    for k in list(STATS):
        STATS[k] = 0 if isinstance(STATS[k], int) else STATS[k]


def throttled() -> bool:
    return _throttled


def resolve_url(google_url: str, client: httpx.Client | None = None) -> tuple[str, bool]:
    global _fail_streak, _throttled
    if google_url in _resolve_cache:          # same article surfaces in several queries
        STATS["hit_mem"] += 1
        return _resolve_cache[google_url]
    hit = _load_disk().get(google_url)        # resolved on an earlier day — free
    if hit:
        STATS["hit_disk"] += 1
        _resolve_cache[google_url] = (hit, True)
        return hit, True
    # Old-format ids sometimes carry the URL in base64 — free, try before the network.
    dec = _decode_gnews_id(google_url)
    if dec and "news.google.com" not in dec:
        STATS["decoded_b64"] += 1
        _remember(google_url, dec)
        return dec, True
    if _throttled:
        # Google is already refusing us; every further call burns ~1s to fail. Leave
        # the article unresolved — cmd_retry_unresolved picks it up on a later run.
        STATS["skipped_throttled"] += 1
        return google_url, False
    # Current format (AU_yq… ids) needs Google's batchexecute round-trip.
    out = (google_url, False)
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(google_url, interval=1)
        if r.get("status") and r.get("decoded_url"):
            out = (r["decoded_url"], True)
    except Exception as e:
        STATS.setdefault("last_error", str(e)[:200])
    if out[1]:
        STATS["ok"] += 1
        _fail_streak = 0
        _remember(google_url, out[0])
    else:
        STATS["failed"] += 1
        _fail_streak += 1
        if _fail_streak >= FAILURE_STREAK_TRIP:
            _throttled = True
            print(f"  RESOLVER THROTTLED after {_fail_streak} consecutive failures — "
                  f"skipping resolution for the rest of this run")
        _resolve_cache[google_url] = out      # in-process only; never persisted
    return out


def _remember(google_url: str, publisher_url: str) -> None:
    """Record a SUCCESSFUL resolution in both caches."""
    global _pending_writes
    _resolve_cache[google_url] = (publisher_url, True)
    _load_disk()[google_url] = publisher_url
    _pending_writes += 1
    if _pending_writes >= SAVE_EVERY:
        save_cache()


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
