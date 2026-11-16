"""A geocoder hit must name the place the article names (16 Nov 2026).

14 Nov audit: 26 of 40 sampled public pins sat more than 10 km from the place in the
article. The provider, asked for a village road it does not know, answers with a
street in the district town literally named "Road" — right state, right district,
0.8 confidence, published. The hits below are verbatim Mapbox answers from 16 Nov.
"""
import pytest

from pipeline.processing import placematch as pm

UP = "Uttar Pradesh"


TOWNS = frozenset({"sitapur", "varanasi", "hardoi", "mathura", "lucknow", "delhi"})


def _stated(loc, road=None, city=None, district=None, state=UP):
    return pm.stated_names(loc, road, city, district, state, TOWNS)


def _words(stated):
    return set(stated.road | stated.place)


# --------------------------------------------------------------- the failure itself
ROAD_IN_HARDOI = {"name": "Road", "ftype": "street",
                  "ctx": {"street": "Cinema Road", "place": "Hardoi"}}


def test_a_street_called_road_in_the_district_town_is_not_the_village_road():
    stated = _stated("Aureni-Mahapur road, Hardoi district, Uttar Pradesh", district="Hardoi")
    assert _words(stated) == {"aureni", "mahapur"} and stated.road == {"aureni", "mahapur"}
    assert pm.evidence(stated, ROAD_IN_HARDOI) == (None, None)


@pytest.mark.parametrize("name", ["Road", "Main Road", "Near", "Chauraha", "Station Road",
                                  "Bypass Road", "Link Road", "Gali No 4", ""])
def test_generic_street_names_are_never_evidence(name):
    stated = _stated("Arwal village road to thana and bank, Hardoi district", district="Hardoi")
    assert pm.evidence(stated, {"name": name, "ftype": "street", "ctx": {"place": "Hardoi"}}) \
        == (None, None)


def test_the_district_name_alone_is_not_evidence():
    """Every place in Hardoi district is 'in Hardoi'. The ladder's district-tail
    variant used to return the district town at 0.70 as 'road_city'."""
    stated = _stated("Bandari to Kaikhai village road, Sandi block, Hardoi district",
                     district="Hardoi")
    assert "hardoi" not in _words(stated)
    town = {"name": "Hardoi", "ftype": "place", "ctx": {"place": "Hardoi"}}
    assert pm.evidence(stated, town) == (None, None)


# ------------------------------------------------------------------ what does pass
def test_the_right_road_passes_through_a_transliteration_difference():
    stated = _stated("Kosi Kalan-Shahpur road, Mathura, Uttar Pradesh", district="Mathura")
    hit = {"name": "Sahpur Road", "ftype": "street",
           "ctx": {"street": "Sahpur Road", "locality": "Kosi Kalan", "place": "Chhata"}}
    assert pm.evidence(stated, hit) == ("road", "shahpur")


def test_a_named_town_is_an_area():
    stated = _stated("Baifariya-Bilgram Rajwaha link road, Bilgram block, Hardoi district",
                     district="Hardoi")
    hit = {"name": "Bilgram", "ftype": "place", "ctx": {"place": "Bilgram"}}
    assert pm.evidence(stated, hit) == ("area", "bilgram")


def test_a_generic_street_inside_the_named_locality_is_an_area_not_a_road():
    stated = _stated("near Kurseli village, Hardoi district", district="Hardoi")
    hit = {"name": "Road", "ftype": "street",
           "ctx": {"street": "Road", "locality": "Kurseli", "place": "Hardoi"}}
    assert pm.evidence(stated, hit) == ("area", "kurseli")


def test_a_report_about_the_district_town_itself_may_use_the_towns_name():
    stated = _stated("Station Road, Hardoi city, Uttar Pradesh", city="Hardoi", district="Hardoi")
    assert _words(stated) == {"hardoi"} and not stated.weak
    town = {"name": "Hardoi", "ftype": "place", "ctx": {"place": "Hardoi"}}
    assert pm.evidence(stated, town) == ("area", "hardoi")
    # ...but a generic street filed under the town is not the town: Mapbox files
    # village streets 15 km out under the district town (audit rows 48500, 46777)
    street = {"name": "Main Road", "ftype": "street",
              "ctx": {"street": "Main Road", "locality": "Gagalhedi", "place": "Hardoi"}}
    assert pm.evidence(stated, street) == (None, None)


def test_numbered_highways_match_however_they_are_written_and_never_fuzzily():
    stated = _stated("NH-31 near Bakhri, Begusarai, Bihar", road="NH-31",
                     district="Begusarai", state="Bihar")
    assert "nh31" in stated.road
    # a numbered highway alone is the right road and an arbitrary point on it
    assert pm.evidence(stated, {"name": "National Highway 31", "ftype": "street", "ctx": {}}) \
        == ("highway", "nh31")
    # ...unless the hit also lies in a place the article names
    assert pm.evidence(stated, {"name": "NH 31", "ftype": "street",
                                "ctx": {"locality": "Bakhri"}}) == ("road", "nh31")
    assert pm.evidence(stated, {"name": "NH 331", "ftype": "street", "ctx": {}}) == (None, None)
    assert pm.highway_refs("on S.H. 77 and state highway 12A") == {"sh77", "sh12a"}


def test_an_address_level_match_is_a_spot():
    stated = _stated("Hazratganj Road near GPO, Lucknow", city="Lucknow", district="Lucknow")
    hit = {"name": "12 Hazratganj Road", "ftype": "address", "ctx": {"place": "Lucknow"}}
    assert pm.evidence(stated, hit) == ("spot", "hazratganj")


# ------------------------------------------------------------------- fail closed
def test_missing_input_rejects_instead_of_passing():
    """The lesson of 18 Jul: a validity check that cannot run must not pass."""
    hit = {"name": "Bilgram", "ftype": "place", "ctx": {}}
    nothing = pm.stated_names(None, None, None, "Hardoi", UP)
    assert not nothing and pm.evidence(nothing, hit) == (None, None)     # nothing stated
    bilgram = _stated("Bilgram block, Hardoi district", district="Hardoi")
    assert pm.evidence(bilgram, hit) == ("area", "bilgram")              # the control
    assert pm.evidence(bilgram, {}) == (None, None)                      # hit has no name
    assert pm.evidence(bilgram, {"name": None, "ftype": None, "ctx": None}) == (None, None)
    assert pm.evidence(bilgram, {"name": "Bilgram", "ftype": "district", "ctx": {}}) \
        == (None, None)                                                  # not a place type


@pytest.mark.parametrize("a,b,same", [
    ("sahpur", "shahpur", True), ("shahpur", "shahapur", True), ("bilgram", "bilgraam", True),
    ("rampur", "ranipur", False), ("sitapur", "sultanpur", False),
    ("mahapur", "madhopur", False), ("ganj", "gunj", False),      # too short to trust
])
def test_similarity_allows_spelling_but_not_a_different_village(a, b, same):
    assert pm.similar(a, b) is same


def test_short_and_generic_words_are_dropped_from_names():
    assert pm.name_tokens("Near Ram Nagar Chauraha, Main Road") == set()
    assert pm.name_tokens("Kosi Kalan – Shahpur Road") == {"kosi", "shahpur"}


# ------------------------------------------- the three traps (audit rows, 16 Nov)
def test_a_road_named_after_another_districts_town_is_a_direction_not_a_location():
    """Audit row 52866: crash near Sandiluwa village on the Kasta-Sitapur road, pinned
    on 'Sitapur Rd' inside Lakhimpur town, 20+ km away."""
    stated = _stated("near Sandiluwa village bend, Kasta-Sitapur Road, Lakhimpur Kheri district",
                     district="Lakhimpur Kheri")
    assert "sitapur" in stated.weak
    hit = {"name": "Sitapur Rd", "ftype": "street",
           "ctx": {"street": "Sitapur Rd", "locality": "Maharaj Nagar", "place": "Lakhimpur"}}
    assert pm.evidence(stated, hit) == (None, None)
    # the same weak word IS enough when the hit lies in the village the article names
    there = {"name": "Sitapur Rd", "ftype": "street",
             "ctx": {"street": "Sitapur Rd", "locality": "Sandiluwa", "place": "Lakhimpur"}}
    assert pm.evidence(stated, there) == ("road", "sitapur")


def test_a_destination_road_inside_the_named_town_passes():
    """Audit row judged correct within 2 km: 'Ladnun Road ... Sujangarh'."""
    stated = _stated("Ladnun Road, near Ramdevji Mandir, Ward 13, Sujangarh, Churu district",
                     city="Sujangarh", district="Churu", state="Rajasthan")
    hit = {"name": "Ladnun Rd", "ftype": "street",
           "ctx": {"street": "Ladnun Rd", "place": "Sujangarh"}}
    assert pm.evidence(stated, hit) == ("road", "ladnun")


def test_a_common_colony_name_alone_is_not_evidence():
    """Audit row 16436: 'Saini Colony ... Rampur town, Bansur' pinned on a street
    called 'Saini' in Vijay Nagar, Alwar. Audit row 8081: Jawahar Nagar."""
    stated = _stated("Saini Colony, in front of Government School, Rampur town, Bansur, Alwar district",
                     city="Bansur", district="Alwar", state="Rajasthan")
    assert "saini" in stated.weak
    hit = {"name": "Saini", "ftype": "street", "ctx": {"street": "Saini", "locality": "Vijay Nagar",
                                                       "place": "Alwar"}}
    assert pm.evidence(stated, hit) == (None, None)
    jn = _stated("Jawahar Nagar Paschimi Ward-06, Kanpur Nagar district", district="Kanpur Nagar")
    assert pm.evidence(jn, {"name": "Jawahar Nagar", "ftype": "locality", "ctx": {}}) == (None, None)


def test_a_street_named_after_a_place_word_needs_the_place_too():
    """Audit row 17742: 'Basai Puliya ... Khetri subdivision' pinned on 'Khetri Rd'
    in Chirawa — a road named for where it goes."""
    stated = _stated("Basai Puliya, Basai river, Khetri subdivision, Jhunjhunu district",
                     district="Jhunjhunu", state="Rajasthan")
    hit = {"name": "Khetri Rd", "ftype": "street", "ctx": {"street": "Khetri Rd", "place": "Chirawa"}}
    assert pm.evidence(stated, hit) == (None, None)
