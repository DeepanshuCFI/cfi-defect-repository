"""Does a geocoder's answer actually name the place the article names? (BUILD_SPEC §7.4)

The geocoder is asked for free text such as "Aureni-Mahapur road, Hardoi district,
Uttar Pradesh". When the provider does not know the village or the road, it does not
say so: it answers with the closest thing its index has to the WORDS, which is very
often a street in the district town whose name is literally "Road", "Main Road",
"Near" or "Chauraha". State and district both match, so every existing guard passes
and the pin is published at 0.7 or 0.8 confidence.

Measured 14 Nov 2026: in a blind sample of 40 public reports, 26 pins sat more than
10 km from the place the article names (worst 94 km); 4,009 of 7,783 public reports
sat on a map point shared by three or more different place descriptions; one point in
Hardoi held 78 reports naming 74 different roads.

The check has no model in it. A hit is accepted only on NAME EVIDENCE, and what
matched says how precise the pin is, which is what the map must show:

    spot     coordinates in the text, or an address whose road name matches
    road     a street whose own name matches the road the article names
    area     the hit is, or lies inside, a named village / locality / town that matches
    highway  only a numbered highway matched: the right road, an arbitrary point on it

Three traps found by replaying the hand-audited pins (16 Nov 2026), each of which a
plain "shares a word" rule walks into:

  * "Sitapur Rd" exists in every town within 100 km of Sitapur. A road named after
    another district's town is a direction, not a location: such a word is WEAK and
    needs a second piece of evidence (the hit lies in a place the article also names).
  * "Jawahar Nagar", "Gandhi Road", "Saini Colony" exist in every town. Same rule.
  * A street's CONTEXT town is not evidence for the street: Mapbox files village
    streets 15 km out under the district town. A hit whose only match is its
    context `place` is rejected, so the ladder falls through to the town itself.

Everything in this module is pure.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from typing import NamedTuple

# Words that describe a kind of place or a direction rather than naming one. A hit
# that shares only these with the stated location shares nothing: "Road" matches
# every road in India.
GENERIC = frozenset("""
road rd roads marg path street st lane gali galli highway expressway bypass byepass
service link main mains new old upper lower north south east west central
national state district dist tehsil tahsil taluk taluka block mandal subdivision
village villages gram gaon gaun panchayat city town township area areas locality zone
ward sector phase colony nagar vihar puram mohalla mohallah basti tola
chowk chauk chauraha chouraha tiraha crossing crossroad crossroads intersection junction
circle square gate bridge overbridge flyover underpass pul puliya culvert canal nahar
river nadi nala nallah drain pond talab ghat dam
station railway rail bus stand depot airport hospital school college university
temple mandir masjid mosque church gurudwara police thana kotwali chowki court office
market mandi bazar bazaar mall petrol pump toll plaza hotel dhaba factory mill
near nearby opposite front behind beside side between towards toward from via and the
of in at on under over along around inside outside stretch section portion part
km kilometre kilometer kilometres kilometers metre meter number no india indian
kalan khurd buzurg bujurg khas dehat
""".split())

# Names that recur in every town: leaders, deities, castes, virtues. Real names, but
# one of them alone cannot tell two towns apart.
COMMON_NAMES = frozenset("""
jawahar gandhi nehru subhash ambedkar indira rajiv shastri patel azad tilak shivaji
civil lines model krishna shiv shiva durga hanuman kali ganesh laxmi lakshmi saraswati
baba mata devi ganga yamuna shanti prem vijay adarsh vikas naya nayi purana purani
chhota chota bada bara saini yadav gupta sharma verma singh khan ansari pandit thakur
rajput gurjar kumar sita radha mohan gopal hari shri sant guru maharaj maharana pratap
rani raja kisan sarvodaya sadar cantt cantonment sarai kothi purwa tanda
""".split())

# A segment of the stated location that names a ROAD rather than a place.
_ROAD_WORD = re.compile(
    r"\b(road|rd|marg|path|street|lane|gali|galli|highway|expressway|bypass|byepass|"
    r"link|nh|sh|mdr|n\.h\.|s\.h\.)\b", re.I)
_HIGHWAY = re.compile(
    r"\b(nh|n\.h\.|national\s+highway|sh|s\.h\.|state\s+highway|mdr)[\s\-–./]*"
    r"(\d{1,4}[a-z]?)\b", re.I)
_HW_KIND = {"nh": "nh", "n.h.": "nh", "national highway": "nh",
            "sh": "sh", "s.h.": "sh", "state highway": "sh", "mdr": "mdr"}
# "NH-31 near Bakhri" is a road AND a place: split on the words that join them.
_SEGMENT = re.compile(r"[,;()]|\b(?:near|nearby|opposite|in front of|behind|beside|"
                      r"towards|toward|adjacent to|close to)\b")
_WORD = re.compile(r"[a-z0-9]+")
MIN_TOKEN = 4          # shorter words (ram, pur, new) match too much to be evidence
SIMILAR_AT = 0.84      # sahpur ~ shahpur ~ shahapur; bilgram ~ bilgraam
AREA_FTYPES = frozenset({"place", "locality", "neighborhood"})
STREET_FTYPES = frozenset({"street", "address"})


class Stated(NamedTuple):
    """What the article's location names, split by what kind of thing each word names."""
    road: frozenset      # words of the road's own name
    place: frozenset     # words naming a village, locality, block or town
    weak: frozenset      # members of either set that cannot stand alone as evidence

    def __bool__(self) -> bool:
        return bool(self.road or self.place)


def _plain(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def highway_refs(text: str | None) -> set[str]:
    """{'nh31', 'sh77'} — numbered highways, however they were written."""
    out = set()
    for kind, num in _HIGHWAY.findall(_plain(text)):
        out.add(_HW_KIND[re.sub(r"\s+", " ", kind.strip())] + num)
    return out


def _is_highway_ref(tok: str) -> bool:
    return bool(re.fullmatch(r"(nh|sh|mdr)\d{1,4}[a-z]?", tok))


def name_tokens(text: str | None) -> set[str]:
    """The words of a name that could identify a place: lower-case, unaccented,
    generic words and short words dropped, numbered highways folded to one token."""
    plain = _plain(text)
    out = highway_refs(plain)
    plain = _HIGHWAY.sub(" ", plain)
    for w in _WORD.findall(plain):
        if len(w) >= MIN_TOKEN and w not in GENERIC and not w.isdigit():
            out.add(w)
    return out


def similar(a: str, b: str) -> bool:
    """Same name allowing for transliteration. Highway numbers must match exactly."""
    if a == b:
        return True
    if _is_highway_ref(a) or _is_highway_ref(b):
        return False
    if a[0] != b[0] or min(len(a), len(b)) < 5:
        return False
    return SequenceMatcher(None, a, b).ratio() >= SIMILAR_AT


def stated_names(location: str | None, road: str | None, city: str | None,
                 district: str | None, state: str | None,
                 other_towns: frozenset = frozenset()) -> Stated:
    """Split the stated location into road words and place words.

    The state's name is dropped: it is true of every place in the state. So is the
    district's, unless the report is explicitly about the district town itself
    (admin_city says so): 'Station Road, Hardoi city' is evidence for Hardoi town;
    'Arwal village, Hardoi district' is not.

    other_towns: normalized names of district towns nationwide. One of them inside
    a road's name ('Sitapur Road') is a direction and is marked weak."""
    road_t, place_t = set(name_tokens(road)), set(name_tokens(city))
    for seg in _SEGMENT.split(_plain(location)):
        (road_t if _ROAD_WORD.search(seg) else place_t).update(name_tokens(seg))
    admin = name_tokens(state)
    d, c = name_tokens(district), name_tokens(city)
    own_town = d if (d and d <= c) else set()
    if not own_town:
        admin |= d
    road_t -= admin
    place_t -= admin
    place_t -= {t for t in place_t if _is_highway_ref(t)}
    weak = {t for t in road_t | place_t
            if t in COMMON_NAMES or (t in other_towns and t not in own_town)}
    return Stated(frozenset(road_t), frozenset(place_t), frozenset(weak))


def _matches(stated: frozenset, candidate: str | None) -> list[str]:
    """Stated words that the candidate name contains (spelling-tolerant), sorted."""
    cand = name_tokens(candidate)
    return sorted(s for s in stated if any(similar(s, h) for h in cand))


def evidence(stated: Stated, hit: dict) -> tuple[str | None, str | None]:
    """(precision, matched_word) for one geocoder hit, or (None, None) to reject it.

    hit: {"name", "ftype", "ctx": {"street", "neighborhood", "locality", "place"}}.
    A missing key is treated as absent, never as a match: a hit that carries no name
    cannot be shown to be the right place, so it is rejected (fail closed)."""
    if not stated:
        return None, None
    ftype = (hit.get("ftype") or "").lower()
    name = hit.get("name")
    ctx = hit.get("ctx") or {}
    strong_place = stated.place - stated.weak
    near = (_matches(strong_place, ctx.get("neighborhood"))
            + _matches(strong_place, ctx.get("locality")))
    in_town = near + _matches(strong_place, ctx.get("place"))

    if ftype in STREET_FTYPES:
        on_road = _matches(stated.road, name)
        named = [t for t in on_road if t not in stated.weak and not _is_highway_ref(t)]
        if named:
            return ("spot" if ftype == "address" else "road"), named[0]
        any_word = on_road or _matches(stated.place, name)
        if any_word and in_town:
            # a weak or landmark word, AND the hit lies in a place the article names
            return ("road" if on_road else "area"), any_word[0]
        if len(on_road) >= 2 and not all(_is_highway_ref(t) for t in on_road):
            return "road", on_road[0]
        if near:
            # generic street ("Road") inside the named village or neighbourhood
            return "area", near[0]
        if any(_is_highway_ref(t) for t in on_road):
            return "highway", next(t for t in on_road if _is_highway_ref(t))
        return None, None

    if ftype in AREA_FTYPES:
        named = _matches(strong_place, name)
        if named:
            return "area", named[0]
        weak_named = _matches(stated.place, name)
        if len(weak_named) >= 2 or (weak_named and in_town):
            return "area", weak_named[0]
    return None, None
