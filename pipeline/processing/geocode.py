"""Geocoding (BUILD_SPEC §7.4) — most-specific-first with confidence + method.

Ladder (first hit wins, sanity-checked against India bbox and expected state):
  1. explicit coordinates in the text                    -> 0.95 coords_in_text
  2. full location_text_best                             -> 0.80 landmark_district
  3. progressively-trimmed location (drop leading parts) -> 0.70 road_city
  4. road_name + city/district + state                   -> 0.70 road_city
  5. city + district + state                             -> 0.50 city_centroid
  6. district + state centroid                           -> 0.40 district_centroid
Centroid-level results are low-confidence by design and never publish publicly (§8).

Providers: Mapbox (primary when MAPBOX_TOKEN is set — far stronger on Indic
village/tehsil text) falling back to Nominatim (free, 1 req/s etiquette), both behind
a persistent file cache. Mapbox cache keys are namespaced "mb|" so stale Nominatim
misses (cached None) never mask a Mapbox hit.
"""
import json
import re
import time
from pathlib import Path

import httpx

from pipeline.settings import MAPBOX_TOKEN, ROOT

NOMINATIM = "https://nominatim.openstreetmap.org/search"
MAPBOX = "https://api.mapbox.com/search/geocode/v6/forward"
UA = "CrashfreeIndia-DefectRepo/0.1 (road-safety research; contact: deepanshu@crashfreeindia.org)"
CACHE_PATH = ROOT / "data" / "geocode_cache.json"
INDIA_BBOX = (6.0, 68.0, 37.6, 97.5)   # lat_min, lon_min, lat_max, lon_max

_cache: dict | None = None
_last_call = 0.0

# common state-name variants for the sanity check
_STATE_ALIASES = {
    "odisha": {"odisha", "orissa"},
    "uttarakhand": {"uttarakhand", "uttaranchal"},
    "puducherry": {"puducherry", "pondicherry"},
}


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        _cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    return _cache


def _save_cache() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(_load_cache(), ensure_ascii=False))


def _nominatim(query: str) -> dict | None:
    cache = _load_cache()
    key = re.sub(r"\s+", " ", query.strip().lower())
    if key in cache:
        return cache[key]
    global _last_call
    wait = _last_call + 1.1 - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    try:
        r = httpx.get(NOMINATIM, params={
            "q": query, "format": "jsonv2", "countrycodes": "in",
            "limit": 1, "addressdetails": 1}, headers={"User-Agent": UA}, timeout=20)
        hits = r.json() if r.status_code == 200 else []
    except Exception:
        hits = []
    result = hits[0] if hits else None
    cache[key] = result
    _save_cache()
    return result


_last_mb_call = 0.0


def _mapbox(query: str) -> dict | None:
    """Normalized {lat, lon, state, display} via Mapbox forward geocoding, or None."""
    cache = _load_cache()
    key = "mb|" + re.sub(r"\s+", " ", query.strip().lower())
    if key in cache:
        return cache[key]
    global _last_mb_call
    wait = _last_mb_call + 0.12 - time.time()   # stay far under the 600/min limit
    if wait > 0:
        time.sleep(wait)
    _last_mb_call = time.time()
    result = None
    try:
        r = httpx.get(MAPBOX, params={
            "q": query, "country": "IN", "limit": 1, "language": "en",
            "access_token": MAPBOX_TOKEN}, timeout=20)
        feats = (r.json() or {}).get("features", []) if r.status_code == 200 else []
        if feats:
            f = feats[0]
            lon, lat = f["geometry"]["coordinates"]
            props = f.get("properties", {})
            result = {"lat": float(lat), "lon": float(lon),
                      "state": ((props.get("context", {}).get("region") or {})
                                .get("name") or ""),
                      "display": (props.get("full_address") or "")[:160]}
    except Exception:
        result = None
    cache[key] = result
    _save_cache()
    return result


def _resolve(query: str) -> dict | None:
    """Provider ladder: Mapbox first (if token), Nominatim fallback. Normalized shape."""
    if MAPBOX_TOKEN:
        hit = _mapbox(query)
        if hit:
            return hit
    hit = _nominatim(query)
    if hit:
        return {"lat": float(hit["lat"]), "lon": float(hit["lon"]),
                "state": (hit.get("address", {}).get("state") or ""),
                "display": (hit.get("display_name") or "")[:160]}
    return None


def _in_india(lat: float, lon: float) -> bool:
    a, b, c, d = INDIA_BBOX
    return a <= lat <= c and b <= lon <= d


def _state_ok(got_state: str, expected_state: str | None) -> bool:
    if not expected_state:
        return True
    got = (got_state or "").strip().lower()
    if not got:
        return True
    exp = expected_state.strip().lower()
    for canon, aliases in _STATE_ALIASES.items():
        if exp in aliases:
            exp = canon
        if got in aliases:
            got = canon
    return got == exp


_coord_re = re.compile(r"(\d{1,2}\.\d{3,})[,\s]+(\d{2,3}\.\d{3,})")


def _variants(loc: str, road: str | None, city: str | None,
              district: str | None, state: str | None) -> list[tuple[str, str, float]]:
    """[(query, method, confidence)] in descending specificity."""
    out: list[tuple[str, str, float]] = []
    loc = (loc or "").strip()
    tail0 = ", ".join(x for x in (district, state) if x)
    if loc:
        out.append((loc, "landmark_district", 0.80))
        parts = [p.strip() for p in re.split(r"[,;]", loc) if p.strip()]
        # the leading segment is usually the landmark/junction — try it with the
        # admin tail before falling back to bare road names (which hit long ways)
        if parts:
            lm = re.sub(r"^(near|close to|opposite|समीप)\s+", "", parts[0], flags=re.I).strip()
            if lm and tail0 and lm.lower() != (district or "").lower():
                out.append((f"{lm}, {tail0}", "landmark_district", 0.80))
            elif lm and state and not district:
                out.append((f"{lm}, {state}", "landmark_district", 0.60))
        for i in range(1, min(len(parts) - 1, 3)):
            out.append((", ".join(parts[i:]), "road_city", 0.70))
    tail = ", ".join(x for x in (district, state) if x)
    if road and tail:
        out.append((f"{road}, {tail}", "road_city", 0.70))
    if city and tail:
        out.append((f"{city}, {tail}", "city_centroid", 0.50))
    if district and state:
        out.append((f"{district} district, {state}", "district_centroid", 0.40))
    seen, uniq = set(), []
    for q, m, c in out:
        k = q.lower()
        if k not in seen:
            seen.add(k)
            uniq.append((q, m, c))
    return uniq


def geocode(location_text: str, road_name: str | None = None,
            admin_city: str | None = None, admin_district: str | None = None,
            admin_state: str | None = None) -> dict:
    """-> {lat, lon, geocode_confidence, geocode_method, display_name} or all-None."""
    m = _coord_re.search(location_text or "")
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if _in_india(lat, lon):
            return {"lat": lat, "lon": lon, "geocode_confidence": 0.95,
                    "geocode_method": "coords_in_text", "display_name": "coords in text"}
    for query, method, conf in _variants(location_text, road_name, admin_city,
                                         admin_district, admin_state):
        hit = _resolve(query)
        if not hit:
            continue
        lat, lon = hit["lat"], hit["lon"]
        if not _in_india(lat, lon):
            continue
        if not _state_ok(hit["state"], admin_state):
            continue           # homonym in another state — keep descending the ladder
        return {"lat": lat, "lon": lon, "geocode_confidence": conf,
                "geocode_method": method,
                "display_name": hit["display"]}
    return {"lat": None, "lon": None, "geocode_confidence": None,
            "geocode_method": None, "display_name": None}
