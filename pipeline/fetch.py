"""Article fetch + clean-text extraction + simhash (BUILD_SPEC §6).

- robots.txt respected (cached per domain), configurable per-domain delay
- Chrome UA (several Indian portals 403 default clients but serve browsers)
- trafilatura for clean text, with a JSON-LD articleBody fallback
  (Amar Ujala / Deccan Herald render body via JS but ship it in JSON-LD)
"""
import json
import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass

import httpx
import trafilatura
from simhash import Simhash

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_last_hit: dict[str, float] = {}


def _domain(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def robots_allowed(url: str) -> bool:
    dom = _domain(url)
    rp = _robots.get(dom, "unset")
    if rp == "unset":
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = httpx.get(f"https://{dom}/robots.txt", timeout=10,
                          headers={"User-Agent": UA}, follow_redirects=True)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp = None                      # unreachable robots -> default allow
        _robots[dom] = rp
    return True if rp is None else rp.can_fetch(UA, url)


def _rate_limit(url: str, delay_s: float) -> None:
    dom = _domain(url)
    wait = _last_hit.get(dom, 0) + delay_s - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_hit[dom] = time.time()


def _jsonld_body(html: str) -> str:
    best = ""
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                         html, re.S):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        stack = d if isinstance(d, list) else [d]
        flat = []
        for it in stack:
            if isinstance(it, dict) and isinstance(it.get("@graph"), list):
                flat += it["@graph"]
            else:
                flat.append(it)
        for it in flat:
            if isinstance(it, dict) and "NewsArticle" in str(it.get("@type", "")):
                ab = it.get("articleBody") or ""
                if len(ab) > len(best):
                    best = ab
    return best


@dataclass
class Fetched:
    url: str
    status: int
    raw_html: str
    clean_text: str
    dedup_hash: str        # 64-bit simhash, hex
    published_at: str | None
    title: str | None
    blocked_by_robots: bool = False


def simhash_hex(text: str) -> str:
    return format(Simhash(re.findall(r"\w+", text.lower())).value, "016x")


def hamming(h1: str, h2: str) -> int:
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def fetch_article(url: str, delay_s: float = 2.0, timeout: int = 30) -> Fetched:
    if not robots_allowed(url):
        return Fetched(url, 0, "", "", "", None, None, blocked_by_robots=True)
    _rate_limit(url, delay_s)
    r = httpx.get(url, timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": UA, "Accept-Language": "hi,en;q=0.8"})
    html = r.text or ""
    clean = trafilatura.extract(html, include_comments=False,
                                favor_precision=True) or ""
    ld = _jsonld_body(html)
    if len(ld) > len(clean):
        clean = ld
    meta = trafilatura.extract_metadata(html)
    return Fetched(
        url=str(r.url), status=r.status_code, raw_html=html, clean_text=clean.strip(),
        dedup_hash=simhash_hex(clean) if clean else "",
        published_at=(meta.date if meta else None),
        title=(meta.title if meta else None))
