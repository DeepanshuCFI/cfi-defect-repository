"""Geocoding (BUILD_SPEC §7.4) — most-specific-first with confidence + method.

Ladder (first hit wins, sanity-checked against India bbox and expected state):
  1. explicit coordinates in the text                    -> 0.95 coords_in_text
  2. full location_text_best                             -> 0.80 landmark_district
  3. progressively-trimmed location (drop leading parts) -> 0.70 road_city
  4. road_name + city/district + state                   -> 0.70 road_city
  5. city + district + state                             -> 0.50 city_centroid
  6. district + state centroid                           -> 0.40 district_centroid
Centroid-level results are low-confidence by design and never publish publicly (§8).

NAME EVIDENCE (16 Nov 2026). Position on the ladder is no longer what earns a pin. A
provider that does not know a village road answers anyway — with a street called
"Road" in the district town — and state and district both match. Every candidate now
has to share a distinctive name with the stated location (placematch.evidence), and
what matched decides both the precision class and the confidence:
  spot 0.90 · road 0.80 · locality 0.75 · town 0.65 · highway-only or nothing: no pin

Providers: Mapbox (primary when MAPBOX_TOKEN is set — far stronger on Indic
village/tehsil text) falling back to Nominatim (free, 1 req/s etiquette), both behind
a persistent file cache. Mapbox cache keys are namespaced "mb|" so stale Nominatim
misses (cached None) never mask a Mapbox hit.
"""
import json
import re
import time
from pathlib import Path

from functools import lru_cache

import httpx

from pipeline.processing import placematch
from pipeline.settings import MAPBOX_TOKEN, ROOT

NOMINATIM = "https://nominatim.openstreetmap.org/search"
MAPBOX = "https://api.mapbox.com/search/geocode/v6/forward"

# Fail-closed caps: a validity check that can't run must LOWER confidence, never
# silently pass (guard-audit 2026-07-18; the Hathras->Odisha lesson).
UNANCHORED_MAX_CONF = 0.5     # no state AND no district known -> state guard can't run
STATELESS_HIT_MAX_CONF = 0.55  # hit carries no state metadata -> guard ran on nothing
WIDE_AREA_KM = 6.0             # hit spans a huge feature (long road / big polygon):
WIDE_AREA_MAX_CONF = 0.55      # a pin somewhere on 47km of Outer Ring Road is not 0.7

# Confidence follows what the name evidence supports, not which ladder rung answered.
# All four clear the 0.6 publish bar: a report pinned to the right town is publishable
# AS a town-level report — the class travels with it (geocode_precision) so the map
# can draw it as an area and never as a dot. Every value here must be allowed by the
# column's CHECK constraint (migration 018); a test holds the two together.
PRECISION_CONF = {"spot": 0.90, "road": 0.80, "locality": 0.75, "town": 0.65}
MAPBOX_CANDIDATES = 5          # the first answer is often the generic one; look further

# Every geocode_qualifier this module can write. The column has a CHECK constraint, so
# this set and the constraint must stay in step — a term added here without a migration
# aborts the whole geocode batch on the next run, and does so every run after. Declared
# rather than left implicit in the branches because that has now shipped twice: once on
# geocode_method (18 Jul, caught by the audit), once on this column (1 Aug, caught only
# because a repair happened to write the column by hand the same day).
QUALIFIERS = frozenset({"unanchored", "stateless_hit", "districtless_hit", "wide_area"})
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

# Districts India has renamed. Without these a district guard REJECTS correct pins:
# measured 1 Aug 2026, 1 of 3 sampled mismatches was Chhatrapati Sambhajinagar vs
# Aurangabad — the same place under both names. Geocoders lag official renames by
# years, and the article text may use either.
_DISTRICT_ALIASES = {
    "chhatrapati sambhajinagar": {"chhatrapati sambhajinagar", "aurangabad"},
    "dharashiv": {"dharashiv", "osmanabad"},
    "prayagraj": {"prayagraj", "allahabad"},
    "ayodhya": {"ayodhya", "faizabad"},
    "gurugram": {"gurugram", "gurgaon"},
    "bengaluru urban": {"bengaluru urban", "bangalore urban", "bengaluru", "bangalore"},
    "mysuru": {"mysuru", "mysore"},
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

# Every field _mapbox writes. Unlike _nominatim — which caches the RAW provider response
# and lets _resolve derive fields on each read — this function caches the NORMALIZED
# dict, so a cached entry is frozen in the shape the code had when it was written.
_MB_FIELDS = ("lat", "lon", "state", "district", "display", "span_km",
              # what the feature IS — read by placematch.evidence. Until 16 Nov 2026
              # these were thrown away, which is why a street named "Road" could not
              # be told from the road in the article.
              "name", "ftype", "ctx",
              # the provider's further candidates, same shape minus this key
              "alts")


def _mb_cache_usable(entry) -> bool:
    """Whether a cached Mapbox entry still has everything the guards read.

    'district' only started being written by 5db87ee (1 Aug), which landed after that
    day's scheduled run — so every entry in the CI geocode cache predates it and comes
    back with no district key. _district_ok would then see "" and cap the pin at 0.55,
    below the publish gate: the district guard would look over-strict when the real fault
    is a stale cache. Re-resolving a short-shaped entry overwrites it, so this heals
    itself in one run.

    Checked as whole-shape rather than 'district' alone because this is the THIRD time
    the module has been bitten by a cache outliving the code that filled it (the 'mb|'
    namespace exists for the same reason, and repair_district_pins.py works around it
    with a throwaway cache). A future field added to the normalizer invalidates older
    entries automatically.

    Two things deliberately stay usable: a cached None, which is a real 'Mapbox found
    nothing', and "district": "", which is a real 'provider returned no district' — the
    case districtless_hit exists to handle. Re-fetching either would be a lie about what
    the provider said.
    """
    return entry is None or all(k in entry for k in _MB_FIELDS)


def _mb_normalize(f: dict) -> dict:
    """One Mapbox v6 feature in the shape the guards read (every _MB_FIELDS key but
    'alts')."""
    lon, lat = f["geometry"]["coordinates"]
    props = f.get("properties", {})
    ctx = props.get("context", {}) or {}
    bbox = props.get("bbox") or f.get("bbox")
    return {"lat": float(lat), "lon": float(lon),
            "state": (ctx.get("region") or {}).get("name") or "",
            "district": ((ctx.get("district") or {}).get("name")
                         or (ctx.get("place") or {}).get("name") or ""),
            "display": (props.get("full_address") or "")[:160],
            "span_km": _bbox_span_km(bbox[1], bbox[0], bbox[3], bbox[2]) if bbox else 0.0,
            "name": props.get("name") or "",
            "ftype": props.get("feature_type") or "",
            "ctx": {k: (ctx.get(k) or {}).get("name") or ""
                    for k in ("street", "neighborhood", "locality", "place")}}


def _mapbox(query: str) -> dict | None:
    """The top Mapbox candidate, normalized, with the rest under 'alts' — or None."""
    cache = _load_cache()
    key = "mb|" + re.sub(r"\s+", " ", query.strip().lower())
    if key in cache and _mb_cache_usable(cache[key]):
        return cache[key]
    global _last_mb_call
    wait = _last_mb_call + 0.12 - time.time()   # stay far under the 600/min limit
    if wait > 0:
        time.sleep(wait)
    _last_mb_call = time.time()
    result = None
    try:
        r = httpx.get(MAPBOX, params={
            "q": query, "country": "IN", "limit": MAPBOX_CANDIDATES, "language": "en",
            "access_token": MAPBOX_TOKEN}, timeout=20)
        feats = (r.json() or {}).get("features", []) if r.status_code == 200 else []
        if feats:
            hits = [_mb_normalize(f) for f in feats]
            result = {**hits[0], "alts": hits[1:]}
    except Exception:
        result = None
    cache[key] = result
    _save_cache()
    return result


def _bbox_span_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Rough diagonal of a bounding box in km (1 deg ~ 111 km; fine for a threshold)."""
    return ((lat2 - lat1) ** 2 + (lon2 - lon1) ** 2) ** 0.5 * 111.0


_NOMINATIM_FTYPE = {"road": "street", "village": "locality", "hamlet": "locality",
                    "suburb": "neighborhood", "neighbourhood": "neighborhood",
                    "quarter": "neighborhood", "town": "place", "city": "place",
                    "municipality": "place"}


def _resolve(query: str) -> dict | None:
    """Provider ladder: Mapbox first (if token), Nominatim fallback. Normalized shape."""
    if MAPBOX_TOKEN:
        hit = _mapbox(query)
        if hit:
            return hit
    hit = _nominatim(query)
    if hit:
        bb = hit.get("boundingbox")
        addr = hit.get("address", {}) or {}
        return {"lat": float(hit["lat"]), "lon": float(hit["lon"]),
                "state": (addr.get("state") or ""),
                "district": (addr.get("state_district") or addr.get("county")
                             or addr.get("city_district") or ""),
                "display": (hit.get("display_name") or "")[:160],
                "span_km": _bbox_span_km(float(bb[0]), float(bb[2]),
                                         float(bb[1]), float(bb[3])) if bb else 0.0,
                "name": hit.get("name") or "",
                "ftype": _NOMINATIM_FTYPE.get(hit.get("addresstype") or "", ""),
                "ctx": {"street": addr.get("road") or "",
                        "neighborhood": addr.get("suburb") or addr.get("neighbourhood") or "",
                        "locality": addr.get("village") or addr.get("hamlet") or "",
                        "place": addr.get("town") or addr.get("city") or ""},
                "alts": []}
    return None


def _candidates(query: str) -> list[dict]:
    """Every candidate the provider offered for a query, best first."""
    hit = _resolve(query)
    return [] if not hit else [hit, *(hit.get("alts") or [])]


@lru_cache(maxsize=1)
def _district_towns() -> frozenset:
    """Name words of every district in the config spine. A road named after one of
    them ('Sitapur Road') is a direction, not a location — placematch marks it weak."""
    from pipeline import configload
    out: set[str] = set()
    for row in configload.districts():
        out |= placematch.name_tokens(row.get("district"))
    return frozenset(out)


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


def _district_ok(got_district: str, expected_district: str | None) -> bool:
    """The other half of the 18 Jul anchoring fix.

    _state_ok stopped a Hathras crash being pinned in Odisha. It cannot stop a
    Gopalganj crash being pinned in Nawada — both are Bihar. Measured 1 Aug 2026:
    querying 'NH-531, Gopalganj, Bihar' returns a road in NAWADA, the state matches,
    0.80 confidence sails past the 0.6 gate, and DBSCAN then correctly clusters three
    such pins from three districts into one phantom hotspot. 34 of 35 escalation
    candidates — the flag that surfaces a location for government action — were built
    that way.

    Unknown on either side returns True: this guard exists to catch a CONFIDENT
    mismatch, and a missing value is handled by the confidence cap in geocode(), not
    by rejecting the article."""
    if not expected_district:
        return True
    got = (got_district or "").strip().lower()
    if not got:
        return True
    exp = expected_district.strip().lower()
    for s_ in (" district", " dist.", " dist"):
        got = got.removesuffix(s_)
        exp = exp.removesuffix(s_)
    if got == exp:
        return True
    for canon, aliases in _DISTRICT_ALIASES.items():
        if exp in aliases and got in aliases:
            return True
    return False


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


NO_PIN = {"lat": None, "lon": None, "geocode_confidence": None, "geocode_method": None,
          "geocode_qualifier": None, "display_name": None,
          "geocode_precision": None, "geocode_match": None}


def geocode(location_text: str, road_name: str | None = None,
            admin_city: str | None = None, admin_district: str | None = None,
            admin_state: str | None = None) -> dict:
    """-> {lat, lon, geocode_confidence, geocode_method, geocode_qualifier, display_name,
           geocode_precision, geocode_match}; lat/lon None when nothing can be pinned.

    geocode_precision is what the name evidence supports (placematch.PRECISIONS).
    'highway' comes back WITHOUT a pin: only a numbered highway matched, and a point
    somewhere on NH-27 is not a location."""
    m = _coord_re.search(location_text or "")
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if _in_india(lat, lon):
            return {"lat": lat, "lon": lon, "geocode_confidence": 0.95,
                    "geocode_method": "coords_in_text", "geocode_qualifier": None,
                    "display_name": "coords in text",
                    "geocode_precision": "spot", "geocode_match": "coordinates in text"}
    # Anchored = we know a state or district, so _state_ok() can actually validate the
    # hit. Unanchored results are capped below the publish bar (fail closed).
    anchored = bool((admin_state or "").strip() or (admin_district or "").strip())
    stated = placematch.stated_names(location_text, road_name, admin_city,
                                     admin_district, admin_state, _district_towns())
    highway_only = None
    for query, method, _rung_conf in _variants(location_text, road_name, admin_city,
                                               admin_district, admin_state):
        for hit in _candidates(query):
            lat, lon = hit["lat"], hit["lon"]
            if not _in_india(lat, lon):
                continue
            if not _state_ok(hit["state"], admin_state):
                continue       # homonym in another state — keep looking
            if not _district_ok(hit.get("district", ""), admin_district):
                continue       # right state, WRONG district — keep looking
            precision, word = placematch.evidence(stated, hit)
            if precision is None:
                continue       # right district, but nothing says it is the right PLACE
            if precision == "highway":
                # remember it, keep descending: a later rung may place the village
                highway_only = highway_only or (hit.get("name") or word)
                continue
            conf = PRECISION_CONF[precision]
            # NOTE: geocode_method is CHECK-constrained to a controlled vocabulary — the
            # guard signal goes in geocode_qualifier, never as a method suffix (that bug
            # would have failed every geocode UPDATE; migration 012). geocode_qualifier
            # and geocode_precision are CHECK-constrained too: QUALIFIERS and
            # placematch.PRECISIONS are each held against their migration by a test.
            # Adding a term without the matching migration is what shipped on 1 Aug and
            # would have aborted the geocode batch of every run from 2 Aug on.
            qualifier = None
            if not anchored:
                conf, qualifier = min(conf, UNANCHORED_MAX_CONF), "unanchored"
            elif not (hit["state"] or "").strip():
                # the state guard "passed" only because the hit carried no state
                # metadata — unverified is not verified; cap below the publish bar
                conf, qualifier = min(conf, STATELESS_HIT_MAX_CONF), "stateless_hit"
            elif admin_district and not (hit.get("district") or "").strip():
                # we knew the district but the provider returned none, so _district_ok
                # could not run. Same principle as stateless_hit: unverified != verified.
                conf, qualifier = min(conf, STATELESS_HIT_MAX_CONF), "districtless_hit"
            if precision in ("spot", "road") and hit.get("span_km", 0.0) > WIDE_AREA_KM:
                # pin is an arbitrary point on a huge feature (e.g. a 47km ring road).
                # Not applied to locality/town: those classes already say "an area".
                conf, qualifier = min(conf, WIDE_AREA_MAX_CONF), qualifier or "wide_area"
            return {"lat": lat, "lon": lon, "geocode_confidence": conf,
                    "geocode_method": method, "geocode_qualifier": qualifier,
                    "display_name": hit["display"],
                    "geocode_precision": precision,
                    "geocode_match": (placematch.pinned_name(stated, hit, precision)
                                      or word or "")[:120]}
    if highway_only:
        return {**NO_PIN, "geocode_precision": "highway", "geocode_match": highway_only[:120]}
    return dict(NO_PIN)
